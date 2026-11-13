"""Single-user Web access with an expiring signed session cookie."""

import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..config import settings

COOKIE = "jy_session"
LIFETIME = 7 * 24 * 3600
PUBLIC = {"/api/health", "/api/auth/status", "/api/auth/login"}
_ATTEMPTS: dict[str, tuple[int, float]] = {}
router = APIRouter(prefix="/api/auth")


def _token() -> str:
    return (settings.fin_access_token or "").strip()


def _equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def _sign(value: str, token: str) -> str:
    return hmac.new(token.encode(), ("jingyuan-session-v2:" + value).encode(), hashlib.sha256).hexdigest()


def _authenticated(request: Request, token: str) -> bool:
    if not token:
        return True
    supplied = request.headers.get("authorization", "")
    if supplied.lower().startswith("bearer ") and _equals(supplied[7:].strip(), token):
        return True
    value, _, signature = (request.cookies.get(COOKIE) or "").rpartition(".")
    expiry, _, nonce = value.partition(".")
    return bool(nonce and expiry.isascii() and expiry.isdecimal() and len(expiry) <= 12
                and int(expiry) >= time.time() and _equals(signature, _sign(value, token)))


class Login(BaseModel):
    token: str = Field(max_length=1024)


@router.get("/status")
async def status(request: Request) -> dict:
    token = _token()
    return {"required": bool(token), "auth_required": bool(token), "authenticated": _authenticated(request, token)}


@router.post("/login")
async def login(body: Login, request: Request, response: Response) -> dict:
    token = _token()
    if not token:
        return {"authenticated": True}
    address = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts, until = _ATTEMPTS.get(address, (0, now + 60))
    if until <= now:
        attempts, until = 0, now + 60
    if attempts >= 12:
        raise HTTPException(429, "尝试次数过多，请稍后再试。", headers={"Retry-After": str(max(1, int(until - now)))})
    if not _equals(body.token.strip(), token):
        if len(_ATTEMPTS) >= 1024:
            for stale, (_, deadline) in list(_ATTEMPTS.items()):
                if deadline <= now:
                    _ATTEMPTS.pop(stale, None)
            if len(_ATTEMPTS) >= 1024:
                _ATTEMPTS.pop(next(iter(_ATTEMPTS)), None)
        _ATTEMPTS[address] = (attempts + 1, until)
        raise HTTPException(401, "访问口令不正确。")
    _ATTEMPTS.pop(address, None)
    value = f"{int(time.time()) + LIFETIME}.{secrets.token_urlsafe(18)}"
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    is_https = request.url.scheme == "https" or forwarded_proto == "https"
    response.set_cookie(COOKIE, value + "." + _sign(value, token), max_age=LIFETIME, httponly=True,
                        samesite="strict", secure=is_https, path="/")
    return {"authenticated": True}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE, path="/")
    return {"authenticated": False}


class WebAccessTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path != "/api" and not path.startswith("/api/"):
            return await call_next(request)
        origin = request.headers.get("origin")
        if origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = {item.strip().rstrip("/") for item in settings.fin_cors_origins.split(",") if item.strip()}
            if request.app.state.dev_cors:
                allowed.update({"http://localhost:5173", "http://127.0.0.1:5173"})
            allowed.add(f"{request.url.scheme}://{request.url.netloc}")
            allowed.add(f"https://{request.url.netloc}")
            if origin.rstrip("/") not in allowed:
                return JSONResponse({"detail": "此操作需要同源请求。"}, status_code=403)
        desktop_shutdown = path == "/api/shutdown" and not settings.fin_web
        if request.method != "OPTIONS" and path not in PUBLIC and not desktop_shutdown and not _authenticated(request, _token()):
            return JSONResponse({"detail": "请输入部署访问口令。", "code": "AUTH_REQUIRED"}, status_code=401,
                                headers={"Cache-Control": "no-store"})
        return await call_next(request)
