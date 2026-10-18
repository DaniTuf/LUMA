import os
import sys
import tempfile

# let test modules share helpers: `from test_live import ...`
sys.path.insert(0, os.path.dirname(__file__))

# The unit tests run on a throw-away SQLite file through the SAME async SQLAlchemy code (no server needed).
# To run them on real PostgreSQL:  set TEST_DATABASE_URL=postgresql+asyncpg://luma:luma@127.0.0.1:55432/luma_test   (its tables are emptied!)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(prefix="luma_test_"), "luma.db")
os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")
os.environ.setdefault("MAX_MESSAGES_PER_USER_PER_DAY", "500")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("REDIS_URL", "")


async def make_redis():
    """An in-process fake Redis, or a REAL one when REDIS_TEST_URL is set (e.g. redis://127.0.0.1:6379/15; the database is flushed first)."""
    url = os.environ.get("REDIS_TEST_URL")
    if url:
        import redis.asyncio as aioredis
        r = aioredis.from_url(url, decode_responses=True)
        await r.flushdb()
        return r
    import fakeredis
    return fakeredis.FakeAsyncRedis(decode_responses=True)
