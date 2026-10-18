"""Per-student daily message cap, shared by every worker through Redis (see state.py; in-memory when Redis is not configured)."""
from . import config, state


async def check_and_increment(user_id: str) -> tuple[bool, int]:
    """Count one message for today. Returns (allowed, remaining_after_this_call). Atomic: two workers cannot both take the last slot."""
    return await state.incr_daily(user_id, config.MAX_MESSAGES_PER_USER_PER_DAY)
