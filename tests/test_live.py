"""Backend tests for the LUMA Live Teacher (/api/live). A fake Gemini Live session speaks the SDK's REAL message types and the notes writer
is faked, so no API key, network or audio hardware is needed.   Run (inside the backend image):  python -m pytest tests -q
"""
import asyncio
import json
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="luma_test_")
os.environ["DB_PATH"] = os.path.join(_tmp, "luma.db")
os.environ["DEV_LOGIN"] = "1"
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
os.environ["MAX_MESSAGES_PER_USER_PER_DAY"] = "500"

import pytest
from fastapi.testclient import TestClient
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from backend import config, db, live, usage
from backend.llm import NO_NOTES_MARK
from backend.main import app

EMAIL = "tester@tuf.edu.pk"


# ------------------------------------------------------------------ fake Gemini Live
def audio(n=100):
    return types.LiveServerMessage(server_content=types.LiveServerContent(model_turn=types.Content(
        parts=[types.Part(inline_data=types.Blob(data=b"\x01\x02" * n, mime_type="audio/pcm;rate=24000"))])))


def said(t):
    return types.LiveServerMessage(server_content=types.LiveServerContent(output_transcription=types.Transcription(text=t)))


def heard(t):
    return types.LiveServerMessage(server_content=types.LiveServerContent(input_transcription=types.Transcription(text=t)))


def done():
    return types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


class FakeSession:
    """receive() yields ONE model turn per call, like the SDK."""

    def __init__(self, scripts):
        self.scripts, self.q = list(scripts), asyncio.Queue()
        self.sent_text, self.sent_audio, self.audio_end, self.client_content = [], [], 0, []
        self.yielded = 0

    def push(self, msgs):
        for m in msgs:
            self.q.put_nowait(m)

    async def send_realtime_input(self, **kw):
        if "text" in kw:
            self.sent_text.append(kw["text"])
            self.push(self.scripts.pop(0) if self.scripts else [done()])
        if "audio" in kw:
            self.sent_audio.append((kw["audio"].mime_type, len(kw["audio"].data)))
            if len(self.sent_audio) == 1 and self.scripts:       # first mic frame starts a scripted "spoken" turn
                self.push(self.scripts.pop(0))
        if kw.get("audio_stream_end"):
            self.audio_end += 1

    async def send_client_content(self, turns=None, turn_complete=True):
        self.client_content.append((turns, turn_complete))
        if turn_complete and self.scripts:                       # an explicit turn (Stop note / resume) makes the model answer
            self.push(self.scripts.pop(0))

    async def receive(self):
        while True:
            if getattr(self, "drop_when_empty", False) and self.q.empty() and (self.yielded > 0 or getattr(self, "drop_immediately", False)):
                raise RuntimeError("1006 None. abnormal closure [internal]")      # what Gemini really did mid-lesson
            m = await self.q.get()
            self.yielded += 1
            yield m
            if m.server_content and m.server_content.turn_complete:
                return


class Ctx:
    def __init__(self, session):
        self.s = session

    async def __aenter__(self):
        return self.s

    async def __aexit__(self, *a):
        return False


def fake_notes(monkeypatch, pieces, delay=0.0, fail=False):
    """Replace the text-model notes writer (llm.stream_tutor_reply) and record how it was called."""
    calls = []

    async def gen(message, level, language, history, subject="", voice_mode=False, notes_mode=False):
        calls.append(dict(message=message, level=level, language=language, history=history, subject=subject, voice_mode=voice_mode, notes_mode=notes_mode))
        for p in pieces:
            if delay:
                await asyncio.sleep(delay)
            yield p
        if fail:
            raise RuntimeError("provider exploded test-key-not-real")

    monkeypatch.setattr(live, "stream_tutor_reply", gen)
    return calls


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(config, "LIVE_START_WAIT", 0)
    live._active_users.clear()
    usage._usage.clear()
    c = TestClient(app)
    assert c.post("/api/auth/dev", json={"email": EMAIL}).status_code == 200
    return c


def install(monkeypatch, session):
    captured = {}

    def fake_connect(cfg):
        captured["cfg"] = cfg
        return Ctx(session)

    monkeypatch.setattr(live, "_connect", fake_connect)
    return captured


def start(ws, **kw):
    ws.send_json({"type": "start", "level": "undergraduate", "language": "en", "subject": "Computer Science", "gender": "female", **kw})


def collect(ws, until="turn_complete", limit=200):
    """Read frames until a JSON frame of type `until`; returns (json_frames, audio_bytes_total)."""
    frames, nbytes = [], 0
    for _ in range(limit):
        m = ws.receive()
        if m.get("bytes") is not None:
            nbytes += len(m["bytes"])
            continue
        j = json.loads(m["text"])
        frames.append(j)
        if j["type"] == until:
            return frames, nbytes
    raise AssertionError(f"never saw {until}: {frames}")


def notes_text(frames):
    return "".join(f["text"] for f in frames if f["type"] == "notes_delta")


def cid_of(frames):
    return [f for f in frames if f["type"] == "conversation"][0]["id"]


# ------------------------------------------------------------------ config, prompt, voices
def test_live_config_is_voice_only_and_follows_gemini_3_8_rules():
    cfg = live.build_config("undergraduate", "en", "", "Sulafat")
    assert cfg.response_modalities == ["AUDIO"]
    assert cfg.input_audio_transcription is not None and cfg.output_audio_transcription is not None
    assert not cfg.tools, "the voice must have NO tools: it can never see or write the notes"
    assert list(cfg.input_audio_transcription.language_codes) == ["ur-PK", "ar-SA", "fa-IR", "en-US", "de-DE"], "Urdu speech must not be transcribed as Hindi"
    assert cfg.thinking_config is None and not cfg.enable_affective_dialog and cfg.proactivity is None
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Sulafat"
    assert config.LIVE_MODEL == "gemini-3.8-live"


def test_prompt_makes_the_voice_teach_not_read_and_keeps_luma_rules():
    p = live.build_live_prompt("foundation", "en", "Physics")
    for must in ("SCOPE:", "IDENTITY:", "Enterprise Solutions Team", "Physics", "foundation-year", "YOU CANNOT SEE THEM",
                 "do NOT give a textbook summary", "short story", "ONE worked", "trap most students fall into"):
        assert must in p, must
    assert "Never say you were made by Google" in p


def test_urdu_prompt_demands_spoken_bolchaal_urdu_not_bookish_urdu():
    p = live.build_live_prompt("undergraduate", "ur", "Computer Science")
    assert "Urdu" in p and "BOLCHAAL" in p and "not the Urdu of a textbook" in p and "chai, cricket" in p
    assert "مذکورہ" in p, "bookish words must be named as words to avoid"


def test_voices_defaults_and_validation():
    assert len(live.VOICES) == 30
    assert live.pick_voice("Kore", "female") == "Kore"
    assert live.pick_voice("NotAVoice", "female") == config.LIVE_VOICE_FEMALE == "Sulafat"
    assert live.pick_voice(None, "male") == config.LIVE_VOICE_MALE == "Charon"
    assert live.pick_voice("../etc", "female") == "Sulafat", "unknown values must never reach the API"


def test_public_config_exposes_live_flag_and_voices(client):
    j = client.get("/api/config").json()
    assert j["live_enabled"] is True and j["live_max_seconds"] == config.LIVE_MAX_SECONDS
    assert len(j["live_voices"]) == 30 and j["live_default_voices"] == {"female": "Sulafat", "male": "Charon"}


def test_live_requires_login():
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/live"):
            pass


# ------------------------------------------------------------------ the two independent channels
def test_notes_and_voice_are_independent_the_voice_never_sees_the_notes(client, monkeypatch):
    NOTES = ["# Entropy\n", "## Definition\nA measure of disorder $S=k\\ln W$.\n", "## Second law\nIt never decreases."]
    sess = FakeSession([[audio(100), said("Think of a tidy room slowly becoming messy. "), audio(50), said("That drift is the story of entropy."), done()]])
    cap = install(monkeypatch, sess)
    calls = fake_notes(monkeypatch, NOTES)
    with client.websocket_connect("/api/live") as ws:
        start(ws, voice="Kore")
        ready = ws.receive_json()
        assert ready["type"] == "ready" and ready["voice"] == "Kore"
        ws.send_json({"type": "text", "text": "Explain entropy"})
        frames, nbytes = collect(ws)
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "conversation" and "notes_delta" in kinds and "notes_done" in kinds and kinds[-1] == "turn_complete" and kinds.count("turn_complete") == 1
    assert kinds.index("notes_done") < kinds.index("turn_complete"), "turn completes only after BOTH channels are done"
    assert nbytes == 300, "all voice bytes are relayed as binary frames"
    assert notes_text(frames) == "".join(NOTES)
    # the Live model only ever received the student's question: no notes text, no tool traffic
    assert [x.split("\n\n[Teacher note")[0] for x in sess.sent_text] == ["Explain entropy"] and sess.client_content == []
    assert "Teacher note" in sess.sent_text[0] and "notes" not in sess.sent_text[0].split("Teacher note")[0].lower()
    assert not cap["cfg"].tools
    # the notes writer is the normal text-model chain, in notes mode, with the student's settings
    assert calls[0]["notes_mode"] is True and calls[0]["voice_mode"] is False and calls[0]["message"] == "Explain entropy"
    assert calls[0]["level"] == "undergraduate" and calls[0]["language"] == "en" and calls[0]["subject"] == "Computer Science"
    msgs = db.get_messages(cid_of(frames))
    assert msgs[0] == {"role": "user", "content": "Explain entropy"}
    assert msgs[1] == {"role": "assistant", "content": "".join(NOTES).strip()}


def test_notes_wait_for_the_voice_then_appear_together():
    """Text and voice must start together: notes are held until the first audio chunk, then released just before it."""
    class WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, j):
            self.sent.append(("json", j["type"]))

        async def send_bytes(self, b):
            self.sent.append(("bytes", len(b)))

    async def scenario():
        ws = WS()
        lesson = live.Lesson(ws, {"email": EMAIL}, {"level": "undergraduate", "language": "en", "subject": "", "gender": "female", "voice": None, "conversation_id": None})
        t = lesson.turn = live.Turn()
        t.question, t.typed = "q", True
        import time
        t.hold_until = time.time() + 5
        await lesson._emit_notes(t, "# Title\n")
        await lesson._emit_notes(t, "text")
        assert ws.sent == [], "notes must not be shown before the teacher starts talking"
        await lesson._on_audio(b"\x01\x02" * 10)
        await lesson._on_audio(b"\x01\x02" * 10)
        await lesson._emit_notes(t, "more")                        # after the voice began: shown at once
        return ws.sent

    assert asyncio.run(scenario()) == [("json", "notes_delta"), ("bytes", 20), ("bytes", 20), ("json", "notes_delta")]


def test_notes_are_not_held_forever_if_the_voice_is_slow():
    class WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, j):
            self.sent.append(j["type"])

    async def scenario():
        ws = WS()
        lesson = live.Lesson(ws, {"email": EMAIL}, {"level": "undergraduate", "language": "en", "subject": "", "gender": "female", "voice": None, "conversation_id": None})
        t = lesson.turn = live.Turn()
        t.hold_until = 0                                           # the hold window already ran out
        await lesson._emit_notes(t, "x")
        return ws.sent

    assert asyncio.run(scenario()) == ["notes_delta"]


@pytest.mark.parametrize("pieces", [[NO_NOTES_MARK], ["[[LUMA_", "NO_NOTES]]"], ["  " + NO_NOTES_MARK + "\n"]])
def test_small_talk_gets_voice_only_and_the_transcript_is_the_history(client, monkeypatch, pieces):
    sess = FakeSession([[audio(10), said("Hi there, how can I help with your studies today?"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, pieces)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "Hello!"})
        frames, _ = collect(ws)
    kinds = [f["type"] for f in frames]
    assert "notes_none" in kinds and "notes_delta" not in kinds
    assert db.get_messages(cid_of(frames))[1]["content"].startswith("Hi there")


def test_notes_writer_failure_does_not_break_the_lesson_and_leaks_nothing(client, monkeypatch):
    sess = FakeSession([[audio(10), said("Let me explain trees in a friendly way. "), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Trees\n"], fail=True)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "Explain trees"})
        frames, _ = collect(ws)
    assert frames[-1]["type"] == "turn_complete" and "test-key-not-real" not in json.dumps(frames)
    assert db.get_messages(cid_of(frames))[1]["content"].startswith("# Trees")


def test_slow_notes_hold_the_turn_open_until_done(client, monkeypatch):
    sess = FakeSession([[audio(10), said("short voice"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# A\n", "b ", "c ", "d"], delay=0.25)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "q"})
        frames, _ = collect(ws)
    kinds = [f["type"] for f in frames]
    assert kinds.index("notes_done") < kinds.index("turn_complete") and notes_text(frames) == "# A\nb c d"


# ------------------------------------------------------------------ spoken (microphone) questions
def test_spoken_question_starts_the_notes_when_the_voice_starts_and_is_saved(client, monkeypatch):
    sess = FakeSession([[heard("what is "), heard("recursion"), audio(10), said("Imagine two mirrors facing each other."), done()]])
    install(monkeypatch, sess)
    calls = fake_notes(monkeypatch, ["# Recursion\n", "A function that calls itself."])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_bytes(b"\x00\x00" * 1600)                       # one 100 ms microphone frame
        frames, _ = collect(ws)
        ws.send_json({"type": "mic_stop"})
        ws.send_json({"type": "stop"})
    assert sess.sent_audio == [("audio/pcm;rate=16000", 3200)] and sess.audio_end == 1
    assert calls and calls[0]["message"] == "what is recursion" and calls[0]["notes_mode"] is True
    m = db.get_messages(cid_of(frames))
    assert m[0] == {"role": "user", "content": "what is recursion"} and m[1]["content"].startswith("# Recursion")
    assert any(f["type"] == "caption" and f["who"] == "user" for f in frames)


# ------------------------------------------------------------------ Stop, new questions, history
def test_stop_silences_the_voice_keeps_the_notes_and_the_okay_is_not_saved(client, monkeypatch):
    sess = FakeSession([[audio(10), said("Let me explain trees. ")],                        # a long answer, still going...
                        [audio(3), said("Okay."), done()],                                  # ...the reply to the Stop note
                        [audio(10), said("A heap is a tree."), done()]])                    # the next real question
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Trees\n", "Nodes and edges."], delay=0.15)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "Explain trees"})
        seen = []
        while "notes_delta" not in seen:
            m = ws.receive()
            if m.get("text"):
                seen.append(json.loads(m["text"])["type"])
        ws.send_json({"type": "interrupt"})
        frames, nbytes = collect(ws)                       # the first turn completes (notes done, voice cut)
        ws.send_json({"type": "text", "text": "What is a heap?"})
        collect(ws)
    nudge = [c for c in sess.client_content if c[1] is True]
    assert len(nudge) == 1 and "pressed Stop" in nudge[0][0].parts[0].text
    assert nbytes == 0, "the Okay after Stop must not be played"
    m = db.get_messages(db.list_conversations(EMAIL)[0]["id"])
    roles = [(x["role"], x["content"][:14]) for x in m]
    assert ("assistant", "Okay.") not in roles
    assert any(r == "user" and c.startswith("Explain trees") for r, c in roles) and any(c.startswith("What is a heap") for _, c in roles)
    assert any(r == "assistant" and c.startswith("# Trees") for r, c in roles), "the notes of the cut-off answer are kept"


def test_a_new_question_cancels_the_previous_notes(client, monkeypatch):
    sess = FakeSession([[audio(5), said("one"), done()], [audio(5), said("two"), done()]])
    install(monkeypatch, sess)
    started = {"n": 0}

    async def gen(message, level, language, history, subject="", voice_mode=False, notes_mode=False):
        started["n"] += 1
        if message == "slow one":
            for i in range(100):                              # would take 20 s
                await asyncio.sleep(0.2)
                yield f"s{i} "
        else:
            yield "# Fast\nanswer"

    monkeypatch.setattr(live, "stream_tutor_reply", gen)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "slow one"})
        while ws.receive_json()["type"] != "language":       # conversation, then the notes worker announces itself
            pass
        ws.send_json({"type": "text", "text": "fast two"})
        frames, _ = collect(ws)
    assert started["n"] == 2
    roles = [(m["role"], m["content"][:6]) for m in db.get_messages(db.list_conversations(EMAIL)[0]["id"])]
    assert ("assistant", "# Fast") in roles and sum(1 for r, _ in roles if r == "user") == 2


def test_continue_old_chat_reseeds_context_without_a_reply(client, monkeypatch):
    cid = db.create_conversation(EMAIL, "Old chat")
    db.add_message(cid, "user", "What is a B-tree?")
    db.add_message(cid, "assistant", "A balanced search tree used by databases.")
    sess = FakeSession([])
    install(monkeypatch, sess)
    with client.websocket_connect("/api/live") as ws:
        start(ws, conversation_id=cid)
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "stop"})
    turns, turn_complete = sess.client_content[0]
    assert turn_complete is False and "B-tree" in turns.parts[0].text


def test_notes_writer_gets_the_conversation_history_like_the_text_chat(client, monkeypatch):
    cid = db.create_conversation(EMAIL, "Chat")
    db.add_message(cid, "user", "What is a B-tree?")
    db.add_message(cid, "assistant", "A balanced search tree.")
    sess = FakeSession([[audio(5), said("ok"), done()]])
    install(monkeypatch, sess)
    calls = fake_notes(monkeypatch, ["# Nodes\nx"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, conversation_id=cid)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "And how are nodes split?"})
        collect(ws)
    assert [h["content"] for h in calls[0]["history"]] == ["What is a B-tree?", "A balanced search tree."]


def test_cannot_open_someone_elses_conversation(client, monkeypatch):
    db.upsert_user("other@tuf.edu.pk", "Other", "")
    other = db.create_conversation("other@tuf.edu.pk", "Not yours")
    db.add_message(other, "user", "private question")
    sess = FakeSession([])
    install(monkeypatch, sess)
    with client.websocket_connect("/api/live") as ws:
        start(ws, conversation_id=other)
        ws.receive_json()
        ws.send_json({"type": "stop"})
    assert sess.client_content == [], "must not leak another student's chat into the model context"


# ------------------------------------------------------------------ validation, limits, sessions
@pytest.mark.parametrize("bad", [{"level": "wizard"}, {"language": "xx"}])
def test_rejects_invalid_start(client, monkeypatch, bad):
    install(monkeypatch, FakeSession([]))
    with client.websocket_connect("/api/live") as ws:
        start(ws, **bad)
        assert ws.receive_json()["type"] == "error"


def test_rejects_oversize_and_empty_text(client, monkeypatch):
    install(monkeypatch, FakeSession([]))
    fake_notes(monkeypatch, [])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "x" * 4001})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "text", "text": "   "})
        assert ws.receive_json()["type"] == "error"


def test_daily_limit_applies(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_MESSAGES_PER_USER_PER_DAY", 1)
    install(monkeypatch, FakeSession([[audio(2), said("a"), done()], [done()]]))
    fake_notes(monkeypatch, ["# A\nx"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "first"})
        collect(ws)
        ws.send_json({"type": "text", "text": "second"})
        assert collect(ws, until="limit")[0][-1]["type"] == "limit"


def test_one_lesson_per_student(client, monkeypatch):
    install(monkeypatch, FakeSession([]))
    with client.websocket_connect("/api/live") as ws1:
        start(ws1)
        assert ws1.receive_json()["type"] == "ready"
        c2 = TestClient(app)
        c2.post("/api/auth/dev", json={"email": EMAIL})
        with c2.websocket_connect("/api/live") as ws2:
            start(ws2)
            j = ws2.receive_json()
            assert j["type"] == "error" and "already" in j["message"]
        ws1.send_json({"type": "stop"})


def test_restarting_right_after_ending_waits_for_the_old_session(client, monkeypatch):
    """Real bug: End -> Live again said 'already open in another tab' because the old session was still closing."""
    import threading
    install(monkeypatch, FakeSession([]))
    live._active_users.add(EMAIL)
    threading.Timer(0.6, lambda: live._active_users.discard(EMAIL)).start()
    monkeypatch.setattr(config, "LIVE_START_WAIT", 4)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "stop"})


def test_not_configured_without_key(client, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with client.websocket_connect("/api/live") as ws:
        j = ws.receive_json()
        assert j["type"] == "error" and "not configured" in j["message"]


def test_errors_never_leak_details_or_key(client, monkeypatch):
    class Boom:
        async def __aenter__(self):
            raise RuntimeError("secret detail test-key-not-real")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(live, "_connect", lambda cfg: Boom())
    monkeypatch.setattr(config, "LIVE_RECONNECTS", 0)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        j = ws.receive_json()
    assert j["type"] == "error" and "test-key-not-real" not in str(j) and "secret detail" not in str(j)


# ------------------------------------------------------------------ reconnect
def test_reconnects_mid_answer_and_the_teacher_carries_on(client, monkeypatch):
    """Real failure seen with Gemini: the session died with 1006 mid-lesson. The student must not lose the lesson."""
    first = FakeSession([[audio(30), said("Plants are like tiny solar farms. ")]])
    first.drop_when_empty = True
    second = FakeSession([[audio(20), said("So the sugar gets built in the Calvin cycle. "), done()]])
    sessions = [first, second]
    monkeypatch.setattr(live, "_connect", lambda cfg: Ctx(sessions.pop(0)))
    monkeypatch.setattr(config, "LIVE_RECONNECTS", 2)
    fake_notes(monkeypatch, ["# Photosynthesis\n", "Light reactions."])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "text", "text": "Explain photosynthesis"})
        frames, nbytes = collect(ws)
    kinds = [f["type"] for f in frames]
    assert "reconnecting" in kinds and [f for f in frames if f["type"] == "ready"][0]["resumed"] is True, kinds
    assert nbytes == 100, "voice from before AND after the reconnect reaches the student"
    turns, turn_complete = second.client_content[-1]
    txt = turns.parts[0].text
    assert turn_complete is True and "Explain photosynthesis" in txt and "solar farms" in txt and "carry on" in txt
    m = db.get_messages(cid_of(frames))
    assert len(m) == 2 and m[1]["content"].startswith("# Photosynthesis"), "one clean user/assistant pair"


def test_gives_up_after_the_reconnect_budget(client, monkeypatch):
    def boom_session():
        x = FakeSession([])
        x.drop_when_empty = x.drop_immediately = True
        return x

    monkeypatch.setattr(live, "_connect", lambda cfg: Ctx(boom_session()))
    monkeypatch.setattr(config, "LIVE_RECONNECTS", 1)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        seen = []
        for _ in range(6):
            j = ws.receive_json()
            seen.append(j["type"])
            if j["type"] == "error":
                break
    assert seen.count("reconnecting") == 1 and seen[-1] == "error"


def test_existing_endpoints_unchanged(client):
    assert client.get("/api/auth/me").json()["email"] == EMAIL
    assert "levels" in client.get("/api/config").json()
    assert client.get("/api/conversations").status_code == 200
    assert client.post("/api/chat", json={"message": ""}).status_code == 400


def test_prompt_demands_a_real_lesson_not_a_30_second_answer():
    p = live.build_live_prompt("undergraduate", "en")
    assert "at least 250 words" in p and "ONE complete worked example" in p and "common mistake" in p


def test_teaching_note_is_sent_to_the_voice_but_never_saved(client, monkeypatch):
    sess = FakeSession([[audio(5), said("hi"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Mutex\nx"])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "What is a mutex?"})
        frames, _ = collect(ws)
    assert "two to three minutes" in sess.sent_text[0]
    assert db.get_messages(cid_of(frames))[0]["content"] == "What is a mutex?"


def test_teaching_note_is_in_the_students_language_so_urdu_stays_in_urdu_script():
    import re
    for lang in ("ur", "ar", "fa"):
        note = live.teach_note(lang)
        assert re.search(r"[\u0600-\u06FF]", note) and "Teacher note" not in note, lang
    p = live.build_live_prompt("undergraduate", "ur")
    assert "NEVER Roman Urdu" in p and "URDU SCRIPT" in p
    assert live.teach_note("zz") == live.teach_note("en")


# ------------------------------------------------------------------ voice preview + cache flush
def test_preview_returns_a_playable_wav_in_the_chosen_voice(client, monkeypatch):
    sess = FakeSession([[audio(500), audio(500), done()]])
    cap = install(monkeypatch, sess)
    r = client.post("/api/live/preview", json={"voice": "Kore", "language": "ur"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    b = r.content
    assert b[:4] == b"RIFF" and b[8:16] == b"WAVEfmt " and len(b) == 44 + 2000
    assert cap["cfg"].speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    assert sess.sent_text[0].startswith("\u0641\u0631\u0636") or "\u0644\u0627\u0626\u0628\u0631\u06cc\u0631\u06cc" in sess.sent_text[0], "Urdu sample text"


def test_preview_validation_auth_and_rate_limit(client, monkeypatch):
    install(monkeypatch, FakeSession([[audio(10), done()]] * 20))
    assert client.post("/api/live/preview", json={"voice": "Nope", "language": "en"}).status_code == 400
    assert client.post("/api/live/preview", json={"voice": "Kore", "language": "xx"}).status_code == 400
    assert TestClient(app).post("/api/live/preview", json={"voice": "Kore", "language": "en"}).status_code == 401
    live._preview_log.clear()
    monkeypatch.setattr(live, "PREVIEW_PER_HOUR", 2)
    codes = [client.post("/api/live/preview", json={"voice": "Kore", "language": "en"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_preview_failure_is_generic(client, monkeypatch):
    class Boom:
        async def __aenter__(self):
            raise RuntimeError("secret test-key-not-real")

        async def __aexit__(self, *a):
            return False

    live._preview_log.clear()
    monkeypatch.setattr(live, "_connect", lambda cfg: Boom())
    r = client.post("/api/live/preview", json={"voice": "Kore", "language": "en"})
    assert r.status_code == 502 and "test-key-not-real" not in r.text


def test_config_never_asks_the_browser_to_clear_its_cache():
    """Real bug: Clear-Site-Data on /api/config made phones and in-app browsers that refuse the cookie reload the page forever (blank screen)."""
    c = TestClient(app)
    for _ in range(3):
        r = c.get("/api/config")
        assert "clear-site-data" not in r.headers and "luma_cv" not in r.cookies


# ------------------------------------------------------------------ language follows the question (not only the selector)
URDU_Q = "کمپیوٹر میں ریکرشن کیا ہوتی ہے؟ آسان انداز میں سمجھائیں۔"


@pytest.mark.parametrize("text,default,expected", [
    (URDU_Q, "en", "ur"),                                    # Urdu question, selector says English
    ("ریکرشن کیا ہے", "en", "ur"),
    ("What is recursion?", "en", "en"),
    ("What is recursion?", "ur", "ur"),                      # Latin script keeps the selected language (as the normal chat does)
    ("ما هي العودية في البرمجة؟", "en", "ar"),               # Arabic
    ("بازگشت در برنامه نویسی چیست؟ چگونه کار می کند", "en", "fa"),
    ("بازگشت در برنامه نویسی چیست؟ چگونه کار می کند", "ur", "ur"),
    ("", "ur", "ur"),
    ("Erkläre mir bitte, wie Rekursion funktioniert.", "en", "de"),      # German question, selector says English
    ("Was ist der Unterschied zwischen einem Array und einer Liste?", "en", "de"),
    ("What is the difference between an array and a list?", "de", "en"), # English question, selector says German
    ("Rekursion", "de", "de"),                                           # too short to tell: keeps the selector
    ("Rekursion", "en", "en"),
    ("Was ist Rekursion?", "ur", "ur"),                                  # other selectors are left alone (Roman Urdu etc.)
])
def test_detect_language(text, default, expected):
    assert live.detect_language(text, default) == expected


def test_asking_in_urdu_while_the_selector_says_english_gets_urdu_everywhere(client, monkeypatch):
    sess = FakeSession([[audio(10), said("آئینے والی مثال سوچیں۔"), done()]])
    install(monkeypatch, sess)
    calls = fake_notes(monkeypatch, ["# ریکرشن\nایک فنکشن جو خود کو کال کرتا ہے۔"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ws.receive_json()
        ws.send_json({"type": "text", "text": URDU_Q})
        frames, _ = collect(ws)
    assert [f for f in frames if f["type"] == "language"][0]["language"] == "ur", "the page is told to switch to Urdu"
    assert calls[0]["language"] == "ur", "the notes are written in Urdu"
    assert "اردو رسم الخط" in sess.sent_text[0], "the voice gets its teaching note in Urdu"
    assert "Teacher note" not in sess.sent_text[0]


def test_asking_in_english_while_the_selector_says_english_stays_english(client, monkeypatch):
    sess = FakeSession([[audio(10), said("hi"), done()]])
    install(monkeypatch, sess)
    calls = fake_notes(monkeypatch, ["# A\nx"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ws.receive_json()
        ws.send_json({"type": "text", "text": "What is recursion?"})
        collect(ws)
    assert calls[0]["language"] == "en" and "Teacher note" in sess.sent_text[0]


def test_spoken_urdu_question_gets_urdu_notes(client, monkeypatch):
    sess = FakeSession([[heard("ریکرشن کیا ہوتی ہے"), audio(10), said("سوچیں۔"), done()]])
    install(monkeypatch, sess)
    calls = fake_notes(monkeypatch, ["# ریکرشن\nx"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ws.receive_json()
        ws.send_bytes(b"\x00\x00" * 1600)
        frames, _ = collect(ws)
    assert calls[0]["language"] == "ur" and any(f["type"] == "language" and f["language"] == "ur" for f in frames)


def test_prompt_follows_the_language_of_the_question_and_knows_every_style():
    p = live.build_live_prompt("undergraduate", "en")
    assert "language the student used in their latest message" in p and "Never answer in English to a question asked in Urdu" in p
    assert "[Urdu]" in p and "BOLCHAAL" in p and "[English]" in p and "[Arabic]" in p and "[Persian]" in p
    assert "speak strictly in" not in p


@pytest.mark.parametrize("text", ["इसको हिंदी में बताएं। इसको आप उर्दू में बताएं।", "मुझे उर्दू में बताइए, उर्दू में।", "रिकर्शन क्या होता है"])
@pytest.mark.parametrize("default", ["en", "ur", "ar"])
def test_hindi_script_from_the_speech_recognizer_is_treated_as_urdu(text, default):
    """Real bug: a spoken Urdu question was transcribed in Devanagari, detected as English, and answered with English notes."""
    assert live.detect_language(text, default) == "ur"


def test_prompt_insists_on_urdu_not_hindi():
    p = live.build_live_prompt("undergraduate", "en")
    assert "HINDI VS URDU" in p and "ALWAYS answer in URDU" in p and "never Devanagari" in p and "NEVER Hindi/Devanagari" in p


def test_spoken_hindi_script_question_gets_urdu_notes(client, monkeypatch):
    sess = FakeSession([[heard("इसको आप उर्दू में बताएं"), audio(10), said("ٹھیک ہے۔"), done()]])
    install(monkeypatch, sess)
    calls = fake_notes(monkeypatch, ["# مارکیٹنگ\nx"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ws.receive_json()
        ws.send_bytes(b"\x00\x00" * 1600)
        frames, _ = collect(ws)
    assert calls[0]["language"] == "ur" and any(f["type"] == "language" and f["language"] == "ur" for f in frames)


def test_microphone_noise_does_not_open_a_turn(client, monkeypatch):
    """Real bug: room noise was transcribed as 'A' and showed up as a user message with an empty answer."""
    sess = FakeSession([[heard("A"), audio(5), said("hello"), done()]])
    install(monkeypatch, sess)
    calls = fake_notes(monkeypatch, ["# x\ny"])
    convs_before = len(db.list_conversations(EMAIL))
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_bytes(b"\x00\x00" * 1600)
        ws.receive()                                         # the voice bytes: no user caption, no notes, no history
        usage_before = usage._usage.get(EMAIL)
        ws.send_json({"type": "stop"})
    assert calls == [] and usage_before is None
    assert len(db.list_conversations(EMAIL)) == convs_before, "no conversation may be created for noise"
