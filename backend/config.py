import os
from dotenv import load_dotenv

load_dotenv()

# ---------- Accounts & storage ----------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
ALLOWED_EMAIL_DOMAIN = os.getenv("ALLOWED_EMAIL_DOMAIN", "tuf.edu.pk").strip().lower()
DEV_LOGIN = os.getenv("DEV_LOGIN", "0") == "1"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
DB_PATH = os.getenv("DB_PATH", os.path.join("data", "luma.db"))

def _load_secret() -> str:
    key = os.getenv("SECRET_KEY", "").strip()
    if key: return key
    path = os.path.join(os.path.dirname(DB_PATH) or ".", "secret.key")
    if os.path.exists(path): return open(path, encoding="utf-8").read().strip()
    import secrets
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f: f.write(key)
    return key

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
MAX_TOKENS_PER_RESPONSE = int(os.getenv("MAX_TOKENS_PER_RESPONSE", "2500"))
GROQ_MAX_TOKENS_PER_RESPONSE = int(os.getenv("GROQ_MAX_TOKENS_PER_RESPONSE", "1200"))
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
# Hold the written notes until the teacher's voice starts so text and voice appear together (set 0 to show notes immediately).
LIVE_SYNC_NOTES = os.getenv("LIVE_SYNC_NOTES", "1") == "1"
LIVE_START_WAIT = float(os.getenv("LIVE_START_WAIT", "4"))          # seconds a new lesson waits for the student's previous one to close
LIVE_RECONNECTS = int(os.getenv("LIVE_RECONNECTS", "2"))        # automatic reconnects per lesson when Gemini drops the session

SUPPORTED_LANGUAGES = ["en", "ur", "ar", "fa", "de"]
RTL_LANGUAGES = {"ur", "ar", "fa"}
ACADEMIC_LEVELS = ["foundation", "undergraduate", "postgraduate"]