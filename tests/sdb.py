"""Synchronous facade over the async database module, for test code that sets up or inspects data (the app itself is always async)."""
import asyncio

from sqlalchemy import text

from backend import db


class _Sync:
    def __getattr__(self, name):
        fn = getattr(db, name)

        def call(*args, **kwargs):
            return asyncio.run(fn(*args, **kwargs))
        return call


sdb = _Sync()


def reset_database() -> None:
    """Empty every table and make sure the schema exists (before each test)."""
    async def go():
        await db.init_db()
        async with db.sessions()() as s:
            async with s.begin():
                if db._is_pg():
                    await s.execute(text("TRUNCATE messages, conversations, users RESTART IDENTITY CASCADE"))
                else:
                    for t in ("messages", "conversations", "users"):
                        await s.execute(text(f"DELETE FROM {t}"))
    asyncio.run(go())
