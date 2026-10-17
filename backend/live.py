"""LUMA Live Teacher: browser  <->  this server  <->  Gemini 3.8 Live (server-to-server; the API key never reaches the browser).

One WebSocket per lesson at /api/live.

 browser -> server   text frames (JSON):  {"type":"start","level","language","subject","conversation_id","gender","voice"}   (first message)
                                          {"type":"text","text":"..."}  {"type":"interrupt"}  {"type":"mic_stop"}  {"type":"stop"}
                     binary frames:       raw 16-bit PCM, 16 kHz, mono (microphone)
 server  -> browser  text frames (JSON):  ready | conversation | notes_delta | notes_done | notes_none | caption | interrupted |
                                          turn_complete | reconnecting | limit | error
                     binary frames:       raw 16-bit PCM, 24 kHz, mono (the teacher's voice)

TWO INDEPENDENT CHANNELS, so the voice can never "read" the notes:
  * VOICE  - Gemini Live is a pure spoken teacher. It is told that the written notes exist but it CANNOT see them: it tells the idea
             like a human teacher (story, analogy, worked example, the trap, the why) instead of reciting a summary.
  * NOTES  - written by LUMA's normal text models (the same provider chain as the chat: Groq / Gemini / Claude with fallback), in
             parallel, streamed to the screen, and saved to the chat history.
The voice and the notes start together (notes are held until the first audio chunk arrives).
"""
import asyncio
import json
import logging
import re
import struct
import time

from fastapi import APIRouter, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types
from pydantic import BaseModel

from . import auth, config, curriculum, db, usage
from .llm import (IDENTITY_RULES, LANGUAGE_NAMES, LEVEL_INSTRUCTIONS, NO_NOTES_MARK, SCOPE_RULES, TRUNCATED_MARK, stream_tutor_reply)

log = logging.getLogger("luma.live")
router = APIRouter()

MAX_TEXT = 4000
MAX_AUDIO_FRAME = 64_000          # bytes per mic frame (frames are ~100 ms = 3.2 KB; this only blocks abuse)
MAX_CONTEXT_MESSAGES = 6          # earlier messages re-seeded when continuing an old chat
HISTORY_TURNS = 20                # messages given to the notes writer (same as the text chat)
NOTES_HOLD_SECONDS = 8            # never hold the notes longer than this waiting for the voice
_active_users: set[str] = set()   # one live lesson per student at a time

# Gemini prebuilt voices (all 30 accepted by gemini-3.8-live in testing). The UI offers a curated subset.
VOICES = {
    "Zephyr": "Bright", "Puck": "Upbeat", "Charon": "Informative", "Kore": "Firm", "Fenrir": "Excitable", "Leda": "Youthful",
    "Orus": "Firm", "Aoede": "Breezy", "Callirrhoe": "Easy-going", "Autonoe": "Bright", "Enceladus": "Breathy", "Iapetus": "Clear",
    "Umbriel": "Easy-going", "Algieba": "Smooth", "Despina": "Smooth", "Erinome": "Clear", "Algenib": "Gravelly", "Rasalgethi": "Informative",
    "Laomedeia": "Upbeat", "Achernar": "Soft", "Alnilam": "Firm", "Schedar": "Even", "Gacrux": "Mature", "Pulcherrima": "Forward",
    "Achird": "Friendly", "Zubenelgenubi": "Casual", "Vindemiatrix": "Gentle", "Sadachbia": "Lively", "Sadaltager": "Knowledgeable", "Sulafat": "Warm",
}


def _text(msg: str, limit: int = 1200) -> str:
    return " ".join(msg.split())[:limit]


# ---------------------------------------------------------------------------------------------- the spoken teacher
SPOKEN_STYLE = {
    "en": "Speak natural, warm, conversational English, like a friendly university lecturer thinking out loud.",
    "ur": ("Speak BOLCHAAL KI URDU: the everyday spoken Urdu a friendly Pakistani university teacher uses in class (not the Urdu of a "
           "textbook, a news bulletin or a translation). Use simple, warm, natural words and sentences, and mix in common English technical "
           "terms exactly as teachers in Pakistan do (function, loop, base case, formula, example, point, step). Avoid bookish, formal or "
           "Persianised words (for example prefer مطلب ہے / یعنی, دیکھیے, فرض کریے and plain "
           "everyday verbs over words like مذکورہ, لامتناہی, طریقہ کار). Use pictures from daily life in Pakistan: chai, cricket, "
           "rickshaw, bazaar, mobile phone, family, exams. Address the student directly (آپ). Always produce Urdu in URDU SCRIPT, NEVER Roman Urdu and NEVER Hindi/Devanagari or Hindi vocabulary (Urdu written in English letters); only "
           "English technical terms may keep their English spelling."),
    "ar": ("Speak a clear, warm, spoken Modern Standard Arabic with a light conversational tone (the register of a friendly lecturer, "
           "not a printed textbook), using technical terms as they are normally used in lectures."),
    "fa": ("Speak natural, warm, spoken Persian (محاوره‌ای, the way a friendly university lecturer talks in class, "
           "not the language of a printed book), using technical terms as they are normally used in lectures."),
    "de": ("Sprich natürliches, warmes, gesprochenes Hochdeutsch, so wie eine freundliche Dozentin oder ein Dozent an der Universität "
           "laut denkend erklärt: kurze und längere Sätze im Wechsel, direkte Ansprache (Sie), verständliche Alltagsbeispiele. "
           "Verwende Fachbegriffe so, wie sie in Vorlesungen üblich sind (englische Fachwörter dürfen bleiben, wenn man sie in "
           "Deutschland so sagt). Kein Schriftdeutsch wie in einem Gesetzestext, und niemals auf Englisch antworten, wenn Deutsch gefragt wurde."),
}

TEACHER_INSTRUCTION = (
    "LIVE TEACHER MODE. You are a gifted university teacher in a live voice conversation with ONE student. The student can also see "
    "written notes (definitions, steps, formulas, code), but those are produced separately by another system: YOU CANNOT SEE THEM and "
    "you must never try to recite or write them. Your job is the SPOKEN lesson, the part a textbook cannot do. "
    "ABSOLUTE RULES: do NOT give a textbook summary; do NOT list definitions; never read out headings, bullet points, tables, code, "
    "or formulas symbol by symbol; do not follow a definition-then-example-then-summary template. "
    "HOW TO TEACH: start from something the student already knows; tell the idea as a short story or a picture they can see in their "
    "head; use one or two vivid analogies and concrete numbers; think out loud (\"so what happens if...?\"); walk through ONE worked "
    "example slowly, step by step, in plain words; point out the trap most students fall into and why; say why the idea matters; "
    "connect it to what comes next. Vary your rhythm: short sentences, long ones, a rhetorical question, a pause. Sound like a person "
    "talking, not reading: \"so\", \"now\", \"okay\", \"imagine...\", contractions, light enthusiasm. Do not open with praise such as "
    "\"great question\". Start speaking at once. "
    "LENGTH AND SHAPE: this is a LESSON, not an answer. For any real concept question speak at least 250 words (about two minutes) "
    "and up to 600. Do not stop after the first analogy. Cover, in your own order: the picture or story, ONE complete worked example "
    "with concrete numbers walked through step by step, the common mistake and why students make it, why the idea matters in practice, "
    "and what to learn next. Only greetings, thanks and clarifying questions may be short. "
    "END with one check-for-understanding question or a small challenge. "
    "Greetings and small talk get a short, friendly spoken reply. If a question is too vague, ask ONE short clarifying question first. "
    "If the student interrupts, stop and answer what they just said. Be accurate; if you are unsure, say so. "
    "Out-of-scope requests get the polite refusal described above, in one or two spoken sentences. "
)


def build_live_prompt(level: str, language: str, subject: str = "") -> str:
    lang = LANGUAGE_NAMES.get(language, "English")
    return (
        "You are LUMA (Learning & University Mentor Assistant), an AI academic tutor. "
        + curriculum.scope_text()
        + SCOPE_RULES + " " + IDENTITY_RULES
        + (f"The student's current subject is: {subject}. " if subject else "")
        + LEVEL_INSTRUCTIONS.get(level, LEVEL_INSTRUCTIONS["undergraduate"]) + " "
        + f"LANGUAGE: always answer in the language the student used in their latest message or spoke (English, Urdu, Arabic, Persian or German); "
          f"if it is unclear, use {lang}. Never answer in English to a question asked in Urdu, Arabic, Persian or German. "
        + "HINDI VS URDU: spoken Urdu and Hindi sound alike, but this is a Pakistani university and the language is URDU. When the student speaks "
          "or writes Hindustani, Urdu or Hindi, ALWAYS answer in URDU: Urdu vocabulary and Urdu script in your transcript, never Hindi/Sanskrit words "
          "(say شکریہ، مسئلہ، براہ مہربانی، مطلب، ضرور; never धन्यवाद، समस्या، कृपया) and never Devanagari. If the student asks you to speak Urdu or says "
          "you are speaking Hindi, switch to proper Urdu immediately and stay in it. "
        + "HOW TO SPEAK EACH LANGUAGE. " + " ".join(f"[{LANGUAGE_NAMES[k]}] {v}" for k, v in SPOKEN_STYLE.items()) + " "
        + TEACHER_INSTRUCTION
    )


_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF]")
_LATIN = re.compile(r"[A-Za-z]")
_URDU_ONLY = re.compile(r"[\u0679\u0688\u0691\u06BA\u06D2\u06C1\u06BE]")          # ٹ ڈ ڑ ں ے ہ ھ
_PERSIAN_HINT = re.compile(r"[\u0698\u06CC]")                                        # ژ ی
_PERSIAN_LETTERS = re.compile(r"[\u067E\u0686\u06AF]")                              # پ چ گ
_ARABIC_YEH = re.compile(r"[\u0649\u064A]")                                          # ى ي


_GERMAN_WORDS = frozenset("der die das den dem des und ist nicht ich du wir sie er es ein eine einen einem einer zu mit auf für von im am an wie was warum "
                          "wann wo wer welche welcher kann können bitte erkläre erklären erklär mir mich uns wird werden sind sein hat haben "
                          "oder aber auch noch nur dass wenn dann also kurz einfach beispiel bedeutet unterschied".split())
_ENGLISH_WORDS = frozenset("the and is are not you your we they it a an of to in on for with how what why when where who which can could please explain "
                           "me my us be been has have or but also only that if then so simply example means difference does do".split())


def latin_language(text: str, default: str) -> str:
    """German or English for Latin-script text (the selector only says which one to prefer when the text is too short to tell)."""
    words = re.findall(r"[a-zäöüß]+", text.lower())
    de, en = sum(w in _GERMAN_WORDS for w in words), sum(w in _ENGLISH_WORDS for w in words)
    umlaut = bool(re.search(r"[äöüß]", text.lower()))
    if umlaut and de >= en:
        return "de"
    if de >= 2 and de > en:
        return "de"
    if en >= 2 and en > de:
        return "en"
    return default


def detect_language(text: str, default: str = "en") -> str:
    """The language of what the student wrote or said. Arabic-script text is Urdu / Persian / Arabic; Latin script keeps the selected
    language (same behaviour as the normal chat). Fixes: asking in Urdu while the selector said English got an English answer."""
    deva = len(_DEVANAGARI.findall(text))
    if deva and deva >= len(_LATIN.findall(text)):
        # Spoken Urdu and Hindi sound almost identical, so Google's speech recognizer often writes an Urdu question in Devanagari.
        # LUMA teaches in Urdu (not Hindi), so Devanagari is treated as Urdu.
        return "ur"
    arabic, latin = len(_ARABIC_SCRIPT.findall(text)), len(_LATIN.findall(text))
    if arabic == 0 or arabic < latin:
        base = default if default in config.SUPPORTED_LANGUAGES else "en"
        if base in ("en", "de") and latin:
            return latin_language(text, base)
        return base
    if _URDU_ONLY.search(text):
        return "ur"
    if _PERSIAN_HINT.search(text) and _PERSIAN_LETTERS.search(text) and not _ARABIC_YEH.search(text):
        return "ur" if default == "ur" else "fa"
    return default if default in ("ur", "ar", "fa") else "ar"


def pick_voice(requested: str | None, gender: str) -> str | None:
    if requested in VOICES:
        return requested
    return (config.LIVE_VOICE_MALE if gender == "male" else config.LIVE_VOICE_FEMALE) or None


def build_config(level: str, language: str, subject: str, voice: str | None) -> types.LiveConnectConfig:
    kw: dict = dict(
        response_modalities=["AUDIO"],                                   # 3.8 live: audio is the supported response modality
        system_instruction=build_live_prompt(level, language, subject),
        # What the student said. The language list matters: without it spoken Urdu was transcribed in Devanagari (Hindi script) or, with
        # only [ur-PK, en-US], in Roman Urdu. With these four it is written in the right script (Urdu/Arabic/Persian script, English in Latin).
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=list(INPUT_LANGUAGES)),
        output_audio_transcription=types.AudioTranscriptionConfig(),     # what the teacher said (collapsed transcript)
        # no tools: the voice never touches the notes. 3.8 live rules: no thinking_config, no enable_affective_dialog, no proactive_audio override
    )
    if voice:
        kw["speech_config"] = types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))
    return types.LiveConnectConfig(**kw)


def _connect(cfg: types.LiveConnectConfig):
    """Indirection so tests can swap in a fake Gemini session."""
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    return client.aio.live.connect(model=config.LIVE_MODEL, config=cfg)


# Added to each typed question (never saved to history): without it the voice tends to give a 30-second answer.
# It is written in the student's own language: an English note made the Urdu voice switch to Roman Urdu (Latin letters).
TEACH_NOTES = {
    "en": ("\n\n[Teacher note: teach this properly, for two to three minutes: a picture or story, a worked example with real numbers "
           "step by step, the common mistake and why it matters. Speak it, don't recite notes.]"),
    "ur": ("\n\n[استاد کے لیے ہدایت: اسے دو سے تین منٹ میں اچھی طرح پڑھائیں: ایک تصویر یا کہانی، اصل اعداد کے ساتھ ایک حل شدہ مثال "
           "قدم بہ قدم، عام غلطی اور اس کی اہمیت۔ اردو رسم الخط میں بولیں، نوٹس نہ پڑھیں۔]"),
    "ar": ("\n\n[ملاحظة للمعلّم: اشرح هذا الموضوع جيدًا في دقيقتين إلى ثلاث دقائق: صورة أو قصة، ومثال محلول بأرقام حقيقية خطوة بخطوة، "
           "والخطأ الشائع وأهميته. تحدّث ولا تقرأ الملاحظات.]"),
    "de": ("\n\n[Hinweis für die Lehrkraft: Erkläre das gründlich, etwa zwei bis drei Minuten lang: ein Bild oder eine kleine Geschichte, "
           "ein durchgerechnetes Beispiel mit echten Zahlen Schritt für Schritt, der typische Fehler und warum er wichtig ist. "
           "Sprich auf Deutsch und lies keine Notizen vor.]"),
    "fa": ("\n\n[یادداشت برای معلم: این موضوع را در دو تا سه دقیقه به‌خوبی درس بدهید: یک تصویر یا داستان، یک مثال حل‌شده با اعداد واقعی "
           "گام‌به‌گام، اشتباه رایج و اهمیت آن. صحبت کنید و یادداشت‌ها را نخوانید.]"),
}


def teach_note(language: str) -> str:
    return TEACH_NOTES.get(language, TEACH_NOTES["en"])


INPUT_LANGUAGES = ("ur-PK", "ar-SA", "fa-IR", "en-US", "de-DE")

INTERRUPT_NOTE = "(The student pressed Stop. Stop teaching now. Reply with just the word Okay and wait for their next question.)"


# ---------------------------------------------------------------------------------------------- one question/answer
class Turn:
    def __init__(self):
        self.question: str | None = None
        self.typed = False
        self.spoken_q: list[str] = []
        self.spoken: list[str] = []          # what the teacher said (transcription)
        self.notes: list[str] = []           # what the notes writer produced
        self.notes_task: asyncio.Task | None = None
        self.notes_started = False
        self.notes_done = False
        self.no_notes = False
        self.voice_done = False
        self.got_audio = False
        self.held: list[str] = []
        self.hold_until = 0.0
        self.language: str | None = None
        self.finished = False
        self.abandoned = False               # replaced by a newer question: never reported as "complete"
        self.cut = False                     # Stop pressed: the voice was cut short


class Lesson:
    def __init__(self, ws: WebSocket, user: dict, params: dict):
        self.ws, self.user, self.p = ws, user, params
        self.email = user["email"]
        self.cid: str | None = params.get("conversation_id") or None
        self.session = None
        self.turn: Turn | None = None
        self.mute_audio = False
        self._send_lock = asyncio.Lock()
        self.started = time.time()

    # ---------------------------------------------------------------- helpers
    async def send(self, payload: dict) -> None:
        try:
            async with self._send_lock:
                await self.ws.send_json(payload)
        except Exception:
            raise WebSocketDisconnect()

    async def _ensure_conversation(self, first_text: str) -> str:
        if self.cid is None:
            self.cid = await asyncio.to_thread(db.create_conversation, self.email, _text(first_text, 58) or "Live lesson")
            await self.send({"type": "conversation", "id": self.cid})
        return self.cid

    # ---------------------------------------------------------------- notes (independent of the voice)
    def _start_notes(self, turn: Turn, history: list[dict]) -> None:
        if turn.notes_started or not turn.question:
            return
        turn.notes_started = True
        turn.language = turn.language or detect_language(turn.question, self.p["language"])
        turn.hold_until = time.time() + NOTES_HOLD_SECONDS
        turn.notes_task = asyncio.create_task(self._notes_worker(turn, turn.question, history))

    async def _emit_notes(self, turn: Turn, piece: str) -> None:
        turn.notes.append(piece)
        if config.LIVE_SYNC_NOTES and not turn.got_audio and time.time() < turn.hold_until:
            turn.held.append(piece)                      # wait for the voice so text and voice start together
        else:
            await self._release_notes(turn)
            await self.send({"type": "notes_delta", "text": piece})

    async def _release_notes(self, turn: Turn) -> None:
        if turn.held:
            held, turn.held = "".join(turn.held), []
            await self.send({"type": "notes_delta", "text": held})

    async def _notes_worker(self, turn: Turn, question: str, history: list[dict]) -> None:
        head = ""
        decided = False
        try:
            await self.send({"type": "language", "language": turn.language})        # the page switches direction / selector to match
            async for piece in stream_tutor_reply(question, self.p["level"], turn.language, history, self.p["subject"],
                                                  voice_mode=False, notes_mode=True):
                piece = piece.replace(TRUNCATED_MARK, "")
                if not piece:
                    continue
                if not decided:                           # the first few characters tell us whether there are notes at all
                    head += piece
                    probe = head.strip()
                    if len(probe) < len(NO_NOTES_MARK) and NO_NOTES_MARK.startswith(probe):
                        continue
                    decided = True
                    if probe.startswith(NO_NOTES_MARK):
                        turn.no_notes = True
                        await self.send({"type": "notes_none"})
                        return
                    piece, head = head, ""
                await self._emit_notes(turn, piece)
            if not decided:                               # the stream ended while still undecided
                if head.strip() and not NO_NOTES_MARK.startswith(head.strip()):
                    await self._emit_notes(turn, head)
                else:
                    turn.no_notes = True
                    await self.send({"type": "notes_none"})
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:
            raise
        except Exception as exc:
            log.warning("notes writer failed: %s: %s", type(exc).__name__, str(exc)[:200])
        finally:
            turn.notes_done = True
            if not turn.no_notes:
                try:
                    await self._release_notes(turn)
                    await self.send({"type": "notes_done"})
                except Exception:
                    pass
            try:
                await self._maybe_finish(turn)
            except Exception:
                pass

    # ---------------------------------------------------------------- finishing a turn
    async def _maybe_finish(self, turn: Turn) -> None:
        """A turn is finished when the voice is done AND the notes (if any) are done. Then it is saved and reported."""
        if turn.finished or turn.abandoned or not turn.voice_done:
            return
        if turn.notes_started and not turn.notes_done:
            return
        turn.finished = True
        await self._release_notes(turn)
        await self._persist(turn)
        if self.turn is turn:
            await self.send({"type": "turn_complete"})

    async def _persist(self, turn: Turn) -> None:
        question = turn.question or ""
        notes = "".join(turn.notes).strip()
        if notes.startswith(NO_NOTES_MARK):
            notes = ""
        answer = notes or ("" if turn.cut else "".join(turn.spoken).strip())
        if question or answer:
            cid = await self._ensure_conversation(question or "Live lesson")
            if question and not turn.typed:               # typed questions were saved when they were sent
                await asyncio.to_thread(db.add_message, cid, "user", question)
            if answer:
                await asyncio.to_thread(db.add_message, cid, "assistant", answer)

    async def _abandon(self, turn: Turn | None) -> None:
        """A new question arrived (or the lesson ended) while this one was still running: stop its notes, keep what exists."""
        if turn is None or turn.finished:
            return
        turn.abandoned = True
        if turn.notes_task and not turn.notes_task.done():
            turn.notes_task.cancel()
            await asyncio.gather(turn.notes_task, return_exceptions=True)
        turn.finished = True
        await self._persist(turn)

    # ---------------------------------------------------------------- main
    async def run(self) -> None:
        """Run the lesson. If Gemini's side drops mid-answer (observed: `1006 abnormal closure [internal]`), reconnect,
        tell the teacher what it had already covered and let it carry on, instead of ending the student's lesson."""
        cfg = build_config(self.p["level"], self.p["language"], self.p["subject"], self.p["voice"])
        attempt = 0
        while True:
            try:
                await self._session_once(cfg, resumed=attempt > 0)
                return
            except (WebSocketDisconnect, asyncio.CancelledError):
                raise
            except Exception as exc:
                attempt += 1
                if attempt > config.LIVE_RECONNECTS:
                    raise
                log.warning("gemini live dropped (%s: %s); reconnecting %d/%d", type(exc).__name__, str(exc)[:160], attempt, config.LIVE_RECONNECTS)
                await self.send({"type": "reconnecting", "attempt": attempt})
                await asyncio.sleep(min(2 * attempt, 5))

    async def _session_once(self, cfg, resumed: bool) -> None:
        async with _connect(cfg) as session:
            self.session = session
            await self._seed_history(session)
            if resumed:
                await self._resume_turn(session)
            await self.send({"type": "ready", "model": config.LIVE_MODEL, "voice": self.p["voice"], "resumed": resumed})
            tasks = [asyncio.create_task(self._from_browser()), asyncio.create_task(self._from_gemini()),
                     asyncio.create_task(self._watchdog())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if t.exception() and not isinstance(t.exception(), (WebSocketDisconnect, asyncio.CancelledError)):
                    raise t.exception()

    async def _resume_turn(self, session) -> None:
        """After a reconnect: if an answer was in progress, ask the teacher to carry on from where it stopped."""
        t = self.turn
        if not t or t.finished or t.voice_done or not t.question:
            return
        said = "".join(t.spoken).strip()[-700:]
        note = ("(The connection dropped while you were answering. The student's question was: " + _text(t.question, 600) + ". "
                + (f"You had just said: \"...{said}\". " if said else "")
                + "Please carry on from where you stopped, aloud, in your own words, without mentioning the interruption.)")
        await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=note)]), turn_complete=True)

    async def _seed_history(self, session) -> None:
        """Continue an old chat: give the model the last few messages as context (no reply is triggered)."""
        if not self.cid or not await asyncio.to_thread(db.get_conversation, self.email, self.cid):
            self.cid = None
            return
        hist = await asyncio.to_thread(db.get_messages, self.cid, MAX_CONTEXT_MESSAGES)
        if hist:
            recap = "Earlier in this conversation:\n" + "\n".join(
                f"{'Student' if m['role'] == 'user' else 'Teacher (written notes)'}: {_text(m['content'], 500)}" for m in hist)
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=recap)]), turn_complete=False)

    async def _watchdog(self) -> None:
        await asyncio.sleep(config.LIVE_MAX_SECONDS)
        await self.send({"type": "limit", "message": "This live lesson reached its time limit. Start a new one to continue."})

    async def _from_browser(self) -> None:
        while True:
            m = await self.ws.receive()
            if m.get("type") == "websocket.disconnect":
                return
            if m.get("bytes") is not None:
                b = m["bytes"]
                if 0 < len(b) <= MAX_AUDIO_FRAME:
                    await self.session.send_realtime_input(audio=types.Blob(data=b, mime_type="audio/pcm;rate=16000"))
                continue
            try:
                msg = json.loads(m.get("text") or "")
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "stop":
                return
            if kind == "mic_stop":
                await self.session.send_realtime_input(audio_stream_end=True)
            elif kind == "interrupt":                       # Stop: silence the teacher now; the written notes keep coming
                if self.turn and not self.turn.finished:
                    self.turn.cut = True
                    self.turn.voice_done = True
                    await self._maybe_finish(self.turn)
                self.mute_audio = True
                await self.session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=INTERRUPT_NOTE)]), turn_complete=True)
            elif kind == "text":
                await self._on_typed(str(msg.get("text", "")).strip())

    async def _on_typed(self, text: str) -> None:
        if not text or len(text) > MAX_TEXT:
            await self.send({"type": "error", "message": "Please send a question of 1 to 4000 characters."})
            return
        allowed, _ = usage.check_and_increment(self.email)
        if not allowed:
            await self.send({"type": "limit", "message": "Daily message limit reached for this pilot account."})
            return
        await self._abandon(self.turn)
        cid = await self._ensure_conversation(text)
        history = await asyncio.to_thread(db.get_messages, cid, HISTORY_TURNS)      # before this question, like the text chat
        await asyncio.to_thread(db.add_message, cid, "user", text)
        t = self.turn = Turn()
        t.question, t.typed = text, True
        t.language = detect_language(text, self.p["language"])
        self.mute_audio = False
        self._start_notes(t, history)                      # the notes writer starts at once, independently of the voice
        await self.session.send_realtime_input(text=text + teach_note(t.language))

    async def _from_gemini(self) -> None:
        while True:                                        # receive() yields one model turn per call
            async for msg in self.session.receive():
                if getattr(msg, "go_away", None):
                    raise ConnectionError("live session is closing (go_away)")
                sc = msg.server_content
                if not sc:
                    continue
                if sc.interrupted:
                    await self.send({"type": "interrupted"})
                if sc.input_transcription and sc.input_transcription.text:
                    await self._on_heard(sc.input_transcription.text)
                if sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            await self._on_audio(part.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    await self._on_said(sc.output_transcription.text)
                if sc.turn_complete:
                    t = self.turn
                    if t and not t.finished:
                        t.voice_done = True
                        await self._maybe_finish(t)

    async def _on_heard(self, text: str) -> None:
        """The student SPOKE (microphone). Transcription arrives in pieces; the first piece opens a new turn."""
        t = self.turn
        if (t is None or t.finished or t.voice_done) and len(re.sub(r"[\W\d_]+", "", text)) < 2:
            return                                         # a stray letter from room noise, not a question
        if t is None or t.finished or t.voice_done:
            await self._abandon(t)
            t = self.turn = Turn()
            self.mute_audio = False
            allowed, _ = usage.check_and_increment(self.email)
            if not allowed:
                await self.send({"type": "limit", "message": "Daily message limit reached for this pilot account."})
        if t.typed:
            return
        t.spoken_q.append(text.strip())
        t.question = " ".join(t.spoken_q).strip()
        await self.send({"type": "caption", "who": "user", "text": text})

    async def _on_audio(self, data: bytes) -> None:
        t = self.turn
        if t and not t.got_audio:
            t.got_audio = True
            await self._release_notes(t)                   # the voice has started: show the notes now, so both begin together
            if not t.typed and not t.notes_started:        # a spoken question: now we know the question, start the notes
                cid = self.cid
                history = await asyncio.to_thread(db.get_messages, cid, HISTORY_TURNS) if cid else []
                self._start_notes(t, history)
        if self.mute_audio:
            return
        try:
            await self.ws.send_bytes(data)
        except Exception:
            raise WebSocketDisconnect()

    async def _on_said(self, text: str) -> None:
        t = self.turn
        if t and not t.finished:
            t.spoken.append(text)
        if not self.mute_audio:
            await self.send({"type": "caption", "who": "teacher", "text": text})


@router.websocket("/api/live")
async def live_ws(websocket: WebSocket):
    user = auth.current_user_ws(websocket)
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    email = user["email"]
    try:
        if not config.GEMINI_API_KEY:
            await websocket.send_json({"type": "error", "message": "The live teacher is not configured on this server."})
            return
        first = await asyncio.wait_for(websocket.receive_json(), 15)
        if first.get("type") != "start":
            await websocket.send_json({"type": "error", "message": "Expected a start message."})
            return
        level, language = first.get("level", "undergraduate"), first.get("language", "en")
        if level not in config.ACADEMIC_LEVELS or language not in config.SUPPORTED_LANGUAGES:
            await websocket.send_json({"type": "error", "message": "Invalid level or language."})
            return
        gender = "male" if first.get("gender") == "male" else "female"
        params = {"level": level, "language": language, "subject": str(first.get("subject", ""))[:100].strip(), "gender": gender,
                  "voice": pick_voice(first.get("voice"), gender),
                  "conversation_id": str(first.get("conversation_id") or "")[:64] or None}
        # A lesson the student just ended may still be closing its Gemini session: give it a moment before calling it "another tab".
        for _ in range(int(config.LIVE_START_WAIT * 10)):
            if email not in _active_users:
                break
            await asyncio.sleep(0.1)
        if email in _active_users:
            await websocket.send_json({"type": "error", "message": "You already have a live lesson open in another tab."})
            return
        if len(_active_users) >= config.LIVE_MAX_CONCURRENT:
            await websocket.send_json({"type": "error", "message": "The live teacher is busy. Please try again in a few minutes."})
            return
        _active_users.add(email)
        lesson = Lesson(websocket, user, params)
        try:
            await lesson.run()
        finally:
            _active_users.discard(email)
            try:
                await lesson._abandon(lesson.turn)         # don't lose a half-finished turn
            except Exception:
                pass
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as exc:
        log.warning("live session failed: %s: %s", type(exc).__name__, str(exc)[:300])      # details stay in the server log
        try:
            await websocket.send_json({"type": "error", "message": "The live teacher connection was interrupted. Please start it again."})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------- voice preview (audition a voice)
PREVIEW_PER_HOUR = 12
PREVIEW_TEXT = {
    "en": ("Imagine you are looking for one book in a huge library. Without an index you would have to check every shelf. "
           "A hash table is like a magic librarian who points you to the exact shelf in a single step."),
    "ur": ("فرض کریں آپ ایک بہت بڑی لائبریری میں ایک کتاب ڈھونڈ رہے ہیں۔ اگر کوئی فہرست نہ ہو تو آپ کو ہر شیلف دیکھنی پڑے گی۔ "
           "ہیش ٹیبل ایک ایسے لائبریرین کی طرح ہے جو آپ کو ایک ہی قدم میں صحیح شیلف بتا دیتا ہے۔"),
    "de": ("Stell dir vor, du suchst ein einziges Buch in einer riesigen Bibliothek. Ohne Verzeichnis müsstest du jedes Regal durchsuchen. "
           "Eine Hashtabelle ist wie ein magischer Bibliothekar, der dich in einem einzigen Schritt zum richtigen Regal führt."),
    "ar": ("تخيّل أنك تبحث عن كتاب واحد في مكتبة ضخمة. من دون فهرس ستضطر إلى تفتيش كل رفّ. "
           "جدول التجزئة مثل أمين مكتبة سحري يدلّك على الرفّ الصحيح في خطوة واحدة."),
    "fa": ("تصور کنید در یک کتابخانهٔ بزرگ دنبال یک کتاب می‌گردید. بدون فهرست باید همهٔ قفسه‌ها را بگردید. "
           "جدول هش مثل یک کتابدار جادویی است که در یک قدم قفسهٔ درست را نشانتان می‌دهد."),
}
_preview_log: dict[str, list[float]] = {}


class PreviewRequest(BaseModel):
    voice: str
    language: str = "en"


def wav_bytes(pcm: bytes, rate: int = 24000) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV header so the browser can play it with a plain Audio element."""
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


@router.post("/api/live/preview")
async def preview_voice(req: PreviewRequest, user: dict = Depends(auth.current_user)):
    """Audition a voice in the student's language: a short teacher-style sentence, a few seconds of Gemini audio."""
    if req.voice not in VOICES:
        raise HTTPException(400, "Unknown voice.")
    if req.language not in config.SUPPORTED_LANGUAGES:
        raise HTTPException(400, "Unsupported language.")
    if not config.GEMINI_API_KEY:
        raise HTTPException(503, "The live teacher is not configured on this server.")
    now = time.time()
    recent = [t for t in _preview_log.get(user["email"], []) if now - t < 3600]
    if len(recent) >= PREVIEW_PER_HOUR:
        raise HTTPException(429, "Too many previews this hour. Please wait a little.")
    _preview_log[user["email"]] = recent + [now]
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=("You are a voice actor with a warm, natural, expressive teacher's voice. Read the user's text aloud exactly as "
                            "written, once, with natural intonation, and say nothing else. " + SPOKEN_STYLE.get(req.language, "")),
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=req.voice))))
    pcm = bytearray()
    try:
        async def run():
            async with _connect(cfg) as session:
                await session.send_realtime_input(text=PREVIEW_TEXT[req.language])
                async for msg in session.receive():
                    sc = msg.server_content
                    if sc and sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                pcm.extend(part.inline_data.data)
                    if sc and sc.turn_complete:
                        return
        await asyncio.wait_for(run(), 40)
    except Exception as exc:
        log.warning("voice preview failed: %s: %s", type(exc).__name__, str(exc)[:200])
        if not pcm:
            raise HTTPException(502, "Could not generate the preview right now.")
    if not pcm:
        raise HTTPException(502, "Could not generate the preview right now.")
    return Response(wav_bytes(bytes(pcm)), media_type="audio/wav", headers={"Cache-Control": "no-store"})
