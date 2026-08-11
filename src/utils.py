"""
Utility functions for the bot.
"""

import sys
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ANSI color helpers (enabled from main.py on Windows).
RESET = "\x1b[0m"
LOG_LEVELS = {
    "DEBUG": 10,
    "INFO": 20,
    "WARN": 30,
    "ERROR": 40,
    "SUCCESS": 25,
}
CURRENT_LOG_LEVEL = LOG_LEVELS["INFO"]
LOG_HISTORY = deque(maxlen=500)
LOG_HISTORY_LOCK = threading.Lock()
LEVEL_COLORS = {
    "DEBUG": "\x1b[90m",  # Bright black / gray
    "INFO": "\x1b[36m",  # Cyan
    "WARN": "\x1b[33m",  # Yellow
    "ERROR": "\x1b[31m",  # Red
    "SUCCESS": "\x1b[32m",  # Green
}


def set_log_level(level: str) -> None:
    """Set global log level threshold."""
    global CURRENT_LOG_LEVEL
    if not level:
        CURRENT_LOG_LEVEL = LOG_LEVELS["INFO"]
        return
    CURRENT_LOG_LEVEL = LOG_LEVELS.get(level.upper(), LOG_LEVELS["INFO"])


def log(message: str, level: str = "INFO") -> None:
    """
    Log a message with timestamp and level.

    Args:
        message: The message to log
        level: Log level (DEBUG, INFO, WARN, ERROR, SUCCESS)
    """
    level_key = (level or "INFO").upper()
    level_value = LOG_LEVELS.get(level_key, LOG_LEVELS["INFO"])

    # Skip messages below the current threshold.
    if level_value < CURRENT_LOG_LEVEL:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    thread_name = threading.current_thread().name
    thread_prefix = ""
    if thread_name and thread_name != "MainThread":
        thread_prefix = f"[{thread_name}] "

    with LOG_HISTORY_LOCK:
        LOG_HISTORY.append(
            {
                "timestamp": timestamp,
                "level": level_key,
                "thread": thread_name or "MainThread",
                "message": str(message),
            }
        )

    color = LEVEL_COLORS.get(level_key, "")
    if color:
        level_label = f"{color}{level_key:<7}{RESET}"
    else:
        level_label = f"{level_key:<7}"

    print(f"[{timestamp}] {level_label} {thread_prefix}{message}")
    sys.stdout.flush()


def get_log_history(limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent printed log entries, newest first."""
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 100
    with LOG_HISTORY_LOCK:
        items = list(LOG_HISTORY)[-limit:]
    return list(reversed(items))


def now_utc() -> datetime:
    """Get current UTC datetime."""
    return datetime.now(timezone.utc)


def format_seconds(seconds: int) -> str:
    """Format seconds into human-readable time."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes}m"


def format_file_size(bytes_size: int) -> str:
    """Format bytes into human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f}{unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f}TB"


def truncate_string(text: str, max_length: int = 100) -> str:
    """Truncate string to max length."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def is_url(text: str) -> bool:
    """Check if text is a URL."""
    return text.startswith(("http://", "https://"))


def sanitize_filename(filename: str) -> str:
    """Remove invalid characters from filename."""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, "_")
    return filename


def parse_time(time_str: str) -> Optional[tuple[int, int]]:
    """
    Parse time string in HH:MM format.

    Returns:
        Tuple of (hour, minute) or None if invalid.
    """
    try:
        parts = time_str.split(":")
        if len(parts) != 2:
            return None
        hour = int(parts[0])
        minute = int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute)
    except (ValueError, AttributeError):
        pass
    return None
