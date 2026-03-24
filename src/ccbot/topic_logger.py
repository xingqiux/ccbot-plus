"""Per-topic file logging for easy debugging.

Creates individual log files under ~/.ccbot/logs/ for each Telegram topic:
  topic-<thread_id>-<display_name>.log

Each entry includes timestamp, event type, window/session info.
Use log_topic_event() from any module to record topic-specific events.

Key function: log_topic_event(user_id, thread_id, event, **details)
"""

import logging
import re

from .session import session_manager
from .utils import ccbot_dir

_LOGS_DIR = ccbot_dir() / "logs"
_LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Cache: (user_id, thread_id) → Logger instance
_topic_loggers: dict[tuple[int, int], logging.Logger] = {}

# Sanitize display names for filenames
_SAFE_NAME_RE = re.compile(r"[^\w\-.]", re.UNICODE)


def _sanitize_name(name: str) -> str:
    """Make a display name safe for use in filenames."""
    sanitized = _SAFE_NAME_RE.sub("_", name)
    return sanitized[:50] or "unknown"


def get_topic_logger(user_id: int, thread_id: int) -> logging.Logger:
    """Get or create a file logger for a specific topic."""
    key = (user_id, thread_id)
    if key in _topic_loggers:
        return _topic_loggers[key]

    # Resolve display name from thread binding
    wid = session_manager.get_window_for_thread(user_id, thread_id)
    display = session_manager.get_display_name(wid) if wid else "unbound"
    safe_name = _sanitize_name(display)

    log_file = _LOGS_DIR / f"topic-{thread_id}-{safe_name}.log"
    logger_name = f"ccbot.topic.{thread_id}"

    topic_logger = logging.getLogger(logger_name)
    # Avoid duplicate handlers if logger already exists
    if not topic_logger.handlers:
        topic_logger.setLevel(logging.DEBUG)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        topic_logger.addHandler(handler)
        topic_logger.propagate = False

    _topic_loggers[key] = topic_logger
    return topic_logger


def log_topic_event(
    user_id: int, thread_id: int, event: str, **details: object
) -> None:
    """Log an event for a specific topic.

    Args:
        user_id: Telegram user ID
        thread_id: Telegram thread (topic) ID
        event: Event type (msg_sent, msg_received, claude_exit, auto_restart, etc.)
        **details: Additional key-value pairs to include in the log line
    """
    topic_logger = get_topic_logger(user_id, thread_id)
    wid = session_manager.get_window_for_thread(user_id, thread_id)
    parts = [f"event={event}"]
    if wid:
        parts.append(f"window={wid}")
        state = session_manager.get_window_state(wid)
        if state.session_id:
            parts.append(f"session={state.session_id[:8]}...")
    for k, v in details.items():
        parts.append(f"{k}={v}")
    topic_logger.info(" | ".join(parts))


def refresh_topic_logger(user_id: int, thread_id: int) -> None:
    """Remove cached logger so next call picks up a new display name."""
    key = (user_id, thread_id)
    old = _topic_loggers.pop(key, None)
    if old:
        for h in old.handlers[:]:
            h.close()
            old.removeHandler(h)
