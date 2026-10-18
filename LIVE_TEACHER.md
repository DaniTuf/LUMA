# Live teacher (Gemini 3.8 Live writes the notes AND teaches them)

A **Live** button in the chat box starts a spoken lesson. The student types or talks. **One model, Gemini 3.8 Live, does everything**: in a single conversation turn it first *writes* the study notes, then *elaborates on them aloud*. The screen and the voice always talk about the same thing, in the same language, because nothing else generates text.

## How one answer works
```
student question (typed or spoken)
   │
   ▼
Gemini 3.8 Live  (one WebSocket session, server-to-server; the API key never reaches the browser)
   │  turn 1: calls the tool  show_notes(markdown)      ── the notes it WROTE (4-8 s)
   │            └─► server: language/leak check ─► screen (notes appear) ─► chat history (saved at once)
   │            └─► tool response = "now elaborate: pick the most important part of your notes, don't read them"
   │  turn 2: SPEAKS the elaboration (story, worked example, the trap, why it matters)  ──► 24 kHz audio ─► browser speakers
   ▼
transcript of what it said (collapsed under the answer)
```
Small talk and out-of-scope requests: the model does **not** call the tool and just answers by voice; the spoken answer is shown as the text.

This is the "write it, then elaborate on part of it" prompt technique: the system prompt (`TEACHER_INSTRUCTION` in `backend/live.py`) defines the two steps and the tool result repeats the second step at the exact moment the notes are on screen. Verified on the real service: notes arrive after about 2-8 s, the voice starts about a second later, the voice is 240-300 words per answer, English / Urdu / German all kept to the lesson language, notes complete (never cut off).

## Guardrails around the one model
* **Language**: the interface language decides what is written and spoken (`backend/langpolicy.py`); it changes only on the selector or an explicit request ("explain this in Urdu"). Notes written in the wrong language or containing leaked instructions are **rejected back to the model in the tool response** and it rewrites them in the same turn (one retry, then they are shown anyway and logged). If the voice starts in the wrong language it is restarted once.
* **Judge**: the small judge model reads the notes *after* they are displayed (no added latency); problems go to the failure log.
* **Never frozen**: each turn is closed by the server itself (generation-complete, idle, silent-teacher timers), browser and server ping each other, a dropped Gemini session is reconnected (`LIVE_RECONNECTS`), a lost browser keeps the already-saved question and notes.
* **Saved immediately**: the question and the notes go to PostgreSQL the moment the notes are accepted (and a Redis checkpoint covers a worker dying before that), so a phone dying mid-voice never loses the answer.
* Errors shown to students are generic and translated (see `ERRORS.md`).

## Voices
All 30 Gemini prebuilt voices work. Settings → **Live teacher voice** lists them (recommended teaching voices first). Defaults: `GEMINI_LIVE_VOICE_FEMALE` / `GEMINI_LIVE_VOICE_MALE`.

## Settings (.env, all optional)
See `.env.example`: `GEMINI_LIVE_MODEL`, `LIVE_MAX_SECONDS`, `LIVE_MAX_CONCURRENT`, `LIVE_RECONNECTS`, `LIVE_LANGUAGE_GUARD`, `LIVE_FIRST_AUDIO_SECONDS`, `LIVE_TURN_IDLE_SECONDS`, `LIVE_GENERATION_GRACE_SECONDS`, `LIVE_LATE_TRANSCRIPT_GRACE`, `LIVE_PING_SECONDS`.

The normal (non-live) chat still uses the text models (Gemini / Groq / Claude with retry, failover and the judge) and the answer-length caps `GROQ_MAX_TOKENS_PER_RESPONSE` / `MAX_TOKENS_PER_RESPONSE` (raised to 4000 / 4096: 1200 used to cut long and Urdu answers in half).
