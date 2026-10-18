import os
from dotenv import load_dotenv

load_dotenv()

# ---------- Accounts & storage ----------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
ALLOWED_EMAIL_DOMAIN = os.getenv("ALLOWED_EMAIL_DOMAIN", "tuf.edu.pk").strip().lower()
DEV_LOGIN = os.getenv("DEV_LOGIN", "0") == "1"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
# ---------- PostgreSQL (async SQLAlchemy 2.0 + asyncpg) ----------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://luma:luma@localhost:5432/luma").strip()
# connections per WORKER process. The database must allow  workers x (DB_POOL_SIZE + DB_MAX_OVERFLOW)  (+ a few for admin tools).
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "5"))
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "10"))            # seconds to wait for a free pooled connection
DB_POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))            # replace connections older than this (firewalls / failover)
DB_CONNECT_TIMEOUT = float(os.getenv("DB_CONNECT_TIMEOUT", "10"))
DB_COMMAND_TIMEOUT = float(os.getenv("DB_COMMAND_TIMEOUT", "15"))
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "10000"))
DB_IDLE_TX_TIMEOUT_MS = int(os.getenv("DB_IDLE_TX_TIMEOUT_MS", "15000"))
DB_RETRIES = int(os.getenv("DB_RETRIES", "2"))                         # retries of a transient database error (restart, dropped connection, deadlock)
DB_RETRY_BASE_SECONDS = float(os.getenv("DB_RETRY_BASE_SECONDS", "0.2"))
DB_PGBOUNCER = os.getenv("DB_PGBOUNCER", "0").strip().lower() in ("1", "true", "yes", "on")   # PgBouncer in transaction mode in front of Postgres

def _load_secret() -> str:
    """Session-signing key. Every worker must use the SAME key, so set SECRET_KEY in the environment. Without it the key is generated once into a
    file with an exclusive create, so workers starting together all end up reading the one winner's key."""
    key = os.getenv("SECRET_KEY", "").strip()
    if key: return key
    path = os.path.join(os.getenv("DATA_DIR", "data"), "secret.key")
    if os.path.exists(path): return open(path, encoding="utf-8").read().strip()
    import secrets
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as f:
            key = secrets.token_hex(32)
            f.write(key)
        return key
    except FileExistsError:
        import time
        for _ in range(50):                                            # the winner may still be writing it
            val = open(path, encoding="utf-8").read().strip()
            if len(val) >= 32: return val
            time.sleep(0.05)
        return val

SECRET_KEY = _load_secret()

# ---------- LLM PROVIDERS ----------
# Primary: Gemini 3.5 Flash
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Fallback 1: Groq (Fixed valid model name)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile") # Fixed: openai/gpt-oss-120b does not exist
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Fallback 2: Claude (Fixed valid model name)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-fable-5-1") # Fixed: claude-haiku-4-5... does not exist

# Voice Providers
VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "groq").lower()
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "edge").lower()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID_FEMALE = os.getenv("ELEVENLABS_VOICE_ID_FEMALE", "xctasi8XvGp2cVO9HL9k")
ELEVENLABS_VOICE_ID_MALE = os.getenv("ELEVENLABS_VOICE_ID_MALE", "JBFqnCBsd6RMkjVDRZzb")

# Limits
MAX_MESSAGES_PER_USER_PER_DAY = int(os.getenv("MAX_MESSAGES_PER_USER_PER_DAY", "50"))
MAX_TOKENS_PER_RESPONSE = int(os.getenv("MAX_TOKENS_PER_RESPONSE", "4096"))
GROQ_MAX_TOKENS_PER_RESPONSE = int(os.getenv("GROQ_MAX_TOKENS_PER_RESPONSE", "4000"))   # 1200 cut long (and Urdu/Arabic) answers in half
GROQ_TPM_LIMIT = int(os.getenv("GROQ_TPM_LIMIT", "8000"))

# ---------- Live teacher (Gemini Live, server-to-server) ----------
LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
GEMINI_TTS = os.getenv("GEMINI_TTS", "1") != "0"      # normal (non-Live) voice uses the same Gemini voices as the Live teacher
GEMINI_TTS_ENGINE = os.getenv("GEMINI_TTS_ENGINE", "live")   # "live" = speak with gemini-3.8-live itself, "tts" = the faster gemini-3.8-flash-tts
GEMINI_TTS_MODEL =os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
# Default Gemini prebuilt voices for the Female / Male setting (the student can pick any of the 30 in Settings).
LIVE_VOICE_FEMALE = os.getenv("GEMINI_LIVE_VOICE_FEMALE", "Sulafat").strip()
LIVE_VOICE_MALE = os.getenv("GEMINI_LIVE_VOICE_MALE", "Charon").strip()
LIVE_MAX_SECONDS = int(os.getenv("LIVE_MAX_SECONDS", "1200"))      # one lesson lasts at most 20 minutes
LIVE_MAX_CONCURRENT = int(os.getenv("LIVE_MAX_CONCURRENT", "4"))   # cost / quota guard across all students
LIVE_START_WAIT = float(os.getenv("LIVE_START_WAIT", "4"))          # seconds a new lesson waits for the student's previous one to close
LIVE_RECONNECTS = int(os.getenv("LIVE_RECONNECTS", "4"))        # automatic reconnects per lesson when Gemini drops the session (flaky networks need more than 2)

def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")

# Freeze protection: a turn whose voice never finishes (or never starts) is closed after this long so the next question works.
LIVE_FIRST_AUDIO_SECONDS = float(os.getenv("LIVE_FIRST_AUDIO_SECONDS", "25"))   # question sent, teacher silent
LIVE_TURN_IDLE_SECONDS = float(os.getenv("LIVE_TURN_IDLE_SECONDS", "10"))       # teacher started, then went quiet without "turn complete"
LIVE_GENERATION_GRACE_SECONDS = float(os.getenv("LIVE_GENERATION_GRACE_SECONDS", "1.5"))   # after "generation complete", wait this long for "turn complete"
LIVE_LATE_TRANSCRIPT_GRACE = float(os.getenv("LIVE_LATE_TRANSCRIPT_GRACE", "2.0"))  # speech heard this long after the teacher began = a new question
LIVE_PING_SECONDS = float(os.getenv("LIVE_PING_SECONDS", "15"))                 # server keep-alive to the browser (phones, proxies)
LIVE_TICK_SECONDS = float(os.getenv("LIVE_TICK_SECONDS", "1"))                 # housekeeping interval (pings, freeze guards)
LIVE_RECONNECT_DELAY = float(os.getenv("LIVE_RECONNECT_DELAY", "1"))            # base delay before reconnecting to Gemini
LIVE_LANGUAGE_GUARD =_flag("LIVE_LANGUAGE_GUARD", "1")                         # restart the voice once if it speaks the wrong language

# ---------- Redis (shared state: usage cap, provider cool-downs, turn checkpoints, failure log, lesson locks) ----------
# Empty REDIS_URL = in-memory fallback (single process, fine for local development only).
REDIS_URL = os.getenv("REDIS_URL", "").strip()
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "luma").strip() or "luma"
REDIS_TIMEOUT = float(os.getenv("REDIS_TIMEOUT", "1.5"))              # seconds per Redis call before falling back to memory
CHECKPOINT_TTL_SECONDS = int(os.getenv("CHECKPOINT_TTL_SECONDS", "3600"))
FAILURE_LOG_MAX = int(os.getenv("FAILURE_LOG_MAX", "5000"))
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

# ---------- Retry / failover ----------
LLM_RETRIES = int(os.getenv("LLM_RETRIES", "1"))                      # extra tries on the SAME provider for a transient error before failing over
LLM_RETRY_BASE_SECONDS = float(os.getenv("LLM_RETRY_BASE_SECONDS", "0.4"))
LLM_FIRST_TOKEN_TIMEOUT = float(os.getenv("LLM_FIRST_TOKEN_TIMEOUT", "12"))   # no first token by then = provider failed, fail over

# ---------- LLM judge (small model checks language + quality BEFORE the first words reach the student) ----------
JUDGE_ENABLED = _flag("JUDGE_ENABLED", "1")
JUDGE_PROVIDER = os.getenv("JUDGE_PROVIDER", "gemini").lower()        # gemini | groq
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "").strip()                    # empty = GEMINI_MODEL
JUDGE_GROQ_MODEL = os.getenv("JUDGE_GROQ_MODEL", "llama-3.1-8b-instant")
JUDGE_TIMEOUT_SECONDS = float(os.getenv("JUDGE_TIMEOUT_SECONDS", "3"))  # judge too slow = fail open (the answer is released)
JUDGE_HEAD_CHARS = int(os.getenv("JUDGE_HEAD_CHARS", "280"))          # how much of the answer is held and judged before release
JUDGE_MAX_RETRIES = int(os.getenv("JUDGE_MAX_RETRIES", "1"))          # regenerations with the judge's instructions
JUDGE_FAIL_POLICY = os.getenv("JUDGE_FAIL_POLICY", "deliver").lower() # deliver | block : when retries are used up and it still fails

SUPPORTED_LANGUAGES = ["en", "ur", "ar", "fa", "de"]
RTL_LANGUAGES = {"ur", "ar", "fa"}
ACADEMIC_LEVELS = ["foundation", "undergraduate", "postgraduate"]