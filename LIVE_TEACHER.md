# Live teacher (Gemini 3.8 Live voice + LUMA written notes)

A **Live** button in the chat box starts a spoken lesson. The student types or talks. The teacher **explains out loud like a human lecturer** while **detailed written notes stream onto the screen at the same time**. The voice cannot read the notes, because it never sees them.

## How it works
```
                        ┌─► VOICE : Gemini 3.8 Live (pure spoken teacher, no tools, never sees the notes) ──► 24 kHz PCM → browser speakers
student question ──►    │
 (typed or microphone)  └─► NOTES : LUMA's normal text models (Groq / Gemini / Claude chain, same as the chat) ─► Markdown+LaTeX stream → screen,
                                    saved to the chat history                                                      held until the voice starts
 browser <──WebSocket /api/live──► backend/live.py        (server-to-server: the Gemini key never reaches the browser)
```
**Why two independent channels?** In the first version one model wrote the notes (as a tool call) and then spoke. With the notes in its context it paraphrased them, and in Urdu it was effectively reading the book (25% of spoken sentences mirrored a notes sentence). Now the voice only receives the question, so it has nothing to read. Measured on the real service (shared 4-word runs / sentences mirroring the notes): Urdu 8.4% / 25% → **0.9% / 0%**; English ~2% / 0%.

## What the teacher voice is told (backend/live.py)
- It is a gifted university teacher; the notes exist but **it cannot see them**; no textbook summary, no definition lists, no reading headings/code/formulas.
- Teach like a person: start from what the student knows, a story or picture, 1-2 analogies, ONE complete worked example with numbers, the common mistake, why it matters, what is next; vary rhythm, natural fillers, no "great question".
- At least ~250 words (about two minutes) for a real concept; greetings and clarifying questions may be short; end with a check question.
- **Language style:** Urdu = *bolchaal* Urdu of a friendly Pakistani teacher with natural English technical terms (function, base case...), no bookish words, examples from daily life (chai, cricket, rickshaw...), **Urdu script only, never Roman Urdu**. Arabic and Persian have spoken-register instructions too. Each typed question also carries a short "teacher note" in the student's own language (never saved to history): an English note made the Urdu voice switch to Roman Urdu.
- LUMA's existing rules are included: course scope, "built by the Enterprise Solutions Team of The University of Faisalabad" identity, level / language / subject.

## Voices
All 30 Gemini prebuilt voices were accepted by `gemini-3.8-live` in testing. Settings → **Live teacher voice** lists them (recommended teaching voices first: Sulafat (Warm, default for Female), Charon (Informative, default for Male), Kore, Aoede, Leda, Despina, Erinome, Gacrux, Orus, Achird, Sadaltager, Puck, Zephyr...). The choice is remembered per browser and applies from the next lesson. Defaults: `GEMINI_LIVE_VOICE_FEMALE` / `GEMINI_LIVE_VOICE_MALE`.

## Notes writer
`stream_tutor_reply(..., notes_mode=True)` in `llm.py`: the normal provider chain with LUMA's scope rules plus a "write the study notes" instruction (title, sections, definitions, steps, worked example, LaTeX, code, mistakes, summary). Greetings, thanks and out-of-scope messages return `[[LUMA_NO_NOTES]]`, so the teacher just answers by voice. Notes start streaming at once but are **held until the first voice chunk** (max 8 s) so text and voice start together (`LIVE_SYNC_NOTES=0` to show immediately).

## Behaviour and safeguards (all in `tests/test_live.py`, 34 tests)
- Login required; one lesson per student (a lesson you just ended is given a few seconds to close before "already open"); `LIVE_MAX_CONCURRENT` lessons overall; `LIVE_MAX_SECONDS` per lesson; daily message cap shared with the text chat.
- Spoken questions: the transcription becomes the question; the notes start when the voice starts.
- **Stop** silences the voice instantly and interrupts the model on the server; the notes keep streaming and are saved; the "Okay" is not saved.
- A new question cancels the previous answer's notes (what exists is saved).
- **Reconnect:** if Gemini drops the session mid-answer (`1006 abnormal closure` happens), the backend reconnects up to `LIVE_RECONNECTS` times and tells the teacher what it had covered.
- Opening an old chat in Live mode re-seeds the last messages as context (only the student's own conversations).
- Errors shown to students are generic; details stay in the server log; the API key is never logged or sent to the browser.

## Settings (.env, all optional)
| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | - | required (already used by the text chat); the Live button only appears when set |
| `GEMINI_LIVE_MODEL` | `gemini-3.8-live` | Live model |
| `GEMINI_LIVE_VOICE_FEMALE` / `_MALE` | `Sulafat` / `Charon` | default voices |
| `LIVE_MAX_SECONDS` / `LIVE_MAX_CONCURRENT` | 1200 / 4 | lesson length / simultaneous lessons (cost guard) |
| `LIVE_SYNC_NOTES` | 1 | hold notes until the voice starts |
| `LIVE_RECONNECTS` | 2 | automatic reconnects per lesson |
| `LIVE_START_WAIT` | 4 | seconds a new lesson waits for the previous one to close |

## Testing
```powershell
# side-by-side test stack: UI http://localhost:8081, API :8001, own empty database; never touches the running LUMA containers
docker compose -p luma_test -f docker-compose.test.yml up -d --build
# backend tests (fake Gemini session built from the SDK's real message types + fake notes writer)
docker run --rm -v "${PWD}\tests:/app/tests:ro" luma_test-luma_backend sh -c "uv pip install --system -q pytest && python -m pytest tests -q"
# tear down (also deletes the test database)
docker compose -p luma_test -f docker-compose.test.yml down -v
```
Deploy to the real containers: `docker compose up -d --build` (a few seconds of downtime; chat history in `./data` is kept).

## Static-file caching fix (affects every deployment)
`nginx.conf` used to serve JS/CSS as `immutable` for one year, so browsers never fetched a new `app.js` after a deployment. JS/CSS are now revalidated on every load and `index.html` references `app.js?v=...` / `style.css?v=...`; bump the `?v=` value when you change them, to reach browsers that still hold an old copy.

## Known limits
- Needs internet and a Gemini key with Live API access; each lesson is billed by Google (audio in/out) and each question also uses one text-model call for the notes.
- The microphone path (AudioWorklet, 16 kHz, browser echo cancellation) was not exercised end to end because the test environment has no microphone; use headphones to avoid the teacher being picked up by the mic.
- Voice quality is Gemini's native speech. It is not ChatGPT's or Claude's voice (OpenAI's needs an OpenAI key and Claude has no speech API); pick a different voice in Settings to taste.
- The voice and the notes are generated independently, so they cover the same topic but can occasionally differ in detail.
