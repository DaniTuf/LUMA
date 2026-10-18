# LUMA errors: what students see, what it means, what to do

Every failure has a **code** (defined in [`backend/errors.py`](backend/errors.py), the single source of truth). Students only ever see the short, kind message of that code in their own language (English, Urdu, Arabic, Persian, German): never a stack trace, a SQL error, a provider name or a key. Operators get the full detail in the logs and in the Redis failure stream.

## Where to look

| What | How |
|---|---|
| A student reports a problem | Ask for the **request id** (`X-Request-ID` header / `request_id` in the error JSON) and search the backend log: `docker logs luma_api | grep <id>` |
| Latest failures with the action taken | `GET /api/admin/failures` (accounts listed in `ADMIN_EMAILS`) or `redis-cli XREVRANGE luma:failures + - COUNT 20` |
| Failure counts per component today | `redis-cli HGETALL luma:failure_counts:<YYYY-MM-DD>` |
| Is everything up? | `GET /api/health` (database + pool, Redis, Gemini key, judge) |

Each failure-log entry has: `component` (llm, judge, live_voice, live_session, notes_worker, db, http, chat, startup...), `code`, `error` (scrubbed: keys and tokens are replaced by `[redacted]`), `action` (what was done about it) and `request`.

Actions you will see: `retry_same_provider`, `failover`, `continue_on_next_provider` (the next model continued the half-written answer), `fail_open` (judge down, answer released), `regenerate` (judge rejected the start), `delivered_anyway`, `reconnect` / `give_up` (Gemini Live), `turn_closed` (teacher silent), `degraded` (database down: the lesson goes on without saving), `resumed` (an unfinished answer was continued after a crash), `restart_voice` (voice started in the wrong language).

## Error codes

JSON shape (HTTP and WebSocket): `{"code": "...", "message": "<student message>", "retryable": true|false, "request_id": "..."}`. HTTP bodies also keep `detail` (same text) for older clients.

| Code | HTTP | Retry? | Meaning | What to do |
|---|---|---|---|---|
| `AUTH_REQUIRED` | 401 | no | No valid session cookie | Student signs in again |
| `AUTH_EXPIRED` | 401 | no | Session older than `SESSION_DAYS` | Sign in again |
| `FORBIDDEN` | 403 | no | Not allowed (e.g. admin endpoint without being in `ADMIN_EMAILS`) | Check `ADMIN_EMAILS` |
| `NOT_FOUND` | 404 | no | Chat does not exist or belongs to someone else | Nothing; it is deliberately the same answer for both |
| `BAD_REQUEST` | 400 / 422 | no | Invalid level, language, empty message, bad JSON | Client bug if it happens from the real UI |
| `MESSAGE_TOO_LONG` | 400 | no | More than 4000 characters | Student shortens the message |
| `RATE_LIMIT_DAILY` | 429 | no | `MAX_MESSAGES_PER_USER_PER_DAY` reached | Raise the limit or wait for tomorrow (counter lives in Redis) |
| `RATE_LIMIT_PREVIEW` | 429 | yes | Too many voice previews this hour | Wait |
| `LLM_UNAVAILABLE` | 503 | yes | Every text model failed after retries and failover | Look for `component=llm` entries: usually keys, quota or the network to the provider |
| `NOTES_INCOMPLETE` | 200 | yes | Written notes could not be completed during a live lesson; the voice transcript is shown as the text | Same as `LLM_UNAVAILABLE` |
| `VOICE_UNAVAILABLE` | 503 | yes | Gemini Live could not connect / dropped and the reconnect budget (`LIVE_RECONNECTS`) ran out | Check outbound access to `generativelanguage.googleapis.com` (DNS, IPv6, firewall), `GEMINI_API_KEY`, quota |
| `VOICE_SILENT` | 200 | yes | The teacher did not answer within `LIVE_FIRST_AUDIO_SECONDS`; the turn was closed so the next question works | Student asks again |
| `LIVE_NOT_CONFIGURED` | 503 | no | No `GEMINI_API_KEY` | Set it |
| `LIVE_BUSY` | 503 | yes | `LIVE_MAX_CONCURRENT` lessons are already running | Wait, or raise the limit (cost!) |
| `LIVE_DUPLICATE` | 409 | no | The student already has a lesson open (another tab) | Close the other tab |
| `LIVE_TIME_LIMIT` | 200 | no | Lesson reached `LIVE_MAX_SECONDS` | Start a new lesson |
| `DB_UNAVAILABLE` | 503 | yes | PostgreSQL unreachable, pool exhausted, or timed out (after `DB_RETRIES` retries) | `docker logs luma_postgres`; check `/api/health` pool numbers; see "Database" below |
| `INTERNAL` | 500 | yes | A bug. The full traceback is in the log under the request id | Report it with the request id |

## Database (PostgreSQL) with several workers

* Each worker process has its own pool: total connections = `WEB_CONCURRENCY x (DB_POOL_SIZE + DB_MAX_OVERFLOW)` (default 2 x 10 = 20; Postgres allows 100). Raise `max_connections` or use PgBouncer (`DB_PGBOUNCER=1`) before adding workers.
* `pool_pre_ping` + `pool_recycle` replace dead connections after a Postgres restart; transient errors are retried `DB_RETRIES` times; every statement has a server timeout (`DB_STATEMENT_TIMEOUT_MS`).
* A database outage never breaks a lesson: the voice keeps working, history/saving is skipped and `component=db action=degraded` is logged.
* Schema creation at start-up is serialised with an advisory lock; connections show up in `pg_stat_activity` as `luma-<pid>` so you can see which worker holds what.
* If the app logs `pool exhausted` / `DB_UNAVAILABLE` under load: raise `DB_POOL_SIZE` a little, or lower `WEB_CONCURRENCY`, or add PgBouncer. Never raise them so far that workers x pool exceeds `max_connections`.
