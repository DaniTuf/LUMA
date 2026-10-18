"""Multi-worker behaviour of the database layer against REAL PostgreSQL (skipped unless TEST_DATABASE_URL is a postgres URL).

    docker run -d --rm --name luma_pytest_pg -p 127.0.0.1:55432:5432 -e POSTGRES_USER=luma -e POSTGRES_PASSWORD=luma -e POSTGRES_DB=luma_test postgres:16-alpine
    set TEST_DATABASE_URL=postgresql+asyncpg://luma:luma@127.0.0.1:55432/luma_test
"""
import asyncio
import os
import subprocess
import sys
import textwrap

import pytest
from sqlalchemy import text

from backend import config, db
from sdb import reset_database

PG = os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql")
pytestmark = pytest.mark.skipif(not PG, reason="needs a real PostgreSQL (TEST_DATABASE_URL)")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def clean():
    reset_database()
    yield


async def test_schema_creation_is_safe_when_many_workers_start_at_once():
    async with db.sessions()() as s:
        await s.execute(text("DROP TABLE messages, conversations, users CASCADE"))
        await s.commit()
    results = await asyncio.gather(*[db.init_db() for _ in range(12)], return_exceptions=True)
    assert [r for r in results if isinstance(r, Exception)] == [], "advisory lock must serialise create_all"
    assert await db.ping()


async def test_concurrent_logins_of_the_same_student_do_not_conflict():
    await asyncio.gather(*[db.upsert_user("same@tuf.edu.pk", f"Name {i}", "") for i in range(40)])
    assert (await db.get_user("same@tuf.edu.pk"))["email"] == "same@tuf.edu.pk"


async def test_concurrent_messages_get_unique_increasing_ids_and_keep_order_within_a_chat():
    await db.upsert_user("a@tuf.edu.pk", "A", "")
    cid = await db.create_conversation("a@tuf.edu.pk", "chat")
    for i in range(20):
        await db.add_message(cid, "user", f"q{i}")
    await asyncio.gather(*[db.add_message(cid, "assistant", f"parallel {i}") for i in range(30)])
    msgs = await db.get_messages(cid)
    assert len(msgs) == 50 and [m["content"] for m in msgs[:20]] == [f"q{i}" for i in range(20)]
    assert (await db.get_messages(cid, limit=5)) == msgs[-5:]


async def test_deleting_a_conversation_cascades_and_never_touches_another_students_chat():
    await db.upsert_user("a@tuf.edu.pk", "A", "")
    await db.upsert_user("b@tuf.edu.pk", "B", "")
    ca, cb = await db.create_conversation("a@tuf.edu.pk", "a"), await db.create_conversation("b@tuf.edu.pk", "b")
    await db.add_message(ca, "user", "x"); await db.add_message(cb, "user", "y")
    assert await db.delete_conversation("b@tuf.edu.pk", ca) is False and await db.get_conversation("b@tuf.edu.pk", ca) is None
    assert await db.delete_conversation("a@tuf.edu.pk", ca) is True
    assert await db.count_messages(ca) == 0 and await db.count_messages(cb) == 1


async def test_pool_exhaustion_waits_then_reports_database_unavailable_not_a_traceback(monkeypatch):
    await db.dispose()
    monkeypatch.setattr(config, "DB_POOL_SIZE", 1)
    monkeypatch.setattr(config, "DB_MAX_OVERFLOW", 0)
    monkeypatch.setattr(config, "DB_POOL_TIMEOUT", 0.5)
    monkeypatch.setattr(config, "DB_RETRIES", 0)
    await db.dispose()
    async with db.sessions()() as held:                         # the only connection is checked out
        await held.execute(text("SELECT 1"))
        with pytest.raises(db.DatabaseUnavailable):
            await db.get_user("x@tuf.edu.pk")
    await db.dispose()


async def test_a_killed_connection_is_replaced_transparently():
    await db.upsert_user("a@tuf.edu.pk", "A", "")
    async with db.sessions()() as s:                            # kill every other connection of this application (as a failover would)
        await s.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() "
                             "AND pid <> pg_backend_pid() AND application_name LIKE 'luma-%'"))
        await s.commit()
    assert (await db.get_user("a@tuf.edu.pk"))["email"] == "a@tuf.edu.pk"     # pool_pre_ping + retry hide it


async def test_statement_timeout_is_set_on_every_connection():
    async with db.sessions()() as s:
        assert (await s.execute(text("SHOW statement_timeout"))).scalar() in ("10s", "10000ms")
        assert (await s.execute(text("SHOW application_name"))).scalar().startswith("luma-")


def test_four_real_worker_processes_share_one_database():
    """Four separate Python processes (= four uvicorn workers) start together, create the schema and hammer the same chat."""
    script = textwrap.dedent("""
        import asyncio, os, sys
        sys.path.insert(0, %r)
        from backend import db
        async def main(n):
            await db.init_db()
            await db.upsert_user("shared@tuf.edu.pk", "S", "")
            cid = sys.argv[2]
            for i in range(25):
                await db.add_message(cid, "user", f"w{n}-{i}")
            await db.dispose()
        asyncio.run(main(sys.argv[1]))
    """ % ROOT)
    cid = asyncio.run(_prepare())
    env = dict(os.environ, DATABASE_URL=os.environ["TEST_DATABASE_URL"])
    procs = [subprocess.Popen([sys.executable, "-c", script, str(n), cid], env=env, cwd=ROOT, stderr=subprocess.PIPE) for n in range(4)]
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()[-800:]
    assert asyncio.run(_count(cid)) == 100


async def _prepare():
    await db.init_db()
    await db.upsert_user("shared@tuf.edu.pk", "S", "")
    cid = await db.create_conversation("shared@tuf.edu.pk", "shared")
    await db.dispose()
    return cid


async def _count(cid):
    n = await db.count_messages(cid)
    await db.dispose()
    return n
