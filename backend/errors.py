"""One place that defines every error LUMA can show, how an exception becomes one of them, and how it is reported.

 * CATALOG       - stable error CODES with HTTP status, "can the student just retry?", and a short, kind message in all five languages.
                   Students only ever see these messages: never a stack trace, a provider name, a SQL error or a key.
 * classify()    - turns any exception (database, Redis, Gemini, HTTP, timeout, our own) into a code.
 * report()      - the single way to record a failure: log line with code + request id, entry in the Redis failure stream.
 * install(app)  - request ids (X-Request-ID), JSON error bodies, and handlers so no endpoint can leak a traceback.
 * ws_error()    - the same, for WebSocket messages ({"type":"error","code":...,"message":...}).
The human-readable list of codes and what to do about each is in ERRORS.md (kept in sync by tests/test_errors.py).
"""
import asyncio
import logging
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import state

log = logging.getLogger("luma.errors")
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# code: (http status, retryable by the student, {language: message})
CATALOG: dict[str, tuple[int, bool, dict[str, str]]] = {
    "AUTH_REQUIRED": (401, False, {
        "en": "Please sign in to continue.", "ur": "جاری رکھنے کے لیے براہ کرم سائن اِن کریں۔", "ar": "يرجى تسجيل الدخول للمتابعة.",
        "fa": "برای ادامه لطفاً وارد شوید.", "de": "Bitte melde dich an, um fortzufahren."}),
    "AUTH_EXPIRED": (401, False, {
        "en": "Your session expired. Please sign in again.", "ur": "آپ کا سیشن ختم ہو گیا۔ براہ کرم دوبارہ سائن اِن کریں۔",
        "ar": "انتهت جلستك. يرجى تسجيل الدخول مرة أخرى.", "fa": "نشست شما منقضی شد. لطفاً دوباره وارد شوید.",
        "de": "Deine Sitzung ist abgelaufen. Bitte melde dich erneut an."}),
    "FORBIDDEN": (403, False, {
        "en": "You are not allowed to do that.", "ur": "آپ کو اس کی اجازت نہیں ہے۔", "ar": "غير مسموح لك بذلك.",
        "fa": "شما اجازهٔ این کار را ندارید.", "de": "Das ist dir nicht erlaubt."}),
    "NOT_FOUND": (404, False, {
        "en": "That item was not found.", "ur": "یہ چیز نہیں ملی۔", "ar": "لم يتم العثور على العنصر.", "fa": "مورد پیدا نشد.",
        "de": "Dieser Eintrag wurde nicht gefunden."}),
    "BAD_REQUEST": (400, False, {
        "en": "That request was not valid. Please check it and try again.", "ur": "یہ درخواست درست نہیں تھی۔ براہ کرم جانچ کر دوبارہ کوشش کریں۔",
        "ar": "الطلب غير صالح. يرجى التحقق والمحاولة مرة أخرى.", "fa": "درخواست معتبر نبود. لطفاً بررسی کنید و دوباره تلاش کنید.",
        "de": "Die Anfrage war ungültig. Bitte prüfe sie und versuche es erneut."}),
    "MESSAGE_TOO_LONG": (400, False, {
        "en": "Your message is too long (maximum 4000 characters).", "ur": "آپ کا پیغام بہت لمبا ہے (زیادہ سے زیادہ 4000 حروف)۔",
        "ar": "رسالتك طويلة جدًا (الحد الأقصى 4000 حرف).", "fa": "پیام شما بیش از حد طولانی است (حداکثر ۴۰۰۰ نویسه).",
        "de": "Deine Nachricht ist zu lang (höchstens 4000 Zeichen)."}),
    "RATE_LIMIT_DAILY": (429, False, {
        "en": "You reached today's message limit. Please come back tomorrow.", "ur": "آپ آج کے پیغامات کی حد تک پہنچ گئے ہیں۔ براہ کرم کل دوبارہ آئیں۔",
        "ar": "وصلت إلى حد الرسائل اليومي. يرجى العودة غدًا.", "fa": "به سقف پیام‌های امروز رسیدید. لطفاً فردا برگردید.",
        "de": "Du hast das heutige Nachrichtenlimit erreicht. Bitte komm morgen wieder."}),
    "RATE_LIMIT_PREVIEW": (429, True, {
        "en": "Too many previews. Please wait a little.", "ur": "بہت زیادہ پیش نظارے۔ براہ کرم تھوڑا انتظار کریں۔",
        "ar": "معاينات كثيرة جدًا. يرجى الانتظار قليلًا.", "fa": "پیش‌نمایش‌های زیاد. لطفاً کمی صبر کنید.", "de": "Zu viele Vorschauen. Bitte warte kurz."}),
    "LLM_UNAVAILABLE": (503, True, {
        "en": "The tutor is temporarily unavailable. Please try again in a moment.", "ur": "ٹیوٹر عارضی طور پر دستیاب نہیں۔ براہ کرم تھوڑی دیر بعد کوشش کریں۔",
        "ar": "المعلّم غير متاح مؤقتًا. يرجى المحاولة بعد قليل.", "fa": "معلم موقتاً در دسترس نیست. لطفاً کمی بعد دوباره تلاش کنید.",
        "de": "Der Tutor ist vorübergehend nicht erreichbar. Bitte versuche es gleich noch einmal."}),
    "NOTES_INCOMPLETE": (200, True, {
        "en": "The written notes could not be completed. The teacher's explanation is shown instead.",
        "ur": "تحریری نوٹس مکمل نہ ہو سکے۔ اس کی جگہ استاد کی وضاحت دکھائی گئی ہے۔",
        "ar": "تعذّر إكمال الملاحظات المكتوبة. تم عرض شرح المعلّم بدلًا منها.",
        "fa": "یادداشت‌های نوشتاری کامل نشد. به جای آن توضیح معلم نمایش داده شد.",
        "de": "Die schriftlichen Notizen konnten nicht fertiggestellt werden. Stattdessen wird die Erklärung der Lehrkraft angezeigt."}),
    "VOICE_UNAVAILABLE": (503, True, {
        "en": "The live teacher's voice could not connect. Please try again in a moment.", "ur": "لائیو استاد کی آواز سے رابطہ نہ ہو سکا۔ براہ کرم تھوڑی دیر بعد کوشش کریں۔",
        "ar": "تعذّر الاتصال بصوت المعلّم المباشر. يرجى المحاولة بعد قليل.", "fa": "اتصال به صدای معلم زنده برقرار نشد. لطفاً کمی بعد تلاش کنید.",
        "de": "Die Stimme der Live-Lehrkraft konnte nicht verbunden werden. Bitte versuche es gleich noch einmal."}),
    "VOICE_SILENT": (200, True, {
        "en": "The teacher did not answer this time. Please ask again.", "ur": "اس بار استاد نے جواب نہیں دیا۔ براہ کرم دوبارہ پوچھیں۔",
        "ar": "لم يجب المعلّم هذه المرة. يرجى السؤال مرة أخرى.", "fa": "این بار معلم پاسخ نداد. لطفاً دوباره بپرسید.",
        "de": "Die Lehrkraft hat diesmal nicht geantwortet. Bitte frage noch einmal."}),
    "LIVE_NOT_CONFIGURED": (503, False, {
        "en": "The live teacher is not set up on this server.", "ur": "اس سرور پر لائیو استاد سیٹ اپ نہیں ہے۔", "ar": "المعلّم المباشر غير مُعدّ على هذا الخادم.",
        "fa": "معلم زنده روی این سرور راه‌اندازی نشده است.", "de": "Die Live-Lehrkraft ist auf diesem Server nicht eingerichtet."}),
    "LIVE_BUSY": (503, True, {
        "en": "The live teacher is busy. Please try again in a few minutes.", "ur": "لائیو استاد مصروف ہے۔ براہ کرم چند منٹ بعد کوشش کریں۔",
        "ar": "المعلّم المباشر مشغول. يرجى المحاولة بعد دقائق.", "fa": "معلم زنده مشغول است. لطفاً چند دقیقه بعد تلاش کنید.",
        "de": "Die Live-Lehrkraft ist ausgelastet. Bitte versuche es in ein paar Minuten erneut."}),
    "LIVE_DUPLICATE": (409, False, {
        "en": "You already have a live lesson open in another tab.", "ur": "آپ کا ایک لائیو سبق پہلے ہی دوسرے ٹیب میں کھلا ہے۔",
        "ar": "لديك درس مباشر مفتوح بالفعل في علامة تبويب أخرى.", "fa": "یک درس زنده از قبل در برگه دیگری باز است.",
        "de": "Du hast bereits eine Live-Lektion in einem anderen Tab geöffnet."}),
    "LIVE_TIME_LIMIT": (200, False, {
        "en": "This live lesson reached its time limit. Start a new one to continue.", "ur": "اس لائیو سبق کا وقت ختم ہو گیا۔ جاری رکھنے کے لیے نیا شروع کریں۔",
        "ar": "انتهى وقت هذا الدرس المباشر. ابدأ درسًا جديدًا للمتابعة.", "fa": "زمان این درس زنده تمام شد. برای ادامه درس جدیدی شروع کنید.",
        "de": "Diese Live-Lektion hat ihr Zeitlimit erreicht. Starte eine neue, um fortzufahren."}),
    "DB_UNAVAILABLE": (503, True, {
        "en": "Your chats cannot be saved or loaded right now. Please try again in a moment.", "ur": "ابھی آپ کی چیٹس محفوظ یا لوڈ نہیں ہو سکتیں۔ براہ کرم تھوڑی دیر بعد کوشش کریں۔",
        "ar": "تعذّر حفظ محادثاتك أو تحميلها الآن. يرجى المحاولة بعد قليل.", "fa": "اکنون ذخیره یا بارگذاری گفتگوها ممکن نیست. لطفاً کمی بعد تلاش کنید.",
        "de": "Deine Chats können gerade nicht gespeichert oder geladen werden. Bitte versuche es gleich noch einmal."}),
    "INTERNAL": (500, True, {
        "en": "Something went wrong on our side. Please try again.", "ur": "ہماری طرف سے کچھ خرابی ہوئی۔ براہ کرم دوبارہ کوشش کریں۔",
        "ar": "حدث خطأ من جانبنا. يرجى المحاولة مرة أخرى.", "fa": "مشکلی از سمت ما رخ داد. لطفاً دوباره تلاش کنید.",
        "de": "Auf unserer Seite ist etwas schiefgelaufen. Bitte versuche es erneut."}),
}


class LumaError(Exception):
    """Raise this (or use the helpers below) for an error the student should see. `detail` stays in the logs."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code if code in CATALOG else "INTERNAL"
        self.detail = detail

    @property
    def status(self) -> int:
        return CATALOG[self.code][0]


def message(code: str, language: str = "en") -> str:
    msgs = CATALOG.get(code, CATALOG["INTERNAL"])[2]
    return msgs.get(language) or msgs["en"]


def status_for(code: str) -> int:
    return CATALOG.get(code, CATALOG["INTERNAL"])[0]


def retryable(code: str) -> bool:
    return CATALOG.get(code, CATALOG["INTERNAL"])[1]


def classify(exc: BaseException) -> str:
    """Map any exception to an error code, by type name so this module imports none of the heavy libraries."""
    if isinstance(exc, LumaError):
        return exc.code
    names = {c.__name__ for c in type(exc).__mro__}
    mod = type(exc).__module__ or ""
    if names & {"DatabaseUnavailable", "SQLAlchemyError", "DBAPIError", "OperationalError", "InterfaceError", "PostgresError"} or mod.startswith(("sqlalchemy", "asyncpg", "sqlite3", "aiosqlite")):
        return "DB_UNAVAILABLE"
    if "LLMUnavailable" in names:
        return "LLM_UNAVAILABLE"
    if mod.startswith(("google.genai", "websockets")) or "APIError" in names or "ConnectionClosed" in names:
        return "VOICE_UNAVAILABLE"
    if mod.startswith(("openai", "anthropic", "httpx", "aiohttp")) or isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return "LLM_UNAVAILABLE"
    return "INTERNAL"


async def report(component: str, exc: BaseException | str, *, code: str | None = None, **ctx) -> str:
    """Record a failure (log line + Redis failure stream) and return its code. Never raises."""
    code = code or (classify(exc) if isinstance(exc, BaseException) else "INTERNAL")
    try:
        await state.log_failure(component, exc if not isinstance(exc, BaseException) else f"{type(exc).__name__}: {exc}", code=code,
                                request=request_id_var.get(), **ctx)
    except Exception:
        log.exception("could not record failure")
    return code


def payload(code: str, language: str = "en", **extra) -> dict:
    return {"code": code, "message": message(code, language), "retryable": retryable(code), "request_id": request_id_var.get(), **extra}


def ws_error(code: str, language: str = "en", kind: str = "error") -> dict:
    """WebSocket frame: {"type":"error","code","message","retryable"}; the browser shows `message` as is."""
    return {"type": kind, **payload(code, language)}


# ---------------------------------------------------------------------------------------------------- FastAPI wiring
_HTTP_TO_CODE = {400: "BAD_REQUEST", 401: "AUTH_REQUIRED", 403: "FORBIDDEN", 404: "NOT_FOUND", 429: "RATE_LIMIT_DAILY", 503: "LLM_UNAVAILABLE"}


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = (request.headers.get("x-request-id") or uuid.uuid4().hex[:12])[:64]
        request.state.rid = rid
        token = request_id_var.set(rid[:64])
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid[:64]
        return response

    def body(code: str, request: Request, detail: str | None = None) -> dict:
        request_id_var.set(getattr(request.state, "rid", "-"))      # the middleware's context is already gone when an outer handler runs
        lang = request.headers.get("x-language", "en")
        p = payload(code, lang)
        return {"detail": detail or p["message"], "error": p}              # `detail` keeps older clients working

    @app.exception_handler(LumaError)
    async def luma_handler(request: Request, exc: LumaError):
        await report("http", exc, code=exc.code, path=request.url.path)
        return JSONResponse(body(exc.code, request), status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def http_handler(request: Request, exc: HTTPException):
        code = _HTTP_TO_CODE.get(exc.status_code, "INTERNAL" if exc.status_code >= 500 else "BAD_REQUEST")
        if exc.status_code == 401 and "expired" in str(exc.detail).lower():
            code = "AUTH_EXPIRED"
        if exc.status_code >= 500:
            await report("http", f"HTTP {exc.status_code}: {exc.detail}", code=code, path=request.url.path)
        # explicit, already user-facing details written by us are kept; the structured error rides along
        return JSONResponse(body(code, request, str(exc.detail)), status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(body("BAD_REQUEST", request), status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        request_id_var.set(getattr(request.state, "rid", "-"))
        code = await report("http", exc, path=request.url.path)
        log.error("unhandled error on %s (request %s): %s: %s", request.url.path, request_id_var.get(), type(exc).__name__, str(exc)[:300])
        return JSONResponse(body(code, request), status_code=status_for(code), headers={"X-Request-ID": request_id_var.get()})

