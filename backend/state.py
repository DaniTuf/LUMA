"""Shared, fast state for every LUMA worker: Redis when REDIS_URL is set, an in-memory fallback otherwise.

What lives here (all of it must survive a worker restart or be visible to the other workers):
  * daily message counters      -> the per-student cap works across workers and restarts
  * provider cool-downs         -> a rate-limited LLM is skipped by every worker, not just the one that saw the error
  * turn checkpoints            -> the text an answer already has, so another provider / worker continues where the last one stopped
  * failure log (Redis stream)  -> every LLM / voice / judge / worker failure with its retry decision, for the operators
  * live-lesson locks           -> one lesson per student and a global cap, across workers

Design rules: every call is async and has a short timeout; a Redis error NEVER breaks a lesson (the call falls back to the memory
store and the failure is logged); the Redis client is created per event loop (uvicorn has one, the test client has several).
"""
import asyncio
import datetime
import json
import logging
import re
import time
from collections import deque

from . import config

log = logging.getLogger("luma.state")

_client = None
_client_loop = None
_down_until = 0.0                      # after a Redis error we use memory for a few seconds instead of timing out on every call
_ERR_BACKOFF = 5.0

# in-memory fallback ---------------------------------------------------------------------------------------------------
_mem_counts: dict[str, int] = {}
_mem_kv: dict[str, tuple[float, str]] = {}          # key -> (expires_at, value)
_mem_lessons: dict[str, float] = {}                 # email -> expires_at
_mem_turns: dict[str, dict] = {}
_mem_failures: deque = deque(maxlen=1000)

_SECRET = re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{16,}|gsk_[A-Za-z0-9]{16,}|sk_[a-f0-9]{24,}|Bearer\s+[A-Za-z0-9._\-]+|key=[^&\s]+)")


def scrub(text: object, limit: int = 300) -> str:
    """Error text for logs: no API keys, no newlines, bounded."""
    s = _SECRET.sub("[redacted]", " ".join(str(text).split()))
    for secret in (config.GEMINI_API_KEY, config.GROQ_API_KEY, config.ANTHROPIC_API_KEY, config.ELEVENLABS_API_KEY):
        if secret and len(secret) > 6:
            s = s.replace(secret, "[redacted]")
    return s[:limit]


def reset_memory() -> None:
    """Tests: forget everything held in memory (and drop the Redis client)."""
    global _client, _client_loop, _down_until
    _mem_counts.clear(); _mem_kv.clear(); _mem_lessons.clear(); _mem_turns.clear(); _mem_failures.clear()
    _client = _client_loop = None
    _down_until = 0.0


def use_client(client) -> None:
    """Tests: use this (fake) Redis client regardless of REDIS_URL."""
    global _client, _client_loop
    _client, _client_loop = client, "fixed"


def _key(*parts: str) -> str:
    return ":".join((config.REDIS_PREFIX, *parts))


def _get_client():
    """The Redis client for the running loop, or None (not configured / recently failed)."""
    global _client, _client_loop
    if _client_loop == "fixed":
        return _client
    if not config.REDIS_URL or time.time() < _down_until:
        return None
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        import redis.asyncio as aioredis
        _client = aioredis.from_url(config.REDIS_URL, decode_responses=True, socket_connect_timeout=config.REDIS_TIMEOUT,
                                    socket_timeout=config.REDIS_TIMEOUT, health_check_interval=30)
        _client_loop = loop
    return _client


def _failed(op: str, exc: Exception) -> None:
    global _down_until
    _down_until = time.time() + _ERR_BACKOFF
    log.warning("redis %s failed (%s: %s); using memory for %.0fs", op, type(exc).__name__, scrub(exc, 160), _ERR_BACKOFF)


async def _redis(op: str, call):
    """Run `call(client)` against Redis. Returns (True, result), or (False, None) when Redis is absent or failed."""
    c = _get_client()
    if c is None:
        return False, None
    try:
        return True, await asyncio.wait_for(call(c), config.REDIS_TIMEOUT + 0.5)
    except Exception as exc:                                  # connection refused, timeout, wrong type ... never fatal
        _failed(op, exc)
        return False, None


async def ping() -> bool:
    ok, res = await _redis("ping", lambda c: c.ping())
    return bool(ok and res)


async def status() -> dict:
    return {"configured": bool(config.REDIS_URL) or _client_loop == "fixed", "connected": await ping()}


async def close() -> None:
    global _client, _client_loop
    c, _client, _client_loop = _client, None, None
    if c is not None:
        try:
            await c.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------------- daily usage cap
def _today() -> str:
    return datetime.date.today().isoformat()


async def incr_daily(user_id: str, limit: int) -> tuple[bool, int]:
    """Count one message for today. Returns (allowed, remaining). Atomic in Redis, so two workers cannot both take the last slot."""
    key = _key("usage", _today(), user_id)

    async def go(c):
        n = await c.incr(key)
        if n == 1:
            await c.expire(key, 172800)
        if n > limit:
            await c.decr(key)
            return False, 0
        return True, limit - n

    ok, res = await _redis("incr_daily", go)
    if ok:
        return res
    n = _mem_counts.get(key, 0)
    if n >= limit:
        return False, 0
    _mem_counts[key] = n + 1
    return True, limit - n - 1


# ---------------------------------------------------------------------------------------------------- provider cool-down
async def set_cooldown(provider: str, seconds: int) -> None:
    key = _key("cooldown", provider)
    ok, _ = await _redis("set_cooldown", lambda c: c.set(key, "1", ex=max(1, int(seconds))))
    _mem_kv[key] = (time.time() + seconds, "1")               # also remembered locally: a Redis blip must not re-enable a dead provider


async def in_cooldown(provider: str) -> bool:
    key = _key("cooldown", provider)
    exp = _mem_kv.get(key, (0.0, ""))[0]
    if exp > time.time():
        return True
    ok, res = await _redis("in_cooldown", lambda c: c.exists(key))
    return bool(ok and res)


# ---------------------------------------------------------------------------------------------------- failure log
async def log_failure(component: str, error: object, **ctx) -> None:
    """Record a failure and what was done about it (retry / failover / resume / gave up). Never raises."""
    entry = {"ts": f"{time.time():.3f}", "component": component, "error": scrub(error)}
    for k, v in ctx.items():
        if v is not None:
            entry[k] = scrub(v, 120)
    log.warning("failure %s", json.dumps(entry, ensure_ascii=False))
    _mem_failures.append(entry)
    try:
        await _redis("log_failure", lambda c: c.xadd(_key("failures"), entry, maxlen=config.FAILURE_LOG_MAX, approximate=True))
        await _redis("log_failure_count", lambda c: c.hincrby(_key("failure_counts", _today()), component, 1))
    except Exception:
        pass


async def recent_failures(count: int = 50) -> list[dict]:
    ok, res = await _redis("recent_failures", lambda c: c.xrevrange(_key("failures"), count=count))
    if ok and res is not None:
        return [dict(fields, id=eid) for eid, fields in res]
    return list(_mem_failures)[-count:][::-1]


# ---------------------------------------------------------------------------------------------------- turn checkpoints
# A "turn" is one question and its written answer. The answer text is appended while it is generated, so if the LLM, the worker or the
# whole process dies, whoever picks the turn up continues from the saved text instead of starting again.
async def checkpoint_start(turn_id: str, meta: dict) -> None:
    key = _key("turn", turn_id)
    data = {k: ("" if v is None else str(v)) for k, v in meta.items()}
    data.update(status="running", started=f"{time.time():.0f}")
    _mem_turns[turn_id] = dict(data, text="")
    ok, _ = await _redis("checkpoint_start", lambda c: _start_pipe(c, key, data, meta, turn_id))
    if not ok:
        log.debug("checkpoint %s kept in memory only", turn_id)


async def _start_pipe(c, key, data, meta, turn_id):
    async with c.pipeline(transaction=False) as p:
        p.hset(key, mapping=data)
        p.expire(key, config.CHECKPOINT_TTL_SECONDS)
        if meta.get("email") and meta.get("cid"):
            p.set(_key("open", meta["email"], meta["cid"]), turn_id, ex=config.CHECKPOINT_TTL_SECONDS)
        await p.execute()


async def checkpoint_append(turn_id: str, text: str) -> None:
    if not text:
        return
    t = _mem_turns.get(turn_id)
    if t is not None:
        t["text"] = t.get("text", "") + text
    key = _key("turn", turn_id)
    await _redis("checkpoint_append", lambda c: _append_pipe(c, key, text))


async def _append_pipe(c, key, text):
    async with c.pipeline(transaction=False) as p:
        p.append(key + ":text", text)
        p.expire(key + ":text", config.CHECKPOINT_TTL_SECONDS)
        p.hset(key, "updated", f"{time.time():.0f}")
        await p.execute()


async def checkpoint_set(turn_id: str, **fields) -> None:
    key = _key("turn", turn_id)
    data = {k: str(v) for k, v in fields.items()}
    if turn_id in _mem_turns:
        _mem_turns[turn_id].update(data)
    await _redis("checkpoint_set", lambda c: c.hset(key, mapping=data))


async def checkpoint_get(turn_id: str) -> dict | None:
    key = _key("turn", turn_id)

    async def go(c):
        h = await c.hgetall(key)
        if not h:
            return None
        h["text"] = (await c.get(key + ":text")) or ""
        return h

    ok, res = await _redis("checkpoint_get", go)
    if ok:
        return res
    t = _mem_turns.get(turn_id)
    return dict(t) if t else None


async def checkpoint_finish(turn_id: str, status: str = "done") -> None:
    """status: done | abandoned | failed. A finished turn is not offered for resuming again."""
    t = _mem_turns.get(turn_id)
    meta = dict(t) if t else None
    if t is not None:
        t["status"] = status
    key = _key("turn", turn_id)
    if meta is None:
        ok, h = await _redis("checkpoint_finish_get", lambda c: c.hgetall(key))
        meta = h if ok and h else {}

    async def go(c):
        async with c.pipeline(transaction=False) as p:
            p.hset(key, "status", status)
            p.expire(key, 600 if status == "done" else config.CHECKPOINT_TTL_SECONDS)
            p.expire(key + ":text", 600 if status == "done" else config.CHECKPOINT_TTL_SECONDS)
            if status == "done" and meta.get("email") and meta.get("cid"):
                p.delete(_key("open", meta["email"], meta["cid"]))
            await p.execute()

    await _redis("checkpoint_finish", go)


async def find_open_turn(email: str, cid: str) -> dict | None:
    """The unfinished turn of this student's conversation, if a worker died while writing it."""
    ok, tid = await _redis("find_open_turn", lambda c: c.get(_key("open", email, cid)))
    if ok:
        if not tid:
            return None
        h = await checkpoint_get(tid)
        return dict(h, id=tid) if h and h.get("status") in ("running", "failed") else None
    for tid, t in _mem_turns.items():
        if t.get("email") == email and t.get("cid") == cid and t.get("status") in ("running", "failed"):
            return dict(t, id=tid)
    return None


async def claim_turn(turn_id: str, ttl: int = 120) -> bool:
    """Exactly one worker may continue an unfinished turn: the first caller wins."""
    key = _key("claim", turn_id)
    ok, res = await _redis("claim_turn", lambda c: c.set(key, "1", nx=True, ex=ttl))
    if ok:
        return bool(res)
    if _mem_kv.get(key, (0.0, ""))[0] > time.time():
        return False
    _mem_kv[key] = (time.time() + ttl, "1")
    return True


class CheckpointWriter:
    """Appends answer pieces to the checkpoint in the background (batched), so saving never adds latency to the stream."""

    def __init__(self, turn_id: str | None, every: float = 0.25):
        self.turn_id, self.every = turn_id, every
        self._buf: list[str] = []
        self._task: asyncio.Task | None = None

    def add(self, piece: str) -> None:
        if not self.turn_id or not piece:
            return
        self._buf.append(piece)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(self.every)
            await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        if self._buf:
            text, self._buf = "".join(self._buf), []
            await checkpoint_append(self.turn_id, text)

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._flush()


# ---------------------------------------------------------------------------------------------------- live lesson locks
LOCK_TTL = 90          # seconds; the lesson refreshes it every ping, so a crashed worker's lock disappears by itself


async def acquire_lesson(email: str, max_concurrent: int) -> str:
    """'ok' | 'duplicate' (this student already has a lesson) | 'busy' (global cap reached)."""
    now = time.time()
    zkey, lkey = _key("lessons"), _key("lesson", email)

    async def go(c):
        await c.zremrangebyscore(zkey, "-inf", now)
        if not await c.set(lkey, "1", nx=True, ex=LOCK_TTL):
            return "duplicate"
        if await c.zcard(zkey) >= max_concurrent:
            await c.delete(lkey)
            return "busy"
        await c.zadd(zkey, {email: now + LOCK_TTL})
        return "ok"

    ok, res = await _redis("acquire_lesson", go)
    if ok:
        return res
    for e, exp in list(_mem_lessons.items()):
        if exp <= now:
            _mem_lessons.pop(e, None)
    if email in _mem_lessons:
        return "duplicate"
    if len(_mem_lessons) >= max_concurrent:
        return "busy"
    _mem_lessons[email] = now + LOCK_TTL
    return "ok"


async def refresh_lesson(email: str) -> None:
    exp = time.time() + LOCK_TTL
    if email in _mem_lessons:
        _mem_lessons[email] = exp
    await _redis("refresh_lesson", lambda c: _refresh_pipe(c, email, exp))


async def _refresh_pipe(c, email, exp):
    async with c.pipeline(transaction=False) as p:
        p.expire(_key("lesson", email), LOCK_TTL)
        p.zadd(_key("lessons"), {email: exp}, xx=True)
        await p.execute()


async def release_lesson(email: str) -> None:
    _mem_lessons.pop(email, None)
    async def go(c):
        async with c.pipeline(transaction=False) as p:
            p.delete(_key("lesson", email))
            p.zrem(_key("lessons"), email)
            await p.execute()
    await _redis("release_lesson", go)


async def lesson_held(email: str) -> bool:
    if email in _mem_lessons and _mem_lessons[email] > time.time():
        return True
    ok, res = await _redis("lesson_held", lambda c: c.exists(_key("lesson", email)))
    return bool(ok and res)

