"""In-memory per-user daily usage tracking for the pilot.

Good enough for a single-process pilot deployment. Replace with a
persistent store (Redis/DB) before scaling beyond the pilot.
"""
import datetime
import threading

from . import config

_lock = threading.Lock()
_usage: dict[str, tuple[str, int]] = {}  # user_id -> (date_str, count)


def _today() -> str:
    return datetime.date.today().isoformat()


def check_and_increment(user_id: str) -> tuple[bool, int]:
    """Returns (allowed, remaining_after_this_call)."""
    today = _today()
    with _lock:
        date_str, count = _usage.get(user_id, (today, 0))
        if date_str != today:
            count = 0
        if count >= config.MAX_MESSAGES_PER_USER_PER_DAY:
            _usage[user_id] = (today, count)
            return False, 0
        count += 1
        _usage[user_id] = (today, count)
        remaining = config.MAX_MESSAGES_PER_USER_PER_DAY - count
        return True, remaining
