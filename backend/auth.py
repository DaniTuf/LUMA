"""Google sign-in restricted to the university domain, with signed session cookies."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from . import config, db, errors

router = APIRouter(prefix="/api/auth")

COOKIE = "luma_session"
_signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="luma-session")


async def _lookup(email: str) -> dict | None:
    """The signed-in student, or None. A database outage is reported as 503 (not as "please sign in"): the session itself is fine."""
    if not is_allowed_email(email):
        return None
    try:
        return await db.get_user(email)
    except db.DatabaseUnavailable as exc:
        raise errors.LumaError("DB_UNAVAILABLE", str(exc))


def is_allowed_email(email: str) -> bool:
    email = (email or "").strip().lower()
    parts = email.split("@")
    return len(parts) == 2 and bool(parts[0]) and parts[1] == config.ALLOWED_EMAIL_DOMAIN


def _start_session(response: Response, request: Request, email: str) -> None:
    response.set_cookie(
        COOKIE,
        _signer.dumps({"e": email}),
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


async def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Please sign in.")
    try:
        data = _signer.loads(token, max_age=config.SESSION_DAYS * 86400)
    except BadSignature:
        raise HTTPException(401, "Session expired. Please sign in again.")
    email = data.get("e", "")
    user = await _lookup(email)
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


async def current_user_ws(websocket) -> dict | None:
    """Same session-cookie check as current_user(), but for a WebSocket connection.

    A WebSocket can't be answered with an HTTPException before it's accepted, so this
    returns None on any failure and the caller is responsible for closing the socket."""
    token = websocket.cookies.get(COOKIE)
    if not token:
        return None
    try:
        data = _signer.loads(token, max_age=config.SESSION_DAYS * 86400)
    except BadSignature:
        return None
    email = data.get("e", "")
    return await _lookup(email)


class GoogleLogin(BaseModel):
    credential: str


def _verify_google(credential: str) -> dict:
    from google.auth.transport import requests as grequests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(credential, grequests.Request(), config.GOOGLE_CLIENT_ID)


@router.post("/google")
async def login_google(body: GoogleLogin, request: Request, response: Response):
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in is not configured on the server.")
    try:
        info = await asyncio.to_thread(_verify_google, body.credential)
    except Exception:
        raise HTTPException(401, "Google sign-in could not be verified.")

    email = (info.get("email") or "").lower()
    # Verified email, on the allowed domain, and issued for that Workspace domain
    if not info.get("email_verified") or not is_allowed_email(email) or info.get("hd") != config.ALLOWED_EMAIL_DOMAIN:
        raise HTTPException(403, f"Only @{config.ALLOWED_EMAIL_DOMAIN} accounts can sign in.")

    user = await db.upsert_user(email, info.get("name") or email.split("@")[0], info.get("picture") or "")
    _start_session(response, request, email)
    return user


class DevLogin(BaseModel):
    email: str


@router.post("/dev")
async def login_dev(body: DevLogin, request: Request, response: Response):
    """Local development only (DEV_LOGIN=1): sign in without Google, still limited to the domain."""
    if not config.DEV_LOGIN:
        raise HTTPException(404, "Not found")
    email = body.email.strip().lower()
    if not is_allowed_email(email):
        raise HTTPException(403, f"Only @{config.ALLOWED_EMAIL_DOMAIN} accounts can sign in.")
    user = await db.upsert_user(email, email.split("@")[0].replace(".", " ").title(), "")
    _start_session(response, request, email)
    return user


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return user


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}

