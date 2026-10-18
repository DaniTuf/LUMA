"""LUMA Live Teacher: browser  <->  this server  <->  Gemini 3.8 Live (server-to-server; the API key never reaches the browser).

One WebSocket per lesson at /api/live.

 browser -> server   text frames (JSON):  {"type":"start","level","language","subject","conversation_id","gender","voice"}   (first message)
                                          {"type":"text","text":"..."}  {"type":"interrupt"}  {"type":"mic_stop"}  {"type":"stop"}
                                          {"type":"set_language","language":"ur"}  {"type":"ping"}
                     binary frames:       raw 16-bit PCM, 16 kHz, mono (microphone)
 server  -> browser  text frames (JSON):  ready | conversation | turn_start | language | notes_delta | notes_done | notes_none | caption |
                                          interrupted | voice_restart | turn_complete | reconnecting | notice | limit | error | ping | pong
                     binary frames:       raw 16-bit PCM, 24 kHz, mono (the teacher's voice)

ONE MODEL DOES EVERYTHING. Gemini 3.8 Live is the only model of a live lesson. For a study question, in ONE conversation turn, it
  1. WRITES the study notes (the `show_notes` tool call: Markdown, in the lesson language) - they appear on the screen at once, then
  2. SPEAKS: it is told to take the most important part of the notes it just wrote and ELABORATE on it aloud (story, worked example, the trap,
     why it matters), not to read them out.
So the text and the voice always talk about the same thing, in the same language, and nothing is produced by a second model that could disagree.
(Small talk gets a short spoken answer, no notes.) Checked on the real service: the notes arrive after ~4-8 s, the voice follows ~1 s later.

LANGUAGE: the language chosen in the interface decides what is spoken and written. It changes only when the student changes the selector
or explicitly asks for another language (langpolicy.explicit_request). The language the QUESTION is asked in does not matter.

NEVER FROZEN: every turn has a boundary the server enforces itself (interruptions, late transcripts, a teacher that goes silent or never
says "turn complete"), the browser and server ping each other, and a dead Gemini session is reconnected with the lesson state intact.
"""
import asyncio
import json
import logging
import re
import struct
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from . import auth, config, curriculum, db, errors, judge, langpolicy, state, usage
from .llm import IDENTITY_RULES, LANGUAGE_NAMES, LEVEL_INSTRUCTIONS, SCOPE_RULES

log = logging.getLogger("luma.live")
router = APIRouter()

MAX_TEXT = 4000
MAX_AUDIO_FRAME = 64_000          # bytes per mic frame (frames are ~100 ms = 3.2 KB; this only blocks abuse)
MAX_CONTEXT_MESSAGES = 6          # earlier messages re-seeded when continuing an old chat
LANG_CHECK_MIN_CHARS = 70         # transcript length before the voice's language is checked

# Gemini prebuilt voices (all 30 accepted by gemini-3.8-live in testing). The UI offers a curated subset.
VOICES = {
    "Zephyr": "Bright", "Puck": "Upbeat", "Charon": "Informative", "Kore": "Firm", "Fenrir": "Excitable", "Leda": "Youthful",
    "Orus": "Firm", "Aoede": "Breezy", "Callirrhoe": "Easy-going", "Autonoe": "Bright", "Enceladus": "Breathy", "Iapetus": "Clear",
    "Umbriel": "Easy-going", "Algieba": "Smooth", "Despina": "Smooth", "Erinome": "Clear", "Algenib": "Gravelly", "Rasalgethi": "Informative",
    "Laomedeia": "Upbeat", "Achernar": "Soft", "Alnilam": "Firm", "Schedar": "Even", "Gacrux": "Mature", "Pulcherrima": "Forward",
    "Achird": "Friendly", "Zubenelgenubi": "Casual", "Vindemiatrix": "Gentle", "Sadachbia": "Lively", "Sadaltager": "Knowledgeable", "Sulafat": "Warm",
}


async def _db(fn, *args, default=None):
    """Await a database call. A broken or unreachable database must never look like a broken voice connection (it used to trigger endless
    'reconnecting'): report it, return `default`, and let the lesson carry on without history / saving."""
    try:
        return await fn(*args)
    except (SQLAlchemyError, db.DatabaseUnavailable) as exc:
        await errors.report("db", exc, action="degraded", op=getattr(fn, "__name__", "db"))
        return default


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

# Used by the NORMAL (non-live) voice mode, where the written text comes from another model: this voice never sees it.
SPOKEN_TEACHER_INSTRUCTION = (
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


# Used by the LIVE teacher: ONE model writes the notes and then elaborates on them aloud.
NOTES_TOOL_NAME = "show_notes"
NOTES_TOOL = types.Tool(function_declarations=[types.FunctionDeclaration(
    name=NOTES_TOOL_NAME,
    description=("Put the written study notes for this answer on the student's screen. Call it exactly ONCE per study question, as your very first "
                 "action, BEFORE you speak. Do not call it for greetings, thanks or small talk."),
    parameters=types.Schema(type="OBJECT", required=["markdown"], properties={"markdown": types.Schema(
        type="STRING", description=("The study notes in Markdown, in the lesson language: a # title, then ## sections (precise definition, the key "
                                    "steps or reasoning, ONE worked example with numbers, formulas as LaTeX, code in fenced blocks, the common "
                                    "mistake, a short summary). About 250-400 words: complete, never cut off."))}))])

TEACHER_INSTRUCTION = (
    "LIVE TEACHER MODE. You are a gifted university teacher in a live voice conversation with ONE student, and YOU are the only author of "
    "everything the student sees and hears. For every STUDY QUESTION work in exactly two steps. "
    f"STEP 1 - WRITE: your very first action is to call the tool {NOTES_TOOL_NAME} ONCE with the written study notes (Markdown; see the tool "
    "description). Do not say anything before calling it, and start the call immediately. The notes are written in the lesson language and are "
    "complete: precise definitions, the reasoning step by step, one worked example with numbers, formulas, code, the common mistake, a short summary. "
    "STEP 2 - ELABORATE ALOUD: when the tool returns, the notes are on the student's screen. Now SPEAK. Do NOT read the notes out and do NOT "
    "repeat them as a summary. Take the most important or hardest part of the notes you just wrote and ELABORATE on it like a human lecturer, in your "
    "own words, so the voice adds what a page cannot: start from something the student already knows; tell it as a short story or a picture they "
    "can see in their head; use one or two vivid analogies and concrete numbers; think out loud (\"so what happens if...?\"); walk through the "
    "worked example from your notes slowly, step by step, in plain words; point out the trap most students fall into and why; say why the idea "
    "matters; connect it to what comes next. Refer to the screen naturally (\"as you can see in the example on your screen\"). Never read headings, "
    "bullet points, tables, code or formulas symbol by symbol. Vary your rhythm: short sentences, long ones, a rhetorical question. Sound like a "
    "person talking: \"so\", \"now\", \"okay\", \"imagine...\", contractions, light enthusiasm. Do not open with praise such as \"great question\". "
    "LENGTH: the spoken part is a LESSON: at least 250 words (about two minutes) and up to 600, never cut off in the middle of a sentence. "
    "END with one check-for-understanding question or a small challenge. "
    "NO NOTES FOR: greetings, thanks, small talk, a vague question (ask ONE short clarifying question), and out-of-scope requests (the polite "
    "refusal described above, in one or two spoken sentences): for these do NOT call the tool, just answer briefly by voice. "
    "If the student interrupts, stop and answer what they just said. Be accurate; if you are unsure, say so. "
)


def elaborate_prompt(language: str) -> str:
    """The tool result: the second half of the two-step instruction, delivered at the moment the notes are on screen."""
    name = LANGUAGE_NAMES.get(language, "English")
    return ("The notes are now on the student's screen. NOW SPEAK, aloud, in " + name + ": do not call the tool again, do not read the notes and do not "
            "summarise them. Choose the single most important or hardest part of the notes you just wrote and elaborate on it in detail like a lecturer: a "
            "picture or story, the worked example step by step with the numbers, the trap students fall into, why it matters, what comes next. At least 250 "
            "words, finish every sentence, end with a check question. " + langpolicy.directive(language))


def build_live_prompt(level: str, language: str, subject: str = "") -> str:
    lang = LANGUAGE_NAMES.get(language, "English")
    return (
        "You are LUMA (Learning & University Mentor Assistant), an AI academic tutor. "
        + curriculum.scope_text()
        + SCOPE_RULES + " " + IDENTITY_RULES
        + (f"The student's current subject is: {subject}. " if subject else "")
        + LEVEL_INSTRUCTIONS.get(level, LEVEL_INSTRUCTIONS["undergraduate"]) + " "
        + f"LANGUAGE POLICY (the most important rule): the student chose {lang} in the interface, so you speak and teach ONLY in {lang}, from your "
          f"very first word. This does NOT depend on the language the student asks in: they may type or speak in another language; understand it and "
          f"still answer in {lang}. Change language ONLY when the student explicitly asks you to talk, explain or answer in another language (for "
          "example 'explain this in Urdu', 'speak German', 'talk to me in Arabic', 'اردو میں بتائیں', 'auf Deutsch bitte'), or when a bracketed system "
          "note that starts with 'Language' tells you to. Then switch immediately and stay in the new language for every later answer until they "
          "ask again. English technical terms may stay English inside any language. "
        + "HINDI VS URDU: spoken Urdu and Hindi sound alike, but this is a Pakistani university. Whenever you speak Urdu it is proper Urdu: Urdu "
          "vocabulary and Urdu script in your transcript, never Hindi/Sanskrit words (say شکریہ، مسئلہ، براہ مہربانی، مطلب، ضرور; never धन्यवाद، समस्या، "
          "कृपया) and never Devanagari. If the student asks for Hindi or Hindustani, use Urdu. If the student says you are speaking Hindi, switch to "
          "proper Urdu immediately. "
        + "HOW TO SPEAK EACH LANGUAGE. " + " ".join(f"[{LANGUAGE_NAMES[k]}] {v}" for k, v in SPOKEN_STYLE.items()) + " "
        + TEACHER_INSTRUCTION
    )


# Sent to the voice (in the language itself: an English note made the Urdu voice slip into Roman Urdu) when the language changes.
LANGUAGE_LOCK = {
    "en": "(Language: from now on speak and teach ONLY in English, whatever language the student asks in. Change only if the student explicitly asks for another language.)",
    "ur": "(Language: اب سے صرف اردو میں اور اردو رسم الخط میں پڑھائیں، چاہے طالب علم کسی بھی زبان میں سوال پوچھے۔ زبان صرف اس وقت بدلیں جب طالب علم خود کہے۔)",
    "ar": "(Language: من الآن تحدّث وعلّم بالعربية فقط، مهما كانت لغة سؤال الطالب. لا تغيّر اللغة إلا إذا طلب الطالب ذلك صراحةً.)",
    "fa": "(Language: از این پس فقط به فارسی صحبت کنید و درس بدهید، حتی اگر دانشجو به زبان دیگری بپرسد. فقط وقتی زبان را عوض کنید که دانشجو صریحاً بخواهد.)",
    "de": "(Language: Sprich und unterrichte ab jetzt NUR auf Deutsch, egal in welcher Sprache der Student fragt. Wechsle die Sprache nur, wenn der Student es ausdrücklich verlangt.)",
}
# Sent when the voice started in the wrong language: the same question again, with a firm reminder.
LANGUAGE_FIX = {
    "en": "(Language correction: you answered in the wrong language. Start the answer again now and speak ONLY English.) The student's question: ",
    "ur": "(Language correction: آپ نے غلط زبان میں جواب دیا۔ ابھی دوبارہ شروع کریں اور صرف اردو میں، اردو رسم الخط میں بولیں۔) طالب علم کا سوال: ",
    "ar": "(Language correction: أجبتَ بلغة خاطئة. ابدأ الإجابة من جديد الآن وتحدّث بالعربية فقط.) سؤال الطالب: ",
    "fa": "(Language correction: به زبان اشتباه پاسخ دادید. همین حالا دوباره شروع کنید و فقط به فارسی صحبت کنید.) پرسش دانشجو: ",
    "de": "(Language correction: Du hast in der falschen Sprache geantwortet. Beginne die Antwort jetzt noch einmal und sprich NUR Deutsch.) Frage des Studenten: ",
}



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
        tools=[NOTES_TOOL],                                              # the ONE tool: the model writes its own notes (show_notes)
        # 3.8 live rules: no thinking_config, no enable_affective_dialog, no proactive_audio override
    )
    if voice:
        kw["speech_config"] = types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))
    return types.LiveConnectConfig(**kw)


def _connect(cfg: types.LiveConnectConfig):
    """Indirection so tests can swap in a fake Gemini session."""
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    return client.aio.live.connect(model=config.LIVE_MODEL, config=cfg)


# Added to each typed question (never saved to history). It is written in the student's own language: an English note made the Urdu voice
# switch to Roman Urdu (Latin letters).
TEACH_NOTES = {
    "en": ("\n\n[Teacher note: first call show_notes with the written study notes, then speak: elaborate on the most important part of your notes "
           "for two to three minutes (a picture or story, the worked example step by step, the common mistake). Do not read the notes out. "
           "Notes and speech only in English.]"),
    "ur": ("\n\n[استاد کے لیے ہدایت: پہلے show_notes کو تحریری نوٹس کے ساتھ کال کریں، پھر بولیں: اپنے نوٹس کے سب سے اہم حصے کی دو سے تین منٹ تک "
           "وضاحت کریں (ایک تصویر یا کہانی، حل شدہ مثال قدم بہ قدم، عام غلطی)۔ نوٹس پڑھ کر نہ سنائیں۔ نوٹس اور آواز دونوں اردو رسم الخط میں۔]"),
    "ar": ("\n\n[ملاحظة للمعلّم: استدعِ show_notes أولًا مع الملاحظات المكتوبة، ثم تحدّث: اشرح أهمّ جزء في ملاحظاتك لمدة دقيقتين إلى ثلاث دقائق "
           "(صورة أو قصة، المثال المحلول خطوة بخطوة، الخطأ الشائع). لا تقرأ الملاحظات. الملاحظات والكلام بالعربية فقط.]"),
    "de": ("\n\n[Hinweis für die Lehrkraft: Rufe zuerst show_notes mit den schriftlichen Notizen auf, dann sprich: Erkläre den wichtigsten Teil deiner "
           "Notizen etwa zwei bis drei Minuten lang (ein Bild oder eine kleine Geschichte, das Beispiel Schritt für Schritt, der typische Fehler). "
           "Lies die Notizen nicht vor. Notizen und Rede nur auf Deutsch.]"),
    "fa": ("\n\n[یادداشت برای معلم: ابتدا show_notes را با یادداشت‌های نوشتاری فراخوانی کنید، سپس صحبت کنید: مهم‌ترین بخش یادداشت‌هایتان را دو تا سه دقیقه "
           "توضیح دهید (یک تصویر یا داستان، مثال حل‌شده گام‌به‌گام، اشتباه رایج). یادداشت‌ها را نخوانید. یادداشت‌ها و صحبت فقط به فارسی.]"),
}


def teach_note(language: str) -> str:
    return TEACH_NOTES.get(language, TEACH_NOTES["en"])


INPUT_LANGUAGES = ("ur-PK", "ar-SA", "fa-IR", "en-US", "de-DE")

INTERRUPT_NOTE = "(The student pressed Stop. Stop teaching now. Reply with just the word Okay and wait for their next question.)"



# ---------------------------------------------------------------------------------------------- one question/answer
class Turn:
    def __init__(self, language: str | None = None):
        self.id = uuid.uuid4().hex[:16]
        self.question: str | None = None
        self.typed = False
        self.spoken_q: list[str] = []
        self.spoken: list[str] = []          # what the teacher said (transcription)
        self.notes = ""                      # the written notes the SAME model produced with the show_notes tool
        self.notes_given = False
        self.notes_attempts = 0              # rejected attempts (wrong language / leaked instructions) before notes were accepted
        self.notes_at = 0.0
        self.saved = False                   # the question and answer are in the chat history
        self.voice_done = False
        self.got_audio = False
        self.language: str | None = language
        self.finished = False
        self.abandoned = False               # replaced by a newer question: never reported as "complete"
        self.cut = False                     # Stop pressed: the voice was cut short
        self.created = time.time()
        self.question_at = 0.0               # when the (last part of the) question arrived
        self.audio_at = 0.0                  # when the teacher's first audio arrived
        self.last_activity = time.time()     # last sign of life from the voice
        self.restarting = False              # the voice is being restarted in the right language
        self.restart_at = 0.0
        self.lang_retries = 0
        self.lang_checks = 0
        self.silent = False                  # the teacher never answered
        self.resumed = False                 # picked up from a Redis checkpoint after a worker died
        self.checkpoint_task: asyncio.Task | None = None
        self.generation_done_at = 0.0        # Gemini said it finished generating (turn_complete can arrive late)


class Lesson:
    def __init__(self, ws: WebSocket, user: dict, params: dict):
        self.ws, self.user, self.p = ws, user, params
        self.email = user["email"]
        self.cid: str | None = params.get("conversation_id") or None
        self.language: str = params["language"]            # the language of this lesson: changes only on request (see langpolicy)
        self.session = None
        self.turn: Turn | None = None
        self.mute_audio = False
        self.ended_by_user = False
        self._send_lock = asyncio.Lock()
        self.started = time.time()
        self._bg: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- helpers
    async def send(self, payload: dict) -> None:
        try:
            async with self._send_lock:
                await self.ws.send_json(payload)
        except Exception:
            raise WebSocketDisconnect()

    def _background(self, coro) -> None:
        """Fire-and-forget work (checkpoints, quality log) that must never slow down or break the lesson."""
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _ensure_conversation(self, first_text: str) -> str:
        if self.cid is None:
            self.cid = await _db(db.create_conversation, self.email, _text(first_text, 58) or "Live lesson")
            if self.cid:
                await self.send({"type": "conversation", "id": self.cid})
        return self.cid

    async def _open_turn(self) -> Turn:
        """Every question starts a new turn; the browser is told so it can never mix two answers (or stay stuck on the previous one)."""
        t = self.turn = Turn(self.language)
        await self.send({"type": "turn_start", "id": t.id, "language": t.language})
        return t

    async def set_language(self, lang: str, reason: str) -> bool:
        """The lesson language changes (selector or an explicit request): tell the browser and the voice. Nothing else ever changes it."""
        if lang not in config.SUPPORTED_LANGUAGES or lang == self.language:
            return False
        self.language = lang
        if self.turn and not self.turn.finished:
            self.turn.language = lang
        await self.send({"type": "language", "language": lang, "reason": reason})
        if self.session is not None:
            try:                                           # the system prompt cannot change mid-session: tell the voice in its own language
                await self.session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=LANGUAGE_LOCK[lang])]), turn_complete=False)
            except Exception as exc:
                await state.log_failure("live_voice", exc, action="language_note_failed", language=lang)
        return True

    # ---------------------------------------------------------------- the notes: written by Gemini itself (show_notes tool call)
    async def _on_tool_call(self, tool_call) -> None:
        replies = []
        for fc in getattr(tool_call, "function_calls", None) or []:
            try:
                result = await self._run_tool(fc)
            except (WebSocketDisconnect, asyncio.CancelledError):
                raise
            except Exception as exc:                       # a bug in our handling must not leave the model waiting forever
                await errors.report("live_tool", exc, action="tool_failed")
                result = {"error": "The notes could not be shown. Just explain the topic aloud."}
            replies.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
        if replies:
            await self.session.send_tool_response(function_responses=replies)

    async def _turn_for_model_output(self) -> Turn:
        """The model started answering a question whose transcript has not arrived yet (spoken question): open its turn now."""
        t = self.turn
        if t is None or t.finished or t.voice_done:
            await self._abandon(t)
            allowed, _ = await usage.check_and_increment(self.email)
            if not allowed:
                await self.send(errors.ws_error("RATE_LIMIT_DAILY", self.language, kind="limit"))
            t = await self._open_turn()
            self.mute_audio = False
        return t

    async def _run_tool(self, fc) -> dict:
        if fc.name != NOTES_TOOL_NAME:
            return {"error": f"unknown tool {fc.name}"}
        t = await self._turn_for_model_output()
        t.last_activity = time.time()
        if t.notes_given:
            return {"result": "The notes are already on the screen. Do not call the tool again: just speak.", **{"next": elaborate_prompt(t.language)}}
        md = str((getattr(fc, "args", None) or {}).get("markdown", "")).strip()
        problem = self._notes_problem(md, t)
        if problem and t.notes_attempts < 1:
            t.notes_attempts += 1                          # the model is told what is wrong and writes them again, in the same turn
            await errors.report("live_notes", problem, code="INTERNAL", action="notes_rejected_regenerate", turn=t.id, language=t.language)
            return {"error": problem + " Call show_notes again now with corrected notes.", "language": LANGUAGE_NAMES.get(t.language)}
        if problem:                                        # second time: show what we have rather than nothing
            await errors.report("live_notes", problem, code="INTERNAL", action="notes_delivered_anyway", turn=t.id, language=t.language)
        t.notes, t.notes_given, t.notes_at = md, True, time.time()
        await self.send({"type": "notes_delta", "text": md})
        await self.send({"type": "notes_done"})
        t.checkpoint_task = asyncio.create_task(self._checkpoint_notes(t))     # saved in the background: never waits for Redis
        self._background(self._judge_in_background(t, md))
        await self._save_turn(t)                           # the chat history has the question and the notes NOW, even if the phone dies mid-voice
        return {"result": elaborate_prompt(t.language)}

    def _notes_problem(self, md: str, t: Turn) -> str:
        """Why these notes cannot be shown (empty, wrong language, leaked instructions) - or '' when they are fine."""
        if len(md) < 15:
            return "The notes were empty or far too short."
        v = judge.local(md, t.language or self.language)
        if not v.ok:
            return " ".join(v.issues).strip() + ". " + v.instruction
        return ""

    async def _checkpoint_notes(self, t: Turn) -> None:
        await state.checkpoint_start(t.id, {"email": self.email, "cid": self.cid or "", "question": t.question or "", "language": t.language,
                                            "user_saved": "1" if t.typed else "0"})
        await state.checkpoint_append(t.id, t.notes)
        await state.checkpoint_set(t.id, notes_complete=1)

    async def _judge_in_background(self, t: Turn, md: str) -> None:
        """The small judge model reads the notes AFTER they are shown (no added latency): quality problems go to the failure log for the operators."""
        if not judge.judge_available():
            return
        v = await judge.judge(t.question or "", md, t.language or self.language, self.p["level"], self.p["subject"], use_model=True)
        if not v.ok:
            await errors.report("judge_reject", "; ".join(v.issues) or "rejected", code="INTERNAL", action="logged_after_display", turn=t.id, source=v.source)

    # ---------------------------------------------------------------- finishing a turn
    async def _maybe_finish(self, turn: Turn) -> None:
        """A turn is finished when the voice is done. Then it is saved and reported."""
        if turn.finished or turn.abandoned or not turn.voice_done:
            return
        turn.finished = True
        if not turn.notes_given:
            await self._safe_send({"type": "notes_none"})  # small talk (or the teacher skipped them): the spoken answer is the text
        await self._persist(turn)
        if self.turn is turn:
            await self._safe_send({"type": "turn_complete"})

    async def _safe_send(self, payload: dict) -> None:
        try:
            await self.send(payload)
        except WebSocketDisconnect:
            pass

    async def _save_turn(self, turn: Turn) -> None:
        """Write the question and the answer (the notes, or for small talk what the teacher said) to the chat history, once."""
        if turn.saved:
            return
        question = turn.question or ""
        answer = turn.notes.strip() or ("" if turn.cut else "".join(turn.spoken).strip())
        if not (question or answer):
            return
        cid = await self._ensure_conversation(question or "Live lesson")
        if not await _db(db.get_conversation, self.email, cid):
            self.cid = None                               # the student deleted this chat while the lesson was open: nothing to save into
            return
        turn.saved = True
        if question and not turn.typed:                   # typed questions were saved when they were sent
            await _db(db.add_message, cid, "user", question)
            turn.typed = True                             # never saved twice
        if answer:
            await _db(db.add_message, cid, "assistant", answer)

    async def _persist(self, turn: Turn) -> None:
        await self._save_turn(turn)
        if turn.checkpoint_task is not None:
            await asyncio.gather(turn.checkpoint_task, return_exceptions=True)      # the checkpoint must exist before it is closed
            await state.checkpoint_finish(turn.id, "done")

    async def _abandon(self, turn: Turn | None) -> None:
        """A new question arrived (or the lesson ended) while this one was still running: keep what exists."""
        if turn is None or turn.finished:
            return
        turn.abandoned = True
        turn.finished = True
        await self._persist(turn)

    # ---------------------------------------------------------------- main
    async def run(self) -> None:
        """Run the lesson. If Gemini's side drops mid-answer (observed: `1006 abnormal closure [internal]`), reconnect,
        tell the teacher what it had already covered and let it carry on, instead of ending the student's lesson."""
        attempt = 0
        while True:
            cfg = build_config(self.p["level"], self.language, self.p["subject"], self.p["voice"])      # the CURRENT language, also after a reconnect
            try:
                await self._session_once(cfg, resumed=attempt > 0)
                return
            except (WebSocketDisconnect, asyncio.CancelledError):
                raise
            except Exception as exc:
                attempt += 1
                await state.log_failure("live_voice", exc, action="reconnect" if attempt <= config.LIVE_RECONNECTS else "give_up", attempt=attempt)
                if attempt > config.LIVE_RECONNECTS:
                    raise
                await self.send({"type": "reconnecting", "attempt": attempt})
                await asyncio.sleep(min(config.LIVE_RECONNECT_DELAY * attempt, 5))

    async def _session_once(self, cfg, resumed: bool) -> None:
        async with _connect(cfg) as session:
            self.session = session
            await self._seed_history(session)
            if resumed:
                await self._resume_turn(session)
            await self.send({"type": "ready", "model": config.LIVE_MODEL, "voice": self.p["voice"], "resumed": resumed, "language": self.language})
            if not resumed:
                await self._resume_open_turn()
            tasks = [asyncio.create_task(self._from_browser()), asyncio.create_task(self._from_gemini()),
                     asyncio.create_task(self._housekeeper())]
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
                + ("The written notes are already on the student's screen, do not call the tool again. " if t.notes_given else "")
                + (f"You had just said: \"...{said}\". " if said else "")
                + f"Please carry on from where you stopped, aloud, in {LANGUAGE_NAMES.get(self.language, 'English')}, in your own words, without mentioning the interruption.)")
        await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=note)]), turn_complete=True)

    async def _resume_open_turn(self) -> None:
        """A worker died (or the server restarted) after the notes of this conversation's last answer were written but before it was saved:
        the notes are in the Redis checkpoint, so replay them to the screen and save them instead of losing them."""
        if not self.cid:
            return
        meta = await state.find_open_turn(self.email, self.cid)
        if not meta or not await state.claim_turn(meta["id"], ttl=120):
            return
        notes = meta.get("text", "")
        if not meta.get("question") or not notes:
            await state.checkpoint_finish(meta["id"], "abandoned")     # nothing worth restoring
            return
        await state.log_failure("resume", "unfinished turn found", turn=meta["id"], action="resumed", written=len(notes))
        t = self.turn = Turn(meta.get("language") or self.language)
        t.id, t.resumed = meta["id"], True
        t.question, t.typed = meta["question"], meta.get("user_saved") == "1"
        t.notes, t.notes_given, t.voice_done = notes, True, True       # the voice of that answer died with the old session
        t.checkpoint_task = asyncio.create_task(asyncio.sleep(0))
        await self.send({"type": "turn_start", "id": t.id, "language": t.language, "resumed": True})
        await self.send({"type": "notes_delta", "text": notes})
        await self.send({"type": "notes_done"})
        await self._maybe_finish(t)

    async def _seed_history(self, session) -> None:
        """Continue an old chat: give the model the last few messages as context (no reply is triggered)."""
        if not self.cid or not await _db(db.get_conversation, self.email, self.cid):
            self.cid = None
            return
        hist = await _db(db.get_messages, self.cid, MAX_CONTEXT_MESSAGES, default=[])
        if hist:
            recap = "Earlier in this conversation:\n" + "\n".join(
                f"{'Student' if m['role'] == 'user' else 'Teacher (written notes)'}: {_text(m['content'], 500)}" for m in hist)
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=recap)]), turn_complete=False)

    async def _housekeeper(self) -> None:
        """Once a second: keep-alive pings (phones and proxies drop silent sockets), the lesson time limit, and the freeze guards."""
        last_ping = time.time()
        while True:
            await asyncio.sleep(config.LIVE_TICK_SECONDS)
            now = time.time()
            if now - self.started >= config.LIVE_MAX_SECONDS:
                await self.send(errors.ws_error("LIVE_TIME_LIMIT", self.language, kind="limit"))
                return
            if now - last_ping >= config.LIVE_PING_SECONDS:
                last_ping = now
                await self.send({"type": "ping"})
                await state.refresh_lesson(self.email)
            await self._check_turn_timeouts(now)

    async def _check_turn_timeouts(self, now: float) -> None:
        """A turn must never stay open forever: if the teacher never starts, or stops without saying 'turn complete', close the turn
        so the next question works. (This was the 'frozen after the first question' bug.)"""
        t = self.turn
        if not t or t.finished or t.voice_done or t.abandoned or not t.question:
            return
        if not t.got_audio:
            if now - max(t.question_at, t.created, t.notes_at) > config.LIVE_FIRST_AUDIO_SECONDS:
                await state.log_failure("live_voice", "teacher did not answer", action="turn_closed", turn=t.id)
                t.silent = True
                t.voice_done = True
                await self.send(errors.ws_error("VOICE_SILENT", self.language, kind="notice"))
                await self._maybe_finish(t)
        elif t.generation_done_at and now - max(t.last_activity, t.generation_done_at) > config.LIVE_GENERATION_GRACE_SECONDS:
            t.voice_done = True                            # generation finished and nothing more came: the normal, quiet way a turn ends here
            await self._maybe_finish(t)
        elif now - t.last_activity > config.LIVE_TURN_IDLE_SECONDS:
            await state.log_failure("live_voice", "teacher went quiet without finishing", action="turn_closed", turn=t.id)
            t.voice_done = True
            await self._maybe_finish(t)

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
                self.ended_by_user = True
                return
            if kind == "ping":
                await self.send({"type": "pong"})
            elif kind == "set_language":
                await self.set_language(str(msg.get("language", "")), "selector")
            elif kind == "mic_stop":
                await self.session.send_realtime_input(audio_stream_end=True)
            elif kind == "interrupt":                       # Stop: silence the teacher now; the notes are already on the screen
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
            await self.send(errors.ws_error("MESSAGE_TOO_LONG" if text else "BAD_REQUEST", self.language, kind="notice"))
            return
        allowed, _ = await usage.check_and_increment(self.email)
        if not allowed:
            await self.send(errors.ws_error("RATE_LIMIT_DAILY", self.language, kind="limit"))
            return
        await self._abandon(self.turn)
        requested = langpolicy.explicit_request(text)       # "explain this in Urdu": the only way the language changes by itself
        if requested:
            await self.set_language(requested, "requested")
        cid = await self._ensure_conversation(text)
        await _db(db.add_message, cid, "user", text)
        t = await self._open_turn()
        t.question, t.typed, t.question_at = text, True, time.time()
        self.mute_audio = False
        await self.session.send_realtime_input(text=text + teach_note(t.language))

    async def _from_gemini(self) -> None:
        empty = 0
        while True:                                        # receive() yields one model turn per call (a tool call ends one, the answer is the next)
            got = False
            async for msg in self.session.receive():
                got = True
                if getattr(msg, "go_away", None):
                    raise ConnectionError("live session is closing (go_away)")
                if getattr(msg, "tool_call", None):
                    await self._on_tool_call(msg.tool_call)
                sc = msg.server_content
                if not sc:
                    continue
                if sc.interrupted:
                    await self._on_interrupted()
                if sc.input_transcription and sc.input_transcription.text:
                    await self._on_heard(sc.input_transcription.text)
                if sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            await self._on_audio(part.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    await self._on_said(sc.output_transcription.text)
                if getattr(sc, "generation_complete", None) or getattr(sc, "waiting_for_input", None):
                    t = self.turn                           # the model has finished generating: "turn complete" may follow late or never
                    if t and not t.finished and t.got_audio and not t.generation_done_at:
                        t.generation_done_at = time.time()
                if sc.turn_complete:
                    await self._on_turn_complete()
            if got:
                empty = 0
            else:                                          # a closed session can make receive() return at once, forever: never spin
                empty += 1
                if empty >= 3:
                    raise ConnectionError("live session returned nothing")
                await asyncio.sleep(0.2)

    async def _on_interrupted(self) -> None:
        """Gemini stopped talking because the student spoke or sent something new."""
        await self.send({"type": "interrupted"})
        t = self.turn
        if t is None or t.finished:
            return
        if t.restarting:                                   # our own language restart: the new answer follows
            t.restarting = False
        elif t.got_audio and not t.voice_done:             # the student barged in on a turn that was speaking: that turn's voice is over
            t.voice_done = True
            await self._maybe_finish(t)
        # (an interruption of a turn that never produced audio belongs to the PREVIOUS answer and is ignored)

    async def _on_turn_complete(self) -> None:
        t = self.turn
        if not t or t.finished or t.voice_done:
            return
        if not t.got_audio and not t.spoken and not t.restarting and not t.cut:
            return        # the "turn complete" that ends the TOOL-CALL turn (the answer comes next), or the late one of an interrupted answer
        t.voice_done = True
        await self._maybe_finish(t)

    async def _on_heard(self, text: str) -> None:
        """The student SPOKE (microphone). Transcription arrives in pieces; the first piece opens a new turn."""
        now = time.time()
        t = self.turn
        # a new question when: nothing is open, the last turn is over, or the teacher has been answering for a while already
        fresh = t is None or t.finished or t.voice_done or (t.got_audio and now - t.audio_at > config.LIVE_LATE_TRANSCRIPT_GRACE)
        if fresh and len(re.sub(r"[\W\d_]+", "", text)) < 2:
            return                                         # a stray letter from room noise, not a question
        if fresh:
            await self._abandon(t)
            allowed, _ = await usage.check_and_increment(self.email)
            if not allowed:
                await self.send(errors.ws_error("RATE_LIMIT_DAILY", self.language, kind="limit"))
                return
            t = await self._open_turn()
            self.mute_audio = False
        if t.typed:
            return
        t.spoken_q.append(text.strip())
        t.question = " ".join(t.spoken_q).strip()
        t.question_at = t.last_activity = now
        await self.send({"type": "caption", "who": "user", "text": text})
        requested = langpolicy.explicit_request(t.question)
        if requested and requested != self.language:
            await self.set_language(requested, "requested")     # "explain this in Urdu", spoken: the notes and the voice both follow

    async def _on_audio(self, data: bytes) -> None:
        t = self.turn
        if t and not t.finished:
            t.last_activity = time.time()
            if not t.got_audio:
                t.got_audio, t.audio_at = True, t.last_activity
                if not t.typed and t.question:
                    await self._ensure_conversation(t.question)   # a spoken question: the chat exists from the first sound
            if t.restarting and time.time() - t.restart_at < 2.5:
                return                                     # the tail of the wrong-language answer that is being replaced
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
            t.last_activity = time.time()
            if await self._guard_voice_language(t):
                return                                     # wrong language: this text is dropped (and the voice restarted), not shown
        if not self.mute_audio:
            await self.send({"type": "caption", "who": "teacher", "text": text})

    async def _guard_voice_language(self, t: Turn) -> bool:
        """If the voice starts in the wrong language (e.g. English although Urdu is selected), stop it and ask again once.
        Returns True when it did (the caller then drops the wrong-language text)."""
        if not config.LIVE_LANGUAGE_GUARD or t.lang_retries >= 1 or t.cut or t.restarting or not t.question or not t.language:
            return False
        said = "".join(t.spoken)
        due = LANG_CHECK_MIN_CHARS if t.lang_checks == 0 else LANG_CHECK_MIN_CHARS * 3
        if len(said) < due:
            return False
        t.lang_checks += 1
        ok, reason = langpolicy.check_language(said, t.language, min_letters=30)
        if ok:
            return False
        t.lang_retries += 1
        t.restarting, t.restart_at = True, time.time()
        t.spoken = []
        await state.log_failure("voice_language", reason, language=t.language, turn=t.id, action="restart_voice")
        await self.send({"type": "interrupted"})           # the browser drops the audio it has queued
        await self.send({"type": "voice_restart"})         # ...and forgets the wrong-language transcript
        await self.session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=LANGUAGE_FIX.get(t.language, LANGUAGE_FIX["en"]) + _text(t.question, 600))]),
            turn_complete=True)
        return True


@router.websocket("/api/live")
async def live_ws(websocket: WebSocket):
    try:
        user = await auth.current_user_ws(websocket)
    except errors.LumaError as exc:          # the database is down: say so instead of closing silently
        await websocket.accept()
        await websocket.send_json(errors.ws_error(exc.code))
        await websocket.close()
        return
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    email = user["email"]
    acquired = False
    lesson: Lesson | None = None
    try:
        if not config.GEMINI_API_KEY:
            await websocket.send_json(errors.ws_error("LIVE_NOT_CONFIGURED"))
            return
        first = await asyncio.wait_for(websocket.receive_json(), 15)
        if first.get("type") != "start":
            await websocket.send_json(errors.ws_error("BAD_REQUEST"))
            return
        level, language = first.get("level", "undergraduate"), first.get("language", "en")
        if level not in config.ACADEMIC_LEVELS or language not in config.SUPPORTED_LANGUAGES:
            await websocket.send_json(errors.ws_error("BAD_REQUEST"))
            return
        gender = "male" if first.get("gender") == "male" else "female"
        params = {"level": level, "language": language, "subject": str(first.get("subject", ""))[:100].strip(), "gender": gender,
                  "voice": pick_voice(first.get("voice"), gender),
                  "conversation_id": str(first.get("conversation_id") or "")[:64] or None}
        # A lesson the student just ended may still be closing its Gemini session: give it a moment before calling it "another tab".
        deadline = time.time() + config.LIVE_START_WAIT
        while True:
            verdict = await state.acquire_lesson(email, config.LIVE_MAX_CONCURRENT)
            if verdict != "duplicate" or time.time() >= deadline:
                break
            await asyncio.sleep(0.1)
        if verdict == "duplicate":
            await websocket.send_json(errors.ws_error("LIVE_DUPLICATE", language))
            return
        if verdict == "busy":
            await websocket.send_json(errors.ws_error("LIVE_BUSY", language))
            return
        acquired = True
        lesson = Lesson(websocket, user, params)
        await lesson.run()
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as exc:
        code = await errors.report("live_session", exc, action="session_failed")      # details stay in the server log
        try:
            await websocket.send_json(errors.ws_error(code if code in ("DB_UNAVAILABLE", "LLM_UNAVAILABLE") else "VOICE_UNAVAILABLE", lesson.language if lesson else "en"))
        except Exception:
            pass
    finally:
        async def cleanup():
            if acquired:
                await state.release_lesson(email)
            if lesson is not None:
                try:
                    await lesson._abandon(lesson.turn)         # don't lose a half-finished turn (the notes are saved with it)
                except Exception as exc:
                    await errors.report("live_session", exc, action="cleanup_failed")

        # Shielded: when the connection is torn down the handler may be cancelled, and the lock, the saved answer and the checkpoint
        # must still be written (a leaked lock would say "already open in another tab" until it expires).
        await asyncio.shield(asyncio.ensure_future(cleanup()))
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

