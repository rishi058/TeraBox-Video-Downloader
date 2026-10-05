"""Telegram Mini App initData and stateless session validation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qsl


MAX_INIT_DATA_LENGTH = 8_192
MAX_INIT_DATA_FIELDS = 32
MAX_SESSION_TOKEN_LENGTH = 4_096
FUTURE_CLOCK_SKEW_SECONDS = 30


class TelegramInitDataError(ValueError):
    """Raised when Telegram initData is missing, malformed, stale, or invalid."""


class WebAppSessionError(ValueError):
    """Raised when a web-app session token cannot be trusted."""


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "first_name": self.first_name,
            "is_premium": self.is_premium,
        }
        for key in ("last_name", "username", "language_code"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True)
class VerifiedInitData:
    user: TelegramUser
    auth_date: int
    query_id: str | None
    start_param: str | None


@dataclass(frozen=True)
class WebAppSession:
    user: TelegramUser
    issued_at: int
    expires_at: int


def _required_secret(value: str, label: str, minimum_length: int) -> bytes:
    secret = value.strip()
    if len(secret) < minimum_length:
        raise ValueError(f"{label} must contain at least {minimum_length} characters")
    return secret.encode("utf-8")


def _optional_text(value: object, *, maximum_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TelegramInitDataError("invalid Telegram user")
    value = value.strip()
    if not value or len(value) > maximum_length:
        raise TelegramInitDataError("invalid Telegram user")
    return value


def _parse_telegram_user(value: object) -> TelegramUser:
    if not isinstance(value, Mapping):
        raise TelegramInitDataError("invalid Telegram user")

    user_id = value.get("id")
    first_name = value.get("first_name")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not isinstance(first_name, str)
        or not first_name.strip()
        or len(first_name.strip()) > 256
    ):
        raise TelegramInitDataError("invalid Telegram user")

    is_premium = value.get("is_premium", False)
    if not isinstance(is_premium, bool):
        is_premium = False

    return TelegramUser(
        id=user_id,
        first_name=first_name.strip(),
        last_name=_optional_text(value.get("last_name"), maximum_length=256),
        username=_optional_text(value.get("username"), maximum_length=64),
        language_code=_optional_text(value.get("language_code"), maximum_length=16),
        is_premium=is_premium,
    )


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 600,
    now: int | None = None,
) -> VerifiedInitData:
    """Validate Telegram's HMAC signature, timestamp, and signed user payload."""

    if not isinstance(init_data, str) or not init_data or len(init_data) > MAX_INIT_DATA_LENGTH:
        raise TelegramInitDataError("invalid initData")
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")

    token = _required_secret(bot_token, "BOT_TOKEN", 8)
    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=MAX_INIT_DATA_FIELDS,
        )
    except ValueError as exc:
        raise TelegramInitDataError("invalid initData") from exc

    params: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in params:
            raise TelegramInitDataError("invalid initData")
        params[key] = value

    received_hash = params.pop("hash", "")
    if len(received_hash) != 64:
        raise TelegramInitDataError("invalid initData")
    try:
        int(received_hash, 16)
    except ValueError as exc:
        raise TelegramInitDataError("invalid initData") from exc

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(params.items())
    )
    secret_key = hmac.new(b"WebAppData", token, hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash.lower()):
        raise TelegramInitDataError("invalid initData")

    try:
        auth_date = int(params["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TelegramInitDataError("invalid initData") from exc

    current_time = int(time.time()) if now is None else int(now)
    if (
        auth_date <= 0
        or auth_date > current_time + FUTURE_CLOCK_SKEW_SECONDS
        or current_time - auth_date > max_age_seconds
    ):
        raise TelegramInitDataError("expired initData")

    try:
        raw_user = json.loads(params["user"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TelegramInitDataError("invalid initData") from exc

    return VerifiedInitData(
        user=_parse_telegram_user(raw_user),
        auth_date=auth_date,
        query_id=params.get("query_id") or None,
        start_param=params.get("start_param") or None,
    )


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise WebAppSessionError("invalid session")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise WebAppSessionError("invalid session") from exc


def create_webapp_session(
    user: TelegramUser,
    secret: str,
    *,
    ttl_seconds: int = 3_600,
    now: int | None = None,
) -> str:
    """Create a compact signed session token; its JSON payload is not encrypted."""

    key = _required_secret(secret, "WEBAPP_SESSION_SECRET", 32)
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "v": 1,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "user": user.to_public_dict(),
    }
    encoded_payload = _base64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _base64url_encode(
        hmac.new(key, encoded_payload.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded_payload}.{signature}"


def parse_webapp_session(
    token: str,
    secret: str,
    *,
    now: int | None = None,
) -> WebAppSession:
    """Verify and decode a server-issued Mini App session token."""

    if not isinstance(token, str) or not token or len(token) > MAX_SESSION_TOKEN_LENGTH:
        raise WebAppSessionError("invalid session")
    key = _required_secret(secret, "WEBAPP_SESSION_SECRET", 32)

    try:
        encoded_payload, received_signature = token.split(".", 1)
    except ValueError as exc:
        raise WebAppSessionError("invalid session") from exc
    expected_signature = _base64url_encode(
        hmac.new(key, encoded_payload.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected_signature, received_signature):
        raise WebAppSessionError("invalid session")

    try:
        payload = json.loads(_base64url_decode(encoded_payload))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebAppSessionError("invalid session") from exc
    if not isinstance(payload, Mapping) or payload.get("v") != 1:
        raise WebAppSessionError("invalid session")

    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
    ):
        raise WebAppSessionError("invalid session")

    current_time = int(time.time()) if now is None else int(now)
    if issued_at > current_time + FUTURE_CLOCK_SKEW_SECONDS or expires_at <= current_time:
        raise WebAppSessionError("expired session")
    if expires_at <= issued_at:
        raise WebAppSessionError("invalid session")

    try:
        user = _parse_telegram_user(payload.get("user"))
    except TelegramInitDataError as exc:
        raise WebAppSessionError("invalid session") from exc
    return WebAppSession(user=user, issued_at=issued_at, expires_at=expires_at)

