"""The 'transcribes the first question, then freezes on every later one (mostly on phones)' bug, and the other ways a lesson could stick.
Each test is a way the real Gemini session misbehaves; the lesson must recover on its own and the NEXT question must always work."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from sdb import reset_database, sdb  # noqa: F401
from backend import config, db, live, state
from backend.main import app
from test_live import EMAIL, FakeSession, audio, cid_of, client, collect, done, fake_notes, heard, install, said, start  # noqa: F401
from test_language_flow import interrupted


class MicSession(FakeSession):
    """Like FakeSession, but a scripted reply starts on each of the given microphone-frame counts (one 'utterance' = a few frames)."""

    def __init__(self, scripts, triggers):
        super().__init__(scripts)
        self.triggers = set(triggers)

    async def send_realtime_input(self, **kw):
        if "audio" in kw:
            self.sent_audio.append((kw["audio"].mime_type, len(kw["audio"].data)))
            if len(self.sent_audio) in self.triggers and self.scripts:
                self._start_answer()
            return
        await super().send_realtime_input(**kw)


def speak(ws, n=1):
    for _ in range(n):
        ws.send_bytes(b"\x00\x00" * 1600)


@pytest.fixture()
def fast(monkeypatch):
    for k, v in dict(LIVE_FIRST_AUDIO_SECONDS=0.5, LIVE_TURN_IDLE_SECONDS=0.5, LIVE_LATE_TRANSCRIPT_GRACE=0.2, LIVE_TICK_SECONDS=0.05).items():
        monkeypatch.setattr(config, k, v)


# ------------------------------------------------------------------ THE reported bug
def test_second_spoken_question_works_when_the_teacher_never_sent_turn_complete(client, monkeypatch):
    """Question 1 is answered but Gemini never sends 'turn complete'. The student speaks again (interrupting). Before the fix the second
    question was glued to the first ('what is recursion what is a loop') and nothing ever answered it."""
    sess = MicSession([[heard("what is recursion"), audio(10), said("Imagine two mirrors.")],
                       [interrupted(), heard("what is a loop"), audio(10), said("A loop repeats things."), done()]], triggers={1, 3})
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Notes\nbody of the notes"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        speak(ws, 2)
        time.sleep(0.4)                                                  # (the student takes a moment before speaking again)
        speak(ws, 1)                                                     # (frame 3 starts the second utterance)
        first, _ = collect(ws)                                           # question 1 is closed as soon as the student speaks over it
        second, _ = collect(ws)
        ws.send_json({"type": "stop"})
    frames = first + second
    assert len(sess.tool_calls) == 2, "two separate questions, each answered with its own notes"
    assert [f["type"] for f in frames].count("turn_start") == 2 and frames[-1]["type"] == "turn_complete"
    msgs = sdb.get_messages(sdb.list_conversations(EMAIL)[0]["id"])
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["what is recursion", "what is a loop"]


def test_a_late_transcript_after_the_teacher_started_is_a_new_question_not_part_of_the_old_one(client, monkeypatch, fast):
    sess = MicSession([[heard("what is recursion"), audio(10), said("Imagine mirrors.")],
                       [heard("and what is a loop"), audio(10), said("A loop repeats."), done()]], triggers={1, 3})
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# N\nxx"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        speak(ws, 2)
        time.sleep(0.4)                                                  # longer than the late-transcript grace: the teacher is mid-answer
        speak(ws, 1)
        collect(ws)
        ws.send_json({"type": "stop"})
    assert [m["content"] for m in sdb.get_messages(sdb.list_conversations(EMAIL)[0]["id"]) if m["role"] == "user"] == ["what is recursion", "and what is a loop"]


# ------------------------------------------------------------------ a silent or stalled teacher must not freeze the lesson
def test_teacher_never_answers_the_turn_is_closed_and_the_next_question_works(client, monkeypatch, fast):
    sess = FakeSession([[], [audio(10), said("Here is the answer."), done()]])           # first question: Gemini sends nothing useful
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# A\nnotes here"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "first question"})
        f1, _ = collect(ws)
        assert any(f["type"] == "notice" for f in f1), "the student is told to ask again"
        assert f1[-1]["type"] == "turn_complete"
        ws.send_json({"type": "text", "text": "second question"})
        f2, nbytes = collect(ws)
    assert nbytes == 20 and f2[-1]["type"] == "turn_complete"
    assert any(r["action"] == "turn_closed" for r in asyncio.run(state.recent_failures(10)))


def test_teacher_stops_mid_sentence_without_turn_complete(client, monkeypatch, fast):
    sess = FakeSession([[audio(10), said("Let me explain")], [audio(10), said("Second answer."), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# A\nnotes here"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "first"})
        f1, _ = collect(ws)
        assert f1[-1]["type"] == "turn_complete"
        ws.send_json({"type": "text", "text": "second"})
        f2, _ = collect(ws)
    assert f2[-1]["type"] == "turn_complete"


def test_a_stale_turn_complete_of_the_interrupted_answer_does_not_end_the_new_question(client, monkeypatch):
    """Typing while the teacher talks: Gemini sends the old answer's 'turn complete' BEFORE the new answer. It must be ignored."""
    sess = FakeSession([[audio(5), said("one")],
                        [done(), interrupted(), audio(5), said("two"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# A\nnotes here"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "q1"})
        seen = []
        while len(seen) < 1:                                             # wait until the first answer's voice has arrived
            m = ws.receive()
            if m.get("bytes") is not None:
                seen.append(1)
        ws.send_json({"type": "text", "text": "q2"})
        frames, nbytes = collect(ws)
    kinds = [f["type"] for f in frames]
    assert nbytes == 10, "the second answer's voice was played"
    assert kinds[-1] == "turn_complete"


def test_five_questions_in_a_row_all_get_answers(client, monkeypatch):
    sess = FakeSession([[audio(5), said(f"answer {i}"), done()] for i in range(5)])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Notes\nbody text"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        for i in range(5):
            ws.send_json({"type": "text", "text": f"question {i}"})
            frames, nbytes = collect(ws)
            assert nbytes == 10 and frames[-1]["type"] == "turn_complete", i
    assert len(sess.tool_calls) == 5
    assert len(sdb.get_messages(sdb.list_conversations(EMAIL)[0]["id"])) == 10


def test_five_spoken_questions_in_a_row_all_get_answers(client, monkeypatch):
    scripts = [[heard(f"question number {i}"), audio(5), said(f"answer {i}"), done()] for i in range(5)]
    sess = MicSession(scripts, triggers={1, 3, 5, 7, 9})
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Notes\nbody text"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        for i in range(5):
            speak(ws, 2)
            frames, _ = collect(ws)
            assert frames[-1]["type"] == "turn_complete", i
        ws.send_json({"type": "stop"})
    assert len(sess.tool_calls) == 5 and [m["content"] for m in sdb.get_messages(sdb.list_conversations(EMAIL)[0]["id"]) if m["role"] == "user"] == [f"question number {i}" for i in range(5)]


# ------------------------------------------------------------------ dead connections (phones lock, networks switch)
def test_keepalive_ping_and_pong(client, monkeypatch):
    monkeypatch.setattr(config, "LIVE_PING_SECONDS", 0.1)
    install(monkeypatch, FakeSession([]))
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        assert collect(ws, until="ping")[0][-1]["type"] == "ping", "the server keeps the socket alive by itself"
        ws.send_json({"type": "ping"})
        assert collect(ws, until="pong")[0][-1]["type"] == "pong"
        ws.send_json({"type": "stop"})


def test_a_session_that_returns_nothing_forever_is_reconnected_not_spun_on(client, monkeypatch):
    class Empty(FakeSession):
        async def receive(self):
            return
            yield

    monkeypatch.setattr(live, "_connect", lambda cfg: type("C", (), {
        "__aenter__": lambda s: _aw(Empty([])), "__aexit__": lambda s, *a: _aw(False)})())

    async def _aw(v):
        return v

    monkeypatch.setattr(config, "LIVE_RECONNECTS", 1)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        frames, _ = collect(ws, until="error")
    assert [f["type"] for f in frames].count("reconnecting") == 1


def test_lost_browser_after_the_notes_arrived_the_answer_is_still_saved():
    """Phone locks / network switches right after the notes were shown: the question and the notes must be in the history when the student returns."""
    sess = FakeSession([[audio(5), said("voice"), done()]])
    notes = "# Heaps\n" + "".join(f"line {i} of the notes about heaps\n" for i in range(5))
    saved = {}
    with TestClient(app) as c:
        c.post("/api/auth/dev", json={"email": EMAIL})
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(config, "GEMINI_API_KEY", "test-key-not-real")
            mp.setattr(config, "LIVE_START_WAIT", 0)
            state.reset_memory()
            install(mp, sess)
            fake_notes(mp, [notes])
            with c.websocket_connect("/api/live") as ws:
                start(ws)
                ws.receive_json()
                ws.send_json({"type": "text", "text": "Explain heaps"})
                while ws.receive_json()["type"] != "notes_delta":
                    pass
                # leaving the block without a "stop" message = the connection simply vanished
            deadline = time.time() + 5
            while time.time() < deadline:
                convs = sdb.list_conversations(EMAIL)
                msgs = sdb.get_messages(convs[0]["id"]) if convs else []
                if len(msgs) == 2:
                    saved["msgs"] = msgs
                    break
                time.sleep(0.1)
        finally:
            mp.undo()
    assert saved, "the answer was never saved"
    assert saved["msgs"][0]["content"] == "Explain heaps" and saved["msgs"][1]["content"] == notes.strip()


# ------------------------------------------------------------------ a worker died: continue from the Redis checkpoint
def test_notes_written_by_a_worker_that_died_before_saving_are_restored_from_the_checkpoint(client, monkeypatch):
    """The worker died after the model wrote the notes (they are in Redis) but before the answer was saved: the student reconnects and gets them."""
    cid = sdb.create_conversation(EMAIL, "Heaps")
    sdb.add_message(cid, "user", "Explain heaps")                           # the question had been saved before the worker died
    asyncio.run(state.checkpoint_start("dead-worker-turn", {"email": EMAIL, "cid": cid, "question": "Explain heaps", "language": "en", "user_saved": "1"}))
    asyncio.run(state.checkpoint_append("dead-worker-turn", "# Heaps\nA heap is a complete binary tree."))
    install(monkeypatch, FakeSession([]))
    with client.websocket_connect("/api/live") as ws:
        start(ws, conversation_id=cid)
        assert ws.receive_json()["type"] == "ready"
        frames, _ = collect(ws)
        ws.send_json({"type": "stop"})
    ts = [f for f in frames if f["type"] == "turn_start"][0]
    assert ts["resumed"] is True and ts["id"] == "dead-worker-turn"
    assert [f["text"] for f in frames if f["type"] == "notes_delta"] == ["# Heaps\nA heap is a complete binary tree."]
    assert frames[-1]["type"] == "turn_complete"
    msgs = sdb.get_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["content"] == "# Heaps\nA heap is a complete binary tree."
    assert asyncio.run(state.find_open_turn(EMAIL, cid)) is None, "finished: not restored a second time"


def test_a_turn_already_claimed_by_another_worker_is_not_restored_twice(client, monkeypatch):
    cid = sdb.create_conversation(EMAIL, "Heaps")
    asyncio.run(state.checkpoint_start("t-claimed", {"email": EMAIL, "cid": cid, "question": "Explain heaps", "language": "en", "user_saved": "1"}))
    asyncio.run(state.checkpoint_append("t-claimed", "# Heaps\nnotes"))
    assert asyncio.run(state.claim_turn("t-claimed", 60)) is True           # another worker got there first
    install(monkeypatch, FakeSession([]))
    with client.websocket_connect("/api/live") as ws:
        start(ws, conversation_id=cid)
        ws.receive_json()
        ws.send_json({"type": "ping"})
        j = ws.receive_json()
        ws.send_json({"type": "stop"})
    assert j["type"] == "pong", "no replay, no second copy: the next frame is just the answer to our ping"
    assert sdb.count_messages(cid) == 0


def test_another_students_checkpoint_is_never_resumed(client, monkeypatch):
    sdb.upsert_user("other@tuf.edu.pk", "Other", "")
    other = sdb.create_conversation("other@tuf.edu.pk", "Theirs")
    asyncio.run(state.checkpoint_start("t-other", {"email": "other@tuf.edu.pk", "cid": other, "question": "private", "language": "en"}))
    install(monkeypatch, FakeSession([]))
    asyncio.run(state.checkpoint_append("t-other", "# private notes that must never be shown to anyone else"))
    install(monkeypatch, FakeSession([]))
    with client.websocket_connect("/api/live") as ws:
        start(ws, conversation_id=other)
        ws.receive_json()
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        ws.send_json({"type": "stop"})


@pytest.mark.skipif(bool(config.REDIS_URL), reason="Starlette's TestClient cancels the handler's awaits on close; the real-uvicorn e2e test covers Redis")
def test_lesson_lock_is_released_when_the_lesson_ends(client, monkeypatch):
    install(monkeypatch, FakeSession([]))
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        assert asyncio.run(state.lesson_held(EMAIL))
        ws.send_json({"type": "stop"})
    time.sleep(0.2)
    assert not asyncio.run(state.lesson_held(EMAIL))


def test_a_broken_database_is_not_mistaken_for_a_dropped_voice_connection(client, monkeypatch):
    """Real incident: 'database disk image is malformed' made every lesson 'reconnect' forever and the notes never appeared."""
    from sqlalchemy.exc import OperationalError

    async def boom(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(db, "get_messages", boom)
    monkeypatch.setattr(db, "get_conversation", boom)
    monkeypatch.setattr(db, "add_message", boom)
    install(monkeypatch, FakeSession([[audio(5), said("voice"), done()]]))
    fake_notes(monkeypatch, ["# Notes\nstill shown"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "Explain heaps"})
        frames, nbytes = collect(ws)
    kinds = [f["type"] for f in frames]
    assert "reconnecting" not in kinds and "error" not in kinds and kinds[-1] == "turn_complete"
    assert "".join(f["text"] for f in frames if f["type"] == "notes_delta") == "# Notes\nstill shown" and nbytes == 10
    assert any(r["component"] == "db" for r in asyncio.run(state.recent_failures(10)))


def test_generation_complete_without_turn_complete_ends_the_turn_quickly_and_quietly(client, monkeypatch):
    """Real Gemini 3.8 behaviour seen in production logs: it finishes generating but 'turn complete' arrives late or never."""
    from google.genai import types
    monkeypatch.setattr(config, "LIVE_TURN_IDLE_SECONDS", 30)               # the slow idle fallback must NOT be what ends the turn
    monkeypatch.setattr(config, "LIVE_GENERATION_GRACE_SECONDS", 0.2)
    gen_done = types.LiveServerMessage(server_content=types.LiveServerContent(generation_complete=True))
    install(monkeypatch, FakeSession([[audio(5), said("the whole lesson"), gen_done]]))
    fake_notes(monkeypatch, ["# Notes\nbody"])
    t0 = time.time()
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "Explain heaps"})
        frames, _ = collect(ws)
    assert frames[-1]["type"] == "turn_complete" and time.time() - t0 < 5
    assert not any(r.get("action") == "turn_closed" for r in asyncio.run(state.recent_failures(10))), "this is a normal ending, not a failure"
