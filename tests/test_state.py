"""Redis-backed shared state (fakeredis speaks the real protocol) and its in-memory fallback."""
import asyncio

import fakeredis
import pytest

from backend import config, state
from conftest import make_redis


@pytest.fixture()
async def redis():
    state.reset_memory()
    r = await make_redis()
    state.use_client(r)
    yield r
    await r.aclose()
    state.reset_memory()


@pytest.fixture()
def memory_only(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", "")
    state.reset_memory()
    yield
    state.reset_memory()


async def test_daily_cap_is_atomic_and_stops_at_the_limit(redis):
    results = await asyncio.gather(*[state.incr_daily("a@tuf.edu.pk", 5) for _ in range(12)])
    assert sum(1 for ok, _ in results if ok) == 5, "two workers can never both take the last slot"
    assert await state.incr_daily("a@tuf.edu.pk", 5) == (False, 0)
    assert await state.incr_daily("b@tuf.edu.pk", 5) == (True, 4)
    keys = [k async for k in redis.scan_iter("luma:usage:*")]
    assert len(keys) == 2 and await redis.ttl(keys[0]) > 0


async def test_cooldown_is_shared_through_redis(redis):
    assert not await state.in_cooldown("gemini")
    await state.set_cooldown("gemini", 30)
    state._mem_kv.clear()                                   # as if another worker asked
    assert await state.in_cooldown("gemini") and not await state.in_cooldown("groq")


async def test_failure_log_is_a_redis_stream_and_never_contains_keys(redis, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIzaSyFAKEKEYFORTESTING1234567890abcdef")
    await state.log_failure("llm", RuntimeError("503 from https://x?key=AIzaSyFAKEKEYFORTESTING1234567890abcdef Bearer abc.def"),
                            provider="gemini", action="failover", attempt=1, nothing=None)
    rows = await state.recent_failures(10)
    assert len(rows) == 1 and rows[0]["component"] == "llm" and rows[0]["action"] == "failover" and "nothing" not in rows[0]
    assert "AIza" not in rows[0]["error"] and "abc.def" not in rows[0]["error"] and "[redacted]" in rows[0]["error"]
    assert await redis.xlen("luma:failures") == 1
    assert await redis.hget("luma:failure_counts:" + state._today(), "llm") == "1"


async def test_checkpoint_roundtrip_and_resume_lookup(redis):
    await state.checkpoint_start("t1", {"email": "a@tuf.edu.pk", "cid": "c1", "question": "What is a heap?", "language": "ur", "user_saved": "1"})
    w = state.CheckpointWriter("t1", every=0.01)
    for piece in ("# Heap\n", "A complete ", "binary tree."):
        w.add(piece)
    await w.close()
    got = await state.checkpoint_get("t1")
    assert got["text"] == "# Heap\nA complete binary tree." and got["question"] == "What is a heap?" and got["language"] == "ur"
    open_turn = await state.find_open_turn("a@tuf.edu.pk", "c1")
    assert open_turn["id"] == "t1" and open_turn["text"].endswith("binary tree.")
    assert await state.find_open_turn("other@tuf.edu.pk", "c1") is None, "another student's turn is never offered"
    await state.checkpoint_finish("t1", "done")
    assert await state.find_open_turn("a@tuf.edu.pk", "c1") is None, "a finished turn is not resumed again"
    assert await redis.ttl("luma:turn:t1") <= 600


async def test_only_one_worker_can_claim_a_turn(redis):
    assert await state.claim_turn("t9", 30) is True
    assert await state.claim_turn("t9", 30) is False


async def test_lesson_locks_one_per_student_and_a_global_cap(redis):
    assert await state.acquire_lesson("a@tuf.edu.pk", 2) == "ok"
    assert await state.acquire_lesson("a@tuf.edu.pk", 2) == "duplicate"
    assert await state.acquire_lesson("b@tuf.edu.pk", 2) == "ok"
    assert await state.acquire_lesson("c@tuf.edu.pk", 2) == "busy"
    assert await redis.exists("luma:lesson:c@tuf.edu.pk") == 0, "a refused student holds no lock"
    await state.release_lesson("a@tuf.edu.pk")
    assert await state.acquire_lesson("c@tuf.edu.pk", 2) == "ok"
    await state.refresh_lesson("c@tuf.edu.pk")
    assert await redis.ttl("luma:lesson:c@tuf.edu.pk") > 0


async def test_a_dead_workers_lock_expires_by_itself(redis):
    assert await state.acquire_lesson("a@tuf.edu.pk", 1) == "ok"
    await redis.delete("luma:lesson:a@tuf.edu.pk")           # the TTL ran out after the worker died
    await redis.zadd("luma:lessons", {"a@tuf.edu.pk": 1})   # ... and its slot is in the past
    assert await state.acquire_lesson("b@tuf.edu.pk", 1) == "ok"


# ------------------------------------------------------------------ Redis down: everything still works from memory
class BrokenRedis:
    def __getattr__(self, name):
        async def boom(*a, **k):
            raise ConnectionError("redis is down")
        return boom

    def pipeline(self, *a, **k):
        raise ConnectionError("redis is down")


async def test_redis_failure_falls_back_to_memory_without_raising():
    state.reset_memory()
    state.use_client(BrokenRedis())
    assert await state.incr_daily("a@tuf.edu.pk", 2) == (True, 1)
    assert await state.incr_daily("a@tuf.edu.pk", 2) == (True, 0)
    assert await state.incr_daily("a@tuf.edu.pk", 2) == (False, 0)
    await state.set_cooldown("groq", 30)
    assert await state.in_cooldown("groq")
    await state.log_failure("llm", "boom", action="failover")
    assert (await state.recent_failures(5))[0]["component"] == "llm"
    await state.checkpoint_start("t2", {"email": "a@tuf.edu.pk", "cid": "c2", "question": "q"})
    w = state.CheckpointWriter("t2", every=0.01)
    w.add("partial answer")
    await w.close()
    assert (await state.find_open_turn("a@tuf.edu.pk", "c2"))["text"] == "partial answer"
    assert await state.acquire_lesson("a@tuf.edu.pk", 1) == "ok" and await state.acquire_lesson("a@tuf.edu.pk", 1) == "duplicate"
    state.reset_memory()


def test_memory_mode_when_redis_is_not_configured(memory_only):
    async def go():
        assert (await state.status()) == {"configured": False, "connected": False}
        assert await state.incr_daily("m@tuf.edu.pk", 1) == (True, 0)
        assert await state.incr_daily("m@tuf.edu.pk", 1) == (False, 0)
    asyncio.run(go())


def test_scrub_removes_secrets_and_newlines():
    s = state.scrub("fail\nkey=SECRETVALUE sk-abcdefghijklmnopqrstuvwxyz gsk_abcdefghijklmnopqrstuv")
    assert "\n" not in s and "SECRETVALUE" not in s and "sk-abc" not in s and "gsk_abc" not in s

