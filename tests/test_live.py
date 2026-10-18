"""Backend tests for the LUMA Live Teacher (/api/live). A fake Gemini Live session speaks the SDK's REAL message types (including the
show_notes tool call), so no API key, network or audio hardware is needed.   Run (inside the backend image):  python -m pytest tests -q
"""
import asyncio
import json
import os
import time


import pytest
from fastapi.testclient import TestClient
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from sdb import reset_database, sdb  # noqa: F401
from backend import config, db, judge, live, llm, state, usage
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


NOTES = {"md": None}        # when set, every scripted answer is preceded by the model WRITING these notes through the show_notes tool


def tool_call(md, call_id="call-1", name="show_notes"):
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[types.FunctionCall(id=call_id, name=name, args={"markdown": md})]))


class FakeSession:
    """receive() yields ONE model turn per call, like the SDK. A study answer is two model turns, exactly as on the real service
    (checked live): turn 1 = the show_notes tool call + "turn complete"; after our tool response, turn 2 = the spoken elaboration."""

    def __init__(self, scripts):
        self.scripts, self.q = list(scripts), asyncio.Queue()
        self.sent_text, self.sent_audio, self.audio_end, self.client_content = [], [], 0, []
        self.tool_responses, self.tool_calls, self.pending_voice = [], [], None
        self.yielded = 0

    def push(self, msgs):
        for m in msgs:
            self.q.put_nowait(m)

    def _start_answer(self, tool=True):
        script = self.scripts.pop(0) if self.scripts else [done()]
        md = NOTES["md"]
        if tool and md and any(m.server_content and m.server_content.model_turn for m in script):
            self.tool_calls.append(md)
            k = 0                                                # what the student said (and any interruption) comes BEFORE the model reacts
            while k < len(script) and script[k].server_content and (script[k].server_content.interrupted or script[k].server_content.input_transcription):
                k += 1
            self.push(script[:k])
            self.pending_voice = list(script[k:])
            self.push([tool_call(md), done()])
        else:
            self.push(script)

    async def send_realtime_input(self, **kw):
        if "text" in kw:
            self.sent_text.append(kw["text"])
            self._start_answer()
        if "audio" in kw:
            self.sent_audio.append((kw["audio"].mime_type, len(kw["audio"].data)))
            if len(self.sent_audio) == 1 and self.scripts:       # first mic frame starts a scripted "spoken" turn
                self._start_answer()
        if kw.get("audio_stream_end"):
            self.audio_end += 1

    async def send_client_content(self, turns=None, turn_complete=True):
        self.client_content.append((turns, turn_complete))
        if turn_complete and self.scripts:                       # an explicit turn (Stop note / resume / language fix) makes the model answer
            self._start_answer(tool=False)

    async def send_tool_response(self, function_responses=None, **kw):
        self.tool_responses.append(function_responses)
        if self.pending_voice is not None:                       # the tool returned: the model now speaks
            self.push(self.pending_voice)
            self.pending_voice = None

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
    """The model WRITES these notes (the show_notes tool call) before it speaks. `[NO_NOTES_MARK]` = small talk: no notes, voice only."""
    md = "".join(pieces)
    monkeypatch.setitem(NOTES, "md", None if md.strip().startswith("[[LUMA_") or not md.strip() else md)
    return NOTES


async def _judge_ok(prompt):
    return '{"language_ok": true, "quality_ok": true, "issues": [], "instruction": ""}'


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(config, "LIVE_START_WAIT", 0)
    monkeypatch.setattr(config, "LIVE_TICK_SECONDS", 0.05)
    monkeypatch.setattr(config, "LIVE_RECONNECT_DELAY", 0.01)
    monkeypatch.setattr(config, "JUDGE_HEAD_CHARS", 20)
    monkeypatch.setattr(judge, "_call_judge_model", _judge_ok)       # never call a real model from tests
    reset_database()
    state.reset_memory()
    if config.REDIS_URL:                                  # running the suite against a real Redis: start from an empty one
        import redis as _redis
        _redis.Redis.from_url(config.REDIS_URL).flushdb()
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
    names = [fd.name for t in cfg.tools for fd in t.function_declarations]
    assert names == ["show_notes"], "ONE model does everything: its only tool is the one that writes its own notes"
    assert list(cfg.input_audio_transcription.language_codes) == ["ur-PK", "ar-SA", "fa-IR", "en-US", "de-DE"], "Urdu speech must not be transcribed as Hindi"
    assert cfg.thinking_config is None and not cfg.enable_affective_dialog and cfg.proactivity is None
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Sulafat"
    assert config.LIVE_MODEL == "gemini-3.8-live"


def test_prompt_makes_the_model_write_then_elaborate_and_keeps_luma_rules():
    p = live.build_live_prompt("foundation", "en", "Physics")
    for must in ("SCOPE:", "IDENTITY:", "Enterprise Solutions Team", "Physics", "foundation-year", "STEP 1 - WRITE", "show_notes", "STEP 2 - ELABORATE ALOUD",
                 "Do NOT read the notes out", "most important or hardest part", "short story", "trap most students fall into", "NO NOTES FOR"):
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
def test_one_model_writes_the_notes_then_the_voice_elaborates_on_them(client, monkeypatch):
    NOTES_MD = ["# Entropy\n", "## Definition\nA measure of disorder $S=k\\ln W$.\n", "## Second law\nIt never decreases."]
    sess = FakeSession([[audio(100), said("Think of a tidy room slowly becoming messy. "), audio(50), said("That drift is the story of entropy."), done()]])
    cap = install(monkeypatch, sess)
    fake_notes(monkeypatch, NOTES_MD)
    with client.websocket_connect("/api/live") as ws:
        start(ws, voice="Kore")
        ready = ws.receive_json()
        assert ready["type"] == "ready" and ready["voice"] == "Kore"
        ws.send_json({"type": "text", "text": "Explain entropy"})
        frames, nbytes = collect(ws)
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "conversation" and kinds[-1] == "turn_complete" and kinds.count("turn_complete") == 1
    assert "notes_delta" in kinds and "notes_done" in kinds and "notes_none" not in kinds
    assert notes_text(frames) == "".join(NOTES_MD), "the text on screen is exactly what the model wrote with its own tool call"
    assert nbytes == 300, "all voice bytes are relayed as binary frames"
    # the SAME session produced both: one tool call, answered with the instruction to elaborate (not to read the notes)
    assert sess.tool_calls == ["".join(NOTES_MD)] and len(sess.tool_responses) == 1
    resp = sess.tool_responses[0][0]
    assert resp.name == "show_notes" and "NOW SPEAK" in resp.response["result"] and "do not read the notes" in resp.response["result"]
    assert "most important or hardest part" in resp.response["result"] and "English" in resp.response["result"]
    assert "first call show_notes" in sess.sent_text[0] and sess.sent_text[0].startswith("Explain entropy")
    assert [t.function_declarations[0].name for t in cap["cfg"].tools] == ["show_notes"]
    msgs = sdb.get_messages(cid_of(frames))
    assert msgs[0] == {"role": "user", "content": "Explain entropy"}
    assert msgs[1] == {"role": "assistant", "content": "".join(NOTES_MD).strip()}


def test_the_notes_reach_the_screen_before_the_voice_starts(client, monkeypatch):
    """Tool call first, then speech: the written text is on screen when the first audio arrives (never the other way round)."""
    sess = FakeSession([[audio(10), said("Spoken part."), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Heaps\nA heap is a tree with an ordering property that makes the minimum easy to find."])
    order = []
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "Explain heaps"})
        while True:
            m = ws.receive()
            if m.get("bytes") is not None:
                order.append("audio")
                continue
            j = json.loads(m["text"])
            if j["type"] in ("notes_delta", "turn_complete"):
                order.append(j["type"])
            if j["type"] == "turn_complete":
                break
    assert order[0] == "notes_delta" and "audio" in order and order.index("notes_delta") < order.index("audio")


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
    assert sdb.get_messages(cid_of(frames))[1]["content"].startswith("Hi there")


def test_the_model_that_forgets_the_tool_still_gets_a_complete_screen_answer(client, monkeypatch):
    """No show_notes call (the model just talked): the spoken answer is shown as the text and saved, nothing is left empty."""
    sess = FakeSession([[audio(10), said("Let me explain trees in a friendly way. A tree has nodes and edges."), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, [])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "Explain trees"})
        frames, _ = collect(ws)
    kinds = [f["type"] for f in frames]
    assert "notes_none" in kinds and kinds[-1] == "turn_complete" and "test-key-not-real" not in json.dumps(frames)
    assert sdb.get_messages(cid_of(frames))[1]["content"].startswith("Let me explain trees")


class RetrySession(FakeSession):
    """After a REJECTED tool call the model fixes its notes and calls the tool again, as the real model does with an error response."""

    def __init__(self, scripts, second_notes):
        super().__init__(scripts)
        self.second_notes = second_notes

    async def send_tool_response(self, function_responses=None, **kw):
        self.tool_responses.append(function_responses)
        if "error" in function_responses[0].response:
            self.push([tool_call(self.second_notes, "call-2"), done()])
        elif self.pending_voice is not None:
            self.push(self.pending_voice)
            self.pending_voice = None


def test_notes_in_the_wrong_language_are_rejected_to_the_model_and_rewritten_before_the_student_sees_them(client, monkeypatch):
    good_urdu = "# ریکرشن\n\n## تعریف\nریکرشن ایک ایسا طریقہ ہے جس میں فنکشن اپنے آپ کو چھوٹے مسئلے پر دوبارہ کال کرتا ہے، یہاں تک کہ base case آ جائے۔"
    sess = RetrySession([[audio(10), said("آئینے کی مثال۔"), done()]], good_urdu)
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Recursion\nRecursion is a function that calls itself until a base case stops it, and every call waits for the next one to finish."])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="ur")
        ws.receive_json()
        ws.send_json({"type": "text", "text": "What is recursion?"})
        frames, _ = collect(ws)
    assert notes_text(frames) == good_urdu, "the English attempt never reached the screen"
    first = sess.tool_responses[0][0].response
    assert "error" in first and "Urdu" in first["error"] and "show_notes again" in first["error"]
    assert "NOW SPEAK" in sess.tool_responses[1][0].response["result"]
    assert sdb.get_messages(cid_of(frames))[1]["content"] == good_urdu


def test_a_second_tool_call_in_the_same_turn_changes_nothing(client, monkeypatch):
    class Twice(FakeSession):
        async def send_tool_response(self, function_responses=None, **kw):
            self.tool_responses.append(function_responses)
            if len(self.tool_responses) == 1:
                self.push([tool_call("# Other notes\nthat must not replace the first ones, however long they are, because one answer has one set of notes.", "call-2"), done()])
            elif self.pending_voice is not None:
                self.push(self.pending_voice)
                self.pending_voice = None

    sess = Twice([[audio(10), said("voice"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# First notes\nThese are the notes that stay on the screen for this answer, complete and correct."])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_json({"type": "text", "text": "q"})
        frames, _ = collect(ws)
    assert notes_text(frames).startswith("# First notes") and "Other notes" not in notes_text(frames)
    assert "already on the screen" in sess.tool_responses[1][0].response["result"]


# ------------------------------------------------------------------ spoken (microphone) questions
def test_spoken_question_gets_notes_and_voice_from_the_same_model_and_is_saved(client, monkeypatch):
    sess = FakeSession([[heard("what is "), heard("recursion"), audio(10), said("Imagine two mirrors facing each other."), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Recursion\n", "A function that calls itself until a base case stops it, one call at a time."])
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_bytes(b"\x00\x00" * 1600)                       # one 100 ms microphone frame
        frames, _ = collect(ws)
        ws.send_json({"type": "mic_stop"})
        ws.send_json({"type": "stop"})
    assert sess.sent_audio == [("audio/pcm;rate=16000", 3200)] and sess.audio_end == 1
    assert len(sess.tool_calls) == 1
    m = sdb.get_messages(cid_of(frames))
    assert m[0] == {"role": "user", "content": "what is recursion"} and m[1]["content"].startswith("# Recursion")
    assert any(f["type"] == "caption" and f["who"] == "user" for f in frames) and "notes_delta" in [f["type"] for f in frames]


# ------------------------------------------------------------------ Stop, new questions, history
def test_stop_silences_the_voice_keeps_the_notes_and_the_okay_is_not_saved(client, monkeypatch):
    sess = FakeSession([[audio(10), said("Let me explain trees. ")],                        # a long answer, still going...
                        [audio(3), said("Okay."), done()],                                  # ...the reply to the Stop note
                        [audio(10), said("A heap is a tree."), done()]])                    # the next real question
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Trees\n", "Nodes and edges, the basic parts of every tree structure in computer science."])
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
    assert nbytes <= 20, "only the first answer's own audio may have played: the Okay after Stop must not be played"
    m = sdb.get_messages(sdb.list_conversations(EMAIL)[0]["id"])
    roles = [(x["role"], x["content"][:14]) for x in m]
    assert ("assistant", "Okay.") not in roles
    assert any(r == "user" and c.startswith("Explain trees") for r, c in roles) and any(c.startswith("What is a heap") for _, c in roles)
    assert any(r == "assistant" and c.startswith("# Trees") for r, c in roles), "the notes of the cut-off answer are kept"


def test_continue_old_chat_reseeds_context_without_a_reply(client, monkeypatch):
    cid = sdb.create_conversation(EMAIL, "Old chat")
    sdb.add_message(cid, "user", "What is a B-tree?")
    sdb.add_message(cid, "assistant", "A balanced search tree used by databases.")
    sess = FakeSession([])
    install(monkeypatch, sess)
    with client.websocket_connect("/api/live") as ws:
        start(ws, conversation_id=cid)
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "stop"})
    turns, turn_complete = sess.client_content[0]
    assert turn_complete is False and "B-tree" in turns.parts[0].text


def test_cannot_open_someone_elses_conversation(client, monkeypatch):
    sdb.upsert_user("other@tuf.edu.pk", "Other", "")
    other = sdb.create_conversation("other@tuf.edu.pk", "Not yours")
    sdb.add_message(other, "user", "private question")
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
        j = ws.receive_json()
        assert j["type"] == "notice" and j["code"] == "MESSAGE_TOO_LONG" and j["retryable"] is False
        ws.send_json({"type": "text", "text": "   "})
        j = ws.receive_json()
        assert j["type"] == "notice" and j["code"] == "BAD_REQUEST"


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
    assert asyncio.run(state.acquire_lesson(EMAIL, 99)) == "ok"
    threading.Timer(0.6, lambda: asyncio.run(state.release_lesson(EMAIL))).start()
    monkeypatch.setattr(config, "LIVE_START_WAIT", 4)
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "stop"})


def test_not_configured_without_key(client, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with client.websocket_connect("/api/live") as ws:
        j = ws.receive_json()
        assert j["type"] == "error" and j["code"] == "LIVE_NOT_CONFIGURED"


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
    m = sdb.get_messages(cid_of(frames))
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
    assert "at least 250 words" in p and "worked example" in p and "common mistake" in p and "finish every sentence" not in p.split("LENGTH")[0]
    assert "never cut off" in p


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
    assert sdb.get_messages(cid_of(frames))[0]["content"] == "What is a mutex?"


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


# ------------------------------------------------------------------ the language policy lives in tests/test_language_flow.py
def test_prompt_makes_the_selected_language_win_and_knows_every_style():
    p = live.build_live_prompt("undergraduate", "en")
    assert "LANGUAGE POLICY" in p and "chose English in the interface" in p and "does NOT depend on the language the student asks in" in p
    assert "explicitly asks" in p and "[Urdu]" in p and "BOLCHAAL" in p and "[English]" in p and "[Arabic]" in p and "[Persian]" in p
    assert "[German]" in p and "Language" in p
    assert "chose Urdu in the interface" in live.build_live_prompt("undergraduate", "ur")


def test_prompt_insists_on_urdu_not_hindi():
    p = live.build_live_prompt("undergraduate", "en")
    assert "HINDI VS URDU" in p and "never Devanagari" in p and "NEVER Hindi/Devanagari" in p


def test_microphone_noise_does_not_open_a_turn(client, monkeypatch):
    """Real bug: room noise was transcribed as 'A' and showed up as a user message with an empty answer."""
    sess = FakeSession([[heard("A"), audio(5), said("hello"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, [])                              # the model just says "hello" to the noise: no study question, no notes
    convs_before = len(sdb.list_conversations(EMAIL))
    with client.websocket_connect("/api/live") as ws:
        start(ws)
        ws.receive_json()
        ws.send_bytes(b"\x00\x00" * 1600)
        ws.receive()                                         # the voice bytes: no user caption, no notes, no history
        probe = asyncio.run(state.incr_daily(EMAIL, 100))           # remaining == 99 means this was the very first message counted
        ws.send_json({"type": "stop"})
    assert sess.tool_calls == [] and probe == (True, 99), "noise must not be counted as a question"
    assert len(sdb.list_conversations(EMAIL)) == convs_before, "no conversation may be created for noise"


