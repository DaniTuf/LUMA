"""End-to-end test server: the REAL LUMA backend and frontend, with Gemini Live, the text models and the judge replaced by scripted fakes
that behave like the real services (language-aware voice + notes, a continuous 'microphone' that gets answered, silent teacher on demand,
connection drops on demand). Used by the Playwright tests in tests/e2e. Run:  python tests/e2e/server.py 8765
"""
import asyncio
import math
import os
import struct
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.update(DATABASE_URL="sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(prefix="luma_e2e_"), "luma.db").replace(chr(92), "/"), DEV_LOGIN="1", GEMINI_API_KEY="e2e-fake-key",
                  MAX_MESSAGES_PER_USER_PER_DAY="1000", WEB_CONCURRENCY="1", SECRET_KEY="e2e-secret-key-0123456789abcdef0123456789abcdef", REDIS_URL="", LIVE_FIRST_AUDIO_SECONDS="4", LIVE_TURN_IDLE_SECONDS="6",
                  LIVE_PING_SECONDS="2", LIVE_TICK_SECONDS="0.25", LIVE_START_WAIT="2", JUDGE_HEAD_CHARS="30")

import fakeredis  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from google.genai import types  # noqa: E402
from starlette.routing import Mount  # noqa: E402

from backend import config, judge, live, llm, state  # noqa: E402
from backend.main import app  # noqa: E402

VENDOR = os.environ.get("E2E_VENDOR", os.path.join(os.path.dirname(__file__), ".vendor"))

NOTES = {
    "en": "# Recursion\n\n## Definition\nA function that solves a problem by calling itself on a smaller input.\n\n## Example\n`fact(4) = 4 * fact(3)`\n\n## Summary\nEvery recursion needs a base case.",
    "ur": "# ریکرشن\n\n## تعریف\nایسا فنکشن جو چھوٹے مسئلے کے لیے خود کو کال کرتا ہے۔\n\n## مثال\n`fact(4) = 4 * fact(3)`\n\n## خلاصہ\nہر ریکرشن میں ایک base case ضروری ہے۔",
    "de": "# Rekursion\n\n## Definition\nEine Funktion, die sich selbst mit einer kleineren Eingabe aufruft.\n\n## Zusammenfassung\nJede Rekursion braucht einen Basisfall.",
    "ar": "# التكرار الذاتي\n\n## التعريف\nدالة تستدعي نفسها لحل مسألة أصغر.\n\n## الخلاصة\nيحتاج كل تكرار إلى حالة أساسية.",
    "fa": "# بازگشت\n\n## تعریف\nتابعی که خودش را برای مسئله کوچک‌تر صدا می‌زند.\n\n## خلاصه\nهر بازگشت به حالت پایه نیاز دارد.",
}
SAID = {
    "en": "Imagine two mirrors facing each other, every reflection holds a smaller reflection. That is recursion, and it always needs a base case to stop.",
    "ur": "فرض کریں دو آئینے آمنے سامنے ہیں، ہر عکس میں ایک چھوٹا عکس ہے۔ یہی ریکرشن ہے اور اسے رکنے کے لیے ایک base case چاہیے۔",
    "de": "Stell dir zwei Spiegel vor, die sich gegenüberstehen, jedes Spiegelbild enthält ein kleineres Spiegelbild. Das ist Rekursion, und sie braucht einen Basisfall.",
    "ar": "تخيّل مرآتين متقابلتين، وكل انعكاس يحتوي على انعكاس أصغر. هذا هو التكرار الذاتي وهو يحتاج إلى حالة أساسية ليتوقف.",
    "fa": "تصور کنید دو آینه روبه‌روی هم هستند و هر تصویر شامل تصویری کوچک‌تر است. این بازگشت است و به حالت پایه نیاز دارد.",
}
MARKERS = [("اردو رسم الخط", "ur"), ("Speak only in English", "en"), ("Deutsch", "de"), ("بالعربية", "ar"), ("به فارسی", "fa")]
LOCKS = {"اردو": "ur", "English": "en", "Deutsch": "de", "بالعربية": "ar", "به فارسی": "fa"}


def tone(seconds=0.8, rate=24000, freq=330):
    n = int(seconds * rate)
    return b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


def msg(**kw):
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kw))


class E2ESession:
    """A scripted Gemini Live session. Typed text is answered in the language named by the teacher note; microphone frames form a
    'question' every few frames (the fake microphone is a continuous tone); a question containing SILENT is never answered;
    SLOW makes the voice take 3 s; WRONGLANG makes the first attempt come out in English (the language guard must restart it)."""

    def __init__(self, cfg):
        self.language = "en"
        sysi = cfg.system_instruction if isinstance(cfg.system_instruction, str) else ""
        for key in ("chose Urdu", "chose German", "chose Arabic", "chose Persian"):
            if key in sysi:
                self.language = {"Urdu": "ur", "German": "de", "Arabic": "ar", "Persian": "fa"}[key.split()[1]]
        self.q = asyncio.Queue()
        self.frames = 0
        self.question_no = 0
        self.mic_busy_until = 0.0
        self.wrong_first = False
        self.calls = 0
        self.pending = None

    async def __aenter__(self):
        ACTIVE.append(self)
        return self

    async def __aexit__(self, *a):
        if self in ACTIVE:
            ACTIVE.remove(self)
        return False

    async def send_client_content(self, turns=None, turn_complete=True):
        text = turns.parts[0].text if turns and turns.parts else ""
        if text.startswith("(Language:"):
            for k, v in LOCKS.items():
                if k in text.split(":")[1][:60]:
                    self.language = v
            return
        if "Language correction" in text:
            self.q.put_nowait(msg(interrupted=True))
            self.answer(self.language, tool=False)
            return
        if text.startswith("(The student pressed Stop"):
            self.q.put_nowait(msg(interrupted=True))
            self.q.put_nowait(msg(model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=tone(0.1), mime_type="audio/pcm"))])))
            self.q.put_nowait(msg(turn_complete=True))

    def answer(self, lang, wrong=False, tool=True):
        """The model answers a study question in TWO turns, like the real Gemini 3.8 Live: 1) the show_notes tool call, 2) after our tool response, the voice."""
        spoken_lang = "en" if wrong else lang
        voice = [msg(model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=tone(0.9), mime_type="audio/pcm"))])),
                 msg(output_transcription=types.Transcription(text=SAID[spoken_lang]))]
        if not wrong:
            voice += [msg(model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=tone(0.9), mime_type="audio/pcm"))])),
                      msg(turn_complete=True)]
        if tool:
            self.calls += 1
            self.q.put_nowait(types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[types.FunctionCall(
                id=f"call-{self.calls}", name="show_notes", args={"markdown": NOTES.get(lang, NOTES["en"])})])))
            self.q.put_nowait(msg(turn_complete=True))
            self.pending = voice
        else:
            for m in voice:
                self.q.put_nowait(m)

    async def send_tool_response(self, function_responses=None, **kw):
        if self.pending:
            for m in self.pending:
                self.q.put_nowait(m)
            self.pending = None

    async def send_realtime_input(self, **kw):
        if "text" in kw:
            text = kw["text"]
            lang = self.language
            for marker, code in MARKERS:
                if marker in text:
                    lang = code
            self.language = lang
            if "SILENT" in text:
                return
            if "WRONGLANG" in text and lang != "en":
                self.wrong_first = True
                self.answer(lang, wrong=True)
                return
            await self._respond_later(lang, 3.0 if "SLOW" in text else 0.15)
        elif "audio" in kw:
            self.frames += 1
            if time.time() < self.mic_busy_until:
                self.frames = 0
                return
            if self.frames >= 8:                       # ~0.8 s of the fake microphone tone = the student asked something
                self.frames = 0
                self.question_no += 1
                self.mic_busy_until = time.time() + 2.5
                self.q.put_nowait(msg(input_transcription=types.Transcription(text=f"spoken question number {self.question_no}")))
                await self._respond_later(self.language, 0.2)

    async def _respond_later(self, lang, delay):
        async def go():
            await asyncio.sleep(delay)
            self.answer(lang)
        asyncio.get_running_loop().create_task(go())

    async def receive(self):
        while True:
            m = await self.q.get()
            if m == "DROP":
                raise RuntimeError("1006 None. abnormal closure [internal]")
            yield m
            if m.server_content and m.server_content.turn_complete:
                return


ACTIVE: list[E2ESession] = []


async def fake_notes(message, level, language, history, subject="", voice_mode=False, notes_mode=False, lesson_mode=False,
                     extra_instructions="", continue_text=""):
    text = NOTES.get(language, NOTES["en"])
    if "SILENT" in message:
        text = NOTES["en"].replace("Recursion", "Silent question") if language == "en" else text
    for i in range(0, len(text), 18):
        await asyncio.sleep(0.03)
        yield text[i:i + 18]


async def judge_ok(prompt):
    return '{"language_ok": true, "quality_ok": true, "issues": [], "instruction": ""}'


live._connect = lambda cfg: E2ESession(cfg)
llm.stream_tutor_reply = fake_notes
judge._call_judge_model = judge_ok


@app.on_event("startup")
async def _use_fake_redis():
    state.use_client(fakeredis.FakeAsyncRedis(decode_responses=True))


@app.post("/__e2e/drop")
async def drop():
    """Kill every Gemini session as if Gemini had dropped it (1006): the lesson must reconnect and carry on."""
    for s in list(ACTIVE):
        s.q.put_nowait("DROP")
    return {"dropped": len(ACTIVE)}


@app.get("/__e2e/failures")
async def failures():
    return JSONResponse(await state.recent_failures(50))


if os.path.isdir(VENDOR):
    app.router.routes.insert(0, Mount("/vendor", StaticFiles(directory=VENDOR), name="vendor"))
# main.py mounts the frontend at "/" which swallows every route added after it: put that mount last
_root = [r for r in app.router.routes if isinstance(r, Mount) and r.path == ""]
for r in _root:
    app.router.routes.remove(r)
    app.router.routes.append(r)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765, log_level="warning")


