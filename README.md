# LUMA — Learning & University Mentor Assistant (Pilot)

Day 1–2 scaffold: FastAPI backend + Claude (Haiku) streaming chat, static frontend with
level/language selection, LaTeX/Markdown rendering, and RTL support for Urdu/Arabic/Persian.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Set `LLM_PROVIDER=groq` (free tier, needs `GROQ_API_KEY`) or `LLM_PROVIDER=claude` (needs Anthropic credits). Edit `.env` and set `ANTHROPIC_API_KEY` (and ElevenLabs keys once voice work starts).

## Run

```bash
uvicorn backend.main:app --reload --port 8000
```

Open http://localhost:8000

## Status vs. 14-day plan

- [x] Day 1 — environment/config scaffold
- [x] Day 2–4 (partial) — Claude text tutor, streaming, level/language prompts, math/code formatting
- [ ] Day 5–6 — STT/TTS (ElevenLabs)
- [ ] Day 7 — Multilingual/RTL validation pass
- [ ] Day 8–9 — Live voice agent
- [ ] Day 10 — Branding/UI polish
- [ ] Day 11 — HTTPS deployment + usage/cost controls (basic per-user daily cap already in `backend/usage.py`, needs persistent store before real deployment)
- [ ] Day 12–14 — QA, pilot, UAT, go-live

## Notes

- `backend/usage.py` uses an in-memory counter — fine for local dev, replace with Redis/DB before the pilot goes live on a real server (process restarts reset counts).
- The Claude model is fixed server-side to Haiku 4.5 with a 600-token reply cap (pilot cost restriction). See `ALLOWED_CLAUDE_MODELS` in `backend/config.py` to widen it later.

## Sign-in and chat history

Users sign in with Google; only verified `@tuf.edu.pk` accounts are accepted (checked on the server, not just in the page). Each user's chats are stored in `data/luma.db` (SQLite, gitignored) and shown in the sidebar.

**One-time Google setup** (needs a Google Cloud project owned by the university Workspace):
1. https://console.cloud.google.com/apis/credentials -> Create credentials -> OAuth client ID -> Web application.
2. Authorized JavaScript origins: `http://localhost:8000` (and your real https URL when deployed).
3. OAuth consent screen: user type **Internal** (limits sign-in to your Workspace).
4. Put the client ID in `.env` as `GOOGLE_CLIENT_ID=...apps.googleusercontent.com`.
5. Set `DEV_LOGIN=0` (the dev login box skips Google and is for local testing only).

## What LUMA is allowed to teach

LUMA only helps with learning university subjects. Off-topic requests (entertainment, personal advice, politics, harmful code, "ignore your instructions" tricks) are politely declined in the student's language.

To limit it to your university's actual programs, create `curriculum.json` in the project root and restart:

```json
{
  "university": "TUF",
  "programs": [
    {"name": "BS Computer Science", "courses": ["Programming Fundamentals", "Data Structures"]},
    {"name": "BBA", "courses": ["Principles of Marketing", "Financial Accounting"]}
  ],
  "extra_subjects": ["Islamic Studies", "English Composition"]
}
```

With the file present, LUMA teaches only those programs/courses (plus school-level foundations), and the Subject / Course box suggests them. Without it, LUMA covers university-level academic subjects in general.

## Branding & theme

The app ships with The University of Faisalabad's crest (`frontend/assets/`) and two selectable themes:
- **Default** — light theme in TUF's maroon/terracotta brand colors.
- **Dark** — the original dark theme.

The choice is a segmented control under "Settings & voice" in the sidebar, remembered per browser (`localStorage`). To swap in different institutional colors or a different crest, edit the CSS variables at the top of `frontend/style.css` (`:root` = Default, `:root[data-theme="dark"]` = Dark) and replace the files in `frontend/assets/`.
