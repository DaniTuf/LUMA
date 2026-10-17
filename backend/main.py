from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import auth, config, curriculum, db, live, usage, voice
from .llm import NO_NOTES_MARK, TRUNCATED_MARK, stream_tutor_reply
from .router import router
import asyncio
import json

app = FastAPI(title="LUMA - Learning & University Mentor Assistant")
app.include_router(auth.router)
app.include_router(live.router)

HISTORY_TURNS = 20 

@app.on_event("startup")
async def _warm_voices():
    asyncio.create_task(voice.warm_up())

@app.middleware("http")
async def no_cache(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response

class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    level: str = "undergraduate"
    subject: str = ""
    voice_mode: bool = False
    notes_mode: bool = False      # written notes only: the spoken lesson comes separately from /api/lesson
    language: str = "en"

CACHE_GENERATION = "3"      # bump to make every browser drop its cached copy of the app once


@app.get("/api/config")
def get_public_config(request: Request, response: Response):
    # Browsers that still hold the old page/app.js (it used to be cached for a year) flush their cache ONCE, via this call.
    if request.cookies.get("luma_cv") != CACHE_GENERATION:
        response.headers["Clear-Site-Data"] = '"cache"'
        response.set_cookie("luma_cv", CACHE_GENERATION, max_age=365 * 86400, samesite="lax")
    return {
        "languages": config.SUPPORTED_LANGUAGES,
        "rtl_languages": list(config.RTL_LANGUAGES),
        "levels": config.ACADEMIC_LEVELS,
        "max_messages_per_day": config.MAX_MESSAGES_PER_USER_PER_DAY,
        "voice_provider": config.VOICE_PROVIDER,
        "tts_provider": config.TTS_PROVIDER,
        "google_client_id": config.GOOGLE_CLIENT_ID,
        "allowed_email_domain": config.ALLOWED_EMAIL_DOMAIN,
        "dev_login": config.DEV_LOGIN,
        "live_enabled": bool(config.GEMINI_API_KEY),     # the Live teacher button only appears when a Gemini key is configured
        "live_max_seconds": config.LIVE_MAX_SECONDS,
        "live_voices": [{"id": k, "style": v} for k, v in live.VOICES.items()],
        "live_default_voices": {"female": config.LIVE_VOICE_FEMALE, "male": config.LIVE_VOICE_MALE},
        "subjects": curriculum.subject_suggestions(),
    }

@app.get("/api/conversations")
def list_conversations(user: dict = Depends(auth.current_user)):
    return db.list_conversations(user["email"])

@app.get("/api/conversations/{cid}")
def get_conversation(cid: str, user: dict = Depends(auth.current_user)):
    conv = db.get_conversation(user["email"], cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return {**conv, "messages": db.get_messages(cid)}

class RenameRequest(BaseModel):
    title: str

@app.patch("/api/conversations/{cid}")
def rename_conversation(cid: str, body: RenameRequest, user: dict = Depends(auth.current_user)):
    title = body.title.strip()[:120]
    if not title:
        raise HTTPException(400, "Title cannot be empty")
    if not db.rename_conversation(user["email"], cid, title):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}

@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, user: dict = Depends(auth.current_user)):
    if not db.delete_conversation(user["email"], cid):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}

def _clean_reply(text: str) -> str:
    return text.replace(TRUNCATED_MARK, "").strip()

def _make_title(message: str) -> str:
    title = " ".join(message.split())
    return (title[:57] + "…") if len(title) > 58 else title

@app.post("/api/chat")
async def chat(req: ChatRequest, user: dict = Depends(auth.current_user)):
    if req.level not in config.ACADEMIC_LEVELS:
        raise HTTPException(400, f"Invalid level: {req.level}")
    if req.language not in config.SUPPORTED_LANGUAGES:
        raise HTTPException(400, f"Unsupported language: {req.language}")

    message = req.message.strip()
    if not message:
        raise HTTPException(400, "Message is empty")
    if len(message) > 4000:
        raise HTTPException(400, "Message is too long (max 4000 characters)")

    email = user["email"]

    if req.conversation_id:
        if not db.get_conversation(email, req.conversation_id):
            raise HTTPException(404, "Conversation not found")
        cid = req.conversation_id
    else:
        cid = None

    allowed, _remaining = usage.check_and_increment(email)
    if not allowed:
        raise HTTPException(429, "Daily message limit reached for this pilot account.")

    provider, wait_time = router.get_provider()

    if cid is None:
        cid = db.create_conversation(email, _make_title(message))

    history = db.get_messages(cid, limit=HISTORY_TURNS)
    db.add_message(cid, "user", message)

        # Inside @app.post("/api/chat")
    async def event_stream():
        collected = ""
        failed = False
        try:
            async for piece in stream_tutor_reply(
                message, req.level, req.language, history, req.subject.strip()[:100], req.voice_mode, req.notes_mode
            ):
                collected += piece
                yield piece
        except Exception as exc:
            failed = True
            yield f"\n\n[Error generating response: {exc}]"
        finally:
            reply = _clean_reply(collected)
            if reply and not failed and not reply.startswith(NO_NOTES_MARK):
                # FIX Issue #7: Run DB write in a separate thread to prevent asyncio blocking
                await asyncio.to_thread(db.add_message, cid, "assistant", reply)

    return StreamingResponse(
        event_stream(),
        media_type="text/plain",
        headers={
            "X-Conversation-Id": cid,
            "Access-Control-Expose-Headers": "X-Conversation-Id",
            "X-Accel-Buffering": "no",
        },
    )

@app.post("/api/lesson")
async def lesson(req: ChatRequest, user: dict = Depends(auth.current_user)):
    """The spoken explanation for the normal voice (same teacher style as Live). Streamed as plain text for text-to-speech;
    not saved and not counted: the written notes request (/api/chat) is what is saved and counted."""
    if req.level not in config.ACADEMIC_LEVELS or req.language not in config.SUPPORTED_LANGUAGES:
        raise HTTPException(400, "Invalid level or language")
    message = req.message.strip()
    if not message or len(message) > 4000:
        raise HTTPException(400, "Message is empty or too long")
    history = []
    if req.conversation_id and db.get_conversation(user["email"], req.conversation_id):
        history = db.get_messages(req.conversation_id, limit=HISTORY_TURNS)
    history = [m for m in history if not str(m.get("content", "")).startswith(NO_NOTES_MARK)]

    async def spoken():
        async for piece in stream_tutor_reply(message, req.level, req.language, history, req.subject.strip()[:100],
                                              True, False, True):
            yield piece

    return StreamingResponse(spoken(), media_type="text/plain", headers={"X-Accel-Buffering": "no"})


class TTSRequest(BaseModel):
    text: str
    gender: str = "female"
    language: str = "en"
    voice: str | None = None

@app.post("/api/tts")
async def tts(req: TTSRequest, user: dict = Depends(auth.current_user)):
    try:
        audio = await voice.text_to_speech(req.text[:2500], req.gender, req.language, req.voice)
    except Exception as exc:
        raise HTTPException(502, f"TTS failed: {exc}")
    return Response(audio, media_type="audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg")

@app.post("/api/stt")
async def stt(file: UploadFile = File(...), language: str = Form(""), user: dict = Depends(auth.current_user)):
    try:
        return await voice.speech_to_text(
            await file.read(), file.filename or "audio.webm", file.content_type or "audio/webm", language or None
        )
    except Exception as exc:
        raise HTTPException(502, f"STT failed: {exc}")


@app.websocket("/api/stt/stream")
async def stt_stream(websocket: WebSocket):
    """Live speech-to-text: the client streams MediaRecorder chunks over this socket as
    they're produced. Each new chunk is appended to the running buffer and re-transcribed
    with the fast single-pass path, and the result goes back as a partial caption while the
    user is still talking. On {"type": "stop"} the full, more accurate speech_to_text() runs
    once on the whole recording and the result goes back marked final."""
    user = auth.current_user_ws(websocket)
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()

    chunks: list[bytes] = []
    language_hint: str | None = None
    busy = False
    dirty = False

    async def send(payload: dict) -> bool:
        try:
            await websocket.send_json(payload)
            return True
        except Exception:
            return False

    async def transcribe(final: bool) -> None:
        audio = b"".join(chunks)
        if not audio:
            if final:
                await send({"final": True, "text": "", "language": language_hint or "en"})
            return
        try:
            if final:
                result = await voice.speech_to_text(audio, "audio.webm", "audio/webm", language_hint)
            else:
                result = await voice.speech_to_text_partial(audio, "audio.webm", "audio/webm", language_hint)
            await send({"final": final, **result})
        except Exception as exc:
            await send({"final": final, "error": str(exc)})

    async def run_partial_passes() -> None:
        # Debounced: if more chunks arrive while a pass is running, just rerun once more on
        # whatever the buffer grew to, instead of queuing a pass per chunk.
        nonlocal busy, dirty
        busy = True
        try:
            while True:
                dirty = False
                await transcribe(False)
                if not dirty:
                    break
        finally:
            busy = False

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data_bytes = message.get("bytes")
            data_text = message.get("text")
            if data_bytes is not None:
                chunks.append(data_bytes)
                if busy:
                    dirty = True
                else:
                    asyncio.create_task(run_partial_passes())
            elif data_text is not None:
                try:
                    payload = json.loads(data_text)
                except ValueError:
                    continue
                if payload.get("type") == "language":
                    language_hint = payload.get("language") or None
                elif payload.get("type") == "stop":
                    await transcribe(True)
                    break
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")