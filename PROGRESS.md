# LUMA (Learning & University Mentor Assistant) — Progress Tracker

Tracks the 14-day compressed plan against what's actually done. Updated after every work session.

## Plan vs. Status

| Day | Activity | Status | Notes |
|---|---|---|---|
| 1 | Env setup, API credentials, config, repo | Done | Repo, `.env.example`, config module, Python 3.12 + `.venv` installed |
| 2–4 | AI Tutor integration (Claude), prompt config, streaming, level/language selection, math/code formatting | Working (free demo via Groq) | Provider switch `LLM_PROVIDER=groq|claude`. Groq `openai/gpt-oss-120b` tested OK in English, Arabic, Persian, Urdu. Claude path built (Haiku 4.5 only, 600-token cap) but Anthropic account has no credits yet — flip `LLM_PROVIDER=claude` after buying credits |
| 5–6 | STT/TTS integration (ElevenLabs Scribe + TTS) | Working (free path) | `VOICE_PROVIDER=groq`: mic -> Groq Whisper STT (verified in English via API); read-aloud uses browser speech synthesis (needs an installed OS voice for ur/ar/fa; not browser-tested). ElevenLabs path (`VOICE_PROVIDER=elevenlabs`) is built but the account returns `401 detected_unusual_activity` (needs paid plan) |
| 7 | Multilingual + RTL validation (Urdu, Arabic, Persian) | Mostly done | Urdu renders in real Nastaliq (Noto Nastaliq Urdu), Arabic/Persian in Naskh; language auto-detect; per-language voice. Known gap: numbers/formulas inside RTL Urdu paragraphs can display in the wrong visual order (bidi issue, see Known blockers) |
| 8–9 | Live AI voice agent | Working (own build, free) | Not ElevenLabs' Agent product — built a mic-to-mic loop instead: record -> Whisper STT -> LLM (short spoken summary + full written answer) -> TTS, auto-plays back. `ELEVENLABS_AGENT_ID` still unset/unused |
| 10 | Institutional branding + UI/UX pass | Done | Renamed to LUMA, full custom chat UI (sidebar, history, animations, voice indicators). Real TUF crest (`frontend/assets/`) used as favicon/logo/avatar. Two themes: Default (TUF maroon/terracotta, light) and Dark (original), toggle in sidebar, persisted per browser |
| 11 | Deployment, security, usage/cost controls | Partially started | Google sign-in restricted to `@tuf.edu.pk` (server-verified). Per-user chat history in SQLite. Daily message cap still in-memory (resets on restart). Still local-only, no HTTPS/real host |
| 12 | Internal QA + academic validation | Not started | |
| 13 | Controlled student pilot + fixes | Not started | |
| 14 | Final UAT + Go-Live | Not started | |

## Accounts, history & scope (added 2026-09-25/26/28)

- Google sign-in restricted to `@tuf.edu.pk` (server verifies the Google token, `email_verified`, and `hd`). Signed httponly session cookie, 30 days.
- Chat history per user in SQLite (`data/luma.db`): list, open, rename, delete; grouped by date in the sidebar. Server loads the last 20 messages as model context (no duplication, no reranking needed — it's not RAG).
- Daily message cap and voice endpoints now use the signed-in account (previously a client-supplied ID).
- LUMA is scoped to studying only: off-topic requests (entertainment, personal/political advice, harmful code, prompt-injection attempts) are declined in-language; graded work gets guided help, not full hand-in answers.
- Optional `curriculum.json` (see README) locks the subject list to a real university's actual programs/courses. **Not yet created for TUF** — tuf.edu.pk is behind a Cloudflare bot check that blocked automated fetch; still need the real program/course list from the user (paste, saved page, or department list).
- **Google sign-in not yet live**: needs `GOOGLE_CLIENT_ID` in `.env` (see README). Currently `DEV_LOGIN=1` (domain-checked local sign-in, no Google) — must be set to `0` before any real deployment.

## What exists right now

- `backend/main.py` — FastAPI app: `/api/chat` (streaming), `/api/config`, `/api/conversations` (CRUD), `/api/tts`, `/api/stt`, `/api/auth/*`
- `backend/llm.py` — Claude/Groq integration, level/language/subject-aware + scope-restricted prompts, streaming, truncation-continue support
- `backend/voice.py` — TTS (ElevenLabs for English, native Microsoft neural voices for Urdu/Arabic/Persian, with caching + retry) and STT (Groq Whisper, dual-pass language verification)
- `backend/auth.py`, `backend/db.py` — Google sign-in + SQLite users/conversations/messages
- `backend/curriculum.py` — optional subject/course scoping from `curriculum.json`
- `backend/usage.py` — per-user daily message cap (in-memory)
- `backend/config.py` — env-driven settings
- `frontend/` — LUMA-branded chat UI: sidebar with history/settings, login screen, mic + read-aloud with animations, RTL + Nastaliq/Naskh fonts, Markdown + KaTeX
- Run: `.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000` then open http://localhost:8000

## Known blockers

- **Anthropic account has no credits** (`400 credit balance too low`). Key is saved in `.env`; Claude is unused until credits are bought. Groq free tier (`openai/gpt-oss-120b`) covers the demo meanwhile.
- **ElevenLabs account still failing** (falls back to Microsoft voices automatically) — needs a paid plan/working key before English gets ElevenLabs quality voice.
- **No real TUF curriculum list yet** — `curriculum.json` not created; tuf.edu.pk blocked automated fetch (Cloudflare). LUMA currently covers university subjects in general rather than TUF's exact programs.
- **`GOOGLE_CLIENT_ID` not set** — real Google sign-in untested; site currently runs on the `DEV_LOGIN=1` test bypass only.
- **RTL bidi display bug**: numbers/units inside an Urdu sentence (e.g. "N 200") can render in reversed visual order — a known hard problem mixing LTR numerals with RTL Nastaliq text; not yet fixed.
- **Groq free-tier rate limits**: STT now makes 2 Whisper calls per recording (dual-pass language check); a busy pilot could hit Groq's per-minute cap. Retry/backoff exists but a paid tier removes the ceiling.
- **Daily usage cap resets on restart** (in-memory) — fine for a single dev instance, needs a persistent store before a real multi-user deployment.
- **No HTTPS/real hosting** — still runs locally only, per instruction so far.

## Decisions made

- LLM provider: Anthropic -> Gemini (2026-09-24) -> Claude (2026-09-25), because the Gemini keys were denied (school Workspace account). Claude still blocked on credits, so Groq is the working default.
- **Model restriction for the pilot**: users cannot choose the model. Claude path is fixed server-side to Haiku 4.5 with a 2500-token reply cap (raised from 600 after cut-off answers). Latest/high-tier models are blocked via `ALLOWED_CLAUDE_MODELS` in `backend/config.py`; widen only deliberately.
- Voice: TTS provider is per-language — ElevenLabs (Allison/George) only for English; Urdu/Arabic/Persian always use native Microsoft neural voices, because an English ElevenLabs voice speaking those scripts came out with a distorted, wrong-sounding gender/pitch.
- Voice mode no longer asks the model for a separate short "spoken summary" (`<speak>` tag). It reads the same detailed answer shown on screen, converted to speech sentence-by-sentence as it streams in (not after the full answer finishes) — fixes both "too short" and "waits before speaking". Code/math blocks are held back until they close (no half-spoken formulas) and then skipped in speech; short fragments (list markers, "e.g.") are merged into normal-length chunks so it doesn't fire dozens of tiny choppy requests.
- STT does a dual Whisper pass (user's selected language + auto-detect) and takes whichever is more confident, to stop English being misheard as Urdu and Urdu being misheard as Hindi.
- Compressed the 25-day proposal to 14 days by merging phases and running QA continuously.
- Usage/cost control is an in-memory counter for now; needs a real store before a shared deployment.
- Deployment stays local (same folder) for now, per user instruction.

---
*Last updated: 2026-09-28*
