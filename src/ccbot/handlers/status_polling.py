"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Periodically probes topic existence via unpin_all_forum_topic_messages
    (silent no-op when no pins); cleans up deleted topics (kills tmux window
    + unbinds thread)
  - Detects Claude Code process exit and auto-restarts with --resume

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
  - Auto-restart: detects pane_current_command != claude/node, restarts with cooldown
"""

import asyncio
import logging
import time

from telegram import Bot
from telegram.error import BadRequest

from ..config import config
from ..session import session_manager
from ..terminal_parser import is_interactive_ui, parse_status_line
from ..tmux_manager import TmuxWindow, tmux_manager
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .cleanup import clear_topic_state
from .message_queue import enqueue_status_update, get_message_queue
from .message_sender import safe_send
from ..topic_logger import log_topic_event

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Topic existence probe interval
TOPIC_CHECK_INTERVAL = 60.0  # seconds

# --- Auto-restart constants and state ---
_CLAUDE_PROCESS_NAMES = frozenset({"claude", "node"})
MAX_RESTARTS = 3
COOLDOWN_SECONDS = 300.0  # 5 minutes
# Per-window restart tracking: window_id → list of monotonic timestamps
_restart_history: dict[str, list[float]] = {}
# Track windows we've already notified about (avoid spamming every 1s poll)
_notified_exited: set[str] = set()


def _is_claude_running(window: TmuxWindow) -> bool:
    """Check if Claude Code is the foreground process in a tmux window."""
    return window.pane_current_command in _CLAUDE_PROCESS_NAMES


def _can_restart(window_id: str) -> bool:
    """Check if auto-restart is allowed (within cooldown limits)."""
    now = time.monotonic()
    history = _restart_history.get(window_id, [])
    # Prune entries older than cooldown
    history = [t for t in history if now - t < COOLDOWN_SECONDS]
    _restart_history[window_id] = history
    return len(history) < MAX_RESTARTS


async def _auto_restart_claude(
    bot: Bot, user_id: int, window_id: str, thread_id: int
) -> None:
    """Detect Claude exit and auto-restart with --resume if possible."""
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    display = session_manager.get_display_name(window_id)

    if not _can_restart(window_id):
        # Over limit — notify once then stop
        if window_id not in _notified_exited:
            _notified_exited.add(window_id)
            log_topic_event(
                user_id,
                thread_id,
                "auto_restart_limit",
                window=window_id,
                display=display,
            )
            await safe_send(
                bot,
                chat_id,
                f"❌ [{display}] Claude Code 反复退出（5 分钟内 {MAX_RESTARTS} 次），"
                "已停止自动重启，请手动处理。",
                message_thread_id=thread_id,
            )
            logger.warning(
                "Auto-restart limit reached for window %s (user=%d, thread=%d)",
                window_id,
                user_id,
                thread_id,
            )
        return

    # Record this restart attempt
    _restart_history.setdefault(window_id, []).append(time.monotonic())
    _notified_exited.discard(window_id)

    # Build restart command
    state = session_manager.get_window_state(window_id)
    if state.session_id:
        cmd = f"{config.claude_command} --resume {state.session_id}"
    else:
        cmd = config.claude_command

    success = await tmux_manager.send_keys(window_id, cmd)
    if success:
        log_topic_event(
            user_id,
            thread_id,
            "auto_restart",
            window=window_id,
            cmd=cmd,
        )
        await safe_send(
            bot,
            chat_id,
            f"⚠️ [{display}] Claude Code 已退出，正在自动重启...",
            message_thread_id=thread_id,
        )
        logger.info(
            "Auto-restarting Claude in window %s with: %s (user=%d, thread=%d)",
            window_id,
            cmd,
            user_id,
            thread_id,
        )
    else:
        logger.error(
            "Failed to send restart command to window %s (user=%d, thread=%d)",
            window_id,
            user_id,
            thread_id,
        )


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    status_line = parse_status_line(pane_text)

    if status_line:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
    # If no status line, keep existing status message (don't clear on transient state)


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_topic_check = 0.0
    while True:
        try:
            # Periodic topic existence probe
            now = time.monotonic()
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                for user_id, thread_id, wid in list(
                    session_manager.iter_thread_bindings()
                ):
                    try:
                        await bot.unpin_all_forum_topic_messages(
                            chat_id=session_manager.resolve_chat_id(user_id, thread_id),
                            message_thread_id=thread_id,
                        )
                    except BadRequest as e:
                        if "Topic_id_invalid" in str(e):
                            # Topic deleted — kill window, unbind, and clean up state
                            w = await tmux_manager.find_window_by_id(wid)
                            if w:
                                await tmux_manager.kill_window(w.window_id)
                            session_manager.unbind_thread(user_id, thread_id)
                            await clear_topic_state(user_id, thread_id, bot)
                            logger.info(
                                "Topic deleted: killed window_id '%s' and "
                                "unbound thread %d for user %d",
                                wid,
                                thread_id,
                                user_id,
                            )
                        else:
                            logger.debug(
                                "Topic probe error for %s: %s",
                                wid,
                                e,
                            )
                    except Exception as e:
                        logger.debug(
                            "Topic probe error for %s: %s",
                            wid,
                            e,
                        )

            for user_id, thread_id, wid in list(session_manager.iter_thread_bindings()):
                try:
                    # Clean up stale bindings (window no longer exists)
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        session_manager.unbind_thread(user_id, thread_id)
                        await clear_topic_state(user_id, thread_id, bot)
                        logger.info(
                            "Cleaned up stale binding: user=%d thread=%d window_id=%s",
                            user_id,
                            thread_id,
                            wid,
                        )
                        continue

                    # Detect Claude exit and auto-restart
                    if not _is_claude_running(w):
                        await _auto_restart_claude(bot, user_id, wid, thread_id)
                        continue

                    # Claude is running — clear any previous exit notification state
                    _notified_exited.discard(wid)

                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    queue = get_message_queue(user_id)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
