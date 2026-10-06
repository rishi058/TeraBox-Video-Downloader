"""Authentication API routes used by the Telegram Mini App."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from .webapp_auth import (
    MAX_INIT_DATA_LENGTH,
    TelegramInitDataError,
    WebAppSession,
    WebAppSessionError,
    create_webapp_session,
    parse_webapp_session,
    validate_telegram_init_data,
)


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["telegram-mini-app"])

SESSION_COOKIE_NAME = "diskwala_webapp_session"
MAX_AUTH_BODY_BYTES = MAX_INIT_DATA_LENGTH + 1_024
AUTH_RATE_LIMIT_WINDOW_SECONDS = 60
AUTH_RATE_LIMIT_REQUESTS = 20
MAX_AUTH_RATE_LIMIT_KEYS = 10_000

_auth_attempts: dict[str, deque[float]] = defaultdict(deque)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _session_secret() -> str:
    secret = os.environ.get("WEBAPP_SESSION_SECRET", "").strip()
    if len(secret) < 32:
        log.error("WEBAPP_SESSION_SECRET is missing or too short.")
        raise HTTPException(status_code=503, detail="Mini App authentication is unavailable.")
    return secret


def _session_ttl() -> int:
    return _env_int("WEBAPP_SESSION_TTL_SECONDS", 3_600, minimum=300, maximum=86_400)


def _init_data_max_age() -> int:
    return _env_int("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", 600, minimum=60, maximum=86_400)


def _cookie_secure() -> bool:
    return _env_bool("WEBAPP_COOKIE_SECURE", True)


def require_same_origin(request: Request) -> None:
    """Reject cross-site browser POSTs (defense-in-depth atop the SameSite=lax cookie)."""
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")


def _rate_limit_auth(request: Request) -> None:
    now = time.monotonic()
    client_key = request.client.host if request.client else "unknown"
    if client_key not in _auth_attempts and len(_auth_attempts) >= MAX_AUTH_RATE_LIMIT_KEYS:
        oldest_key = next(iter(_auth_attempts))
        _auth_attempts.pop(oldest_key, None)
    attempts = _auth_attempts[client_key]
    while attempts and now - attempts[0] > AUTH_RATE_LIMIT_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= AUTH_RATE_LIMIT_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many authentication attempts.")
    attempts.append(now)


async def _read_init_data(request: Request) -> str:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_AUTH_BODY_BYTES:
                raise TelegramInitDataError("invalid initData")
        except ValueError as exc:
            raise TelegramInitDataError("invalid initData") from exc
    body = await request.body()
    if len(body) > MAX_AUTH_BODY_BYTES:
        raise TelegramInitDataError("invalid initData")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramInitDataError("invalid initData") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("init_data"), str):
        raise TelegramInitDataError("invalid initData")
    return payload["init_data"]


def _set_session_cookie(response: Response, token: str, ttl_seconds: int) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=ttl_seconds,
        expires=ttl_seconds,
        path="/api",
        secure=_cookie_secure(),
        httponly=True,
        samesite="lax",
    )


def require_webapp_session(request: Request) -> WebAppSession:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    try:
        return parse_webapp_session(token, _session_secret())
    except WebAppSessionError as exc:
        raise HTTPException(status_code=401, detail="Telegram session is invalid or expired.") from exc


@router.post("/auth")
async def authenticate(request: Request) -> Response:
    require_same_origin(request)
    _rate_limit_auth(request)
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        log.error("BOT_TOKEN is missing; Mini App authentication cannot start.")
        raise HTTPException(status_code=503, detail="Mini App authentication is unavailable.")
    try:
        init_data = await _read_init_data(request)
        verified = validate_telegram_init_data(
            init_data,
            bot_token,
            max_age_seconds=_init_data_max_age(),
        )
        ttl_seconds = _session_ttl()
        token = create_webapp_session(
            verified.user,
            _session_secret(),
            ttl_seconds=ttl_seconds,
        )
    except TelegramInitDataError as exc:
        log.warning("Telegram Mini App authentication rejected: %s", exc)
        raise HTTPException(status_code=403, detail="Telegram authentication failed.") from None
    except ValueError as exc:
        log.error("Telegram Mini App authentication configuration error: %s", exc)
        raise HTTPException(status_code=403, detail="Telegram authentication failed.") from None

    response = JSONResponse({"user": verified.user.to_public_dict(), "expires_in": ttl_seconds})
    response.headers["Cache-Control"] = "no-store"
    _set_session_cookie(response, token, ttl_seconds)
    return response


@router.get("/auth/me")
async def authentication_status(
    session: WebAppSession = Depends(require_webapp_session),
) -> Response:
    response = JSONResponse(
        {
            "user": session.user.to_public_dict(),
            "expires_at": session.expires_at,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    require_same_origin(request)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/api",
        secure=_cookie_secure(),
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response
