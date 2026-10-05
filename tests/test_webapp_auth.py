import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from telegram_logic.webapp_auth import (
    TelegramInitDataError,
    TelegramUser,
    WebAppSessionError,
    create_webapp_session,
    parse_webapp_session,
    validate_telegram_init_data,
)


BOT_TOKEN = "123456789:test_bot_token_for_unit_tests"
SESSION_SECRET = "session-secret-that-is-longer-than-thirty-two-characters"
NOW = 1_800_000_000


def build_init_data(*, auth_date=NOW, user=None, extra=None):
    values = {
        "auth_date": str(auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(
            user
            or {
                "id": 42,
                "first_name": "Test",
                "username": "test_user",
                "language_code": "en",
            },
            separators=(",", ":"),
        ),
    }
    values.update(extra or {})
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class TelegramInitDataTests(unittest.TestCase):
    def test_valid_data_returns_signed_user(self):
        result = validate_telegram_init_data(build_init_data(), BOT_TOKEN, now=NOW)
        self.assertEqual(result.user.id, 42)
        self.assertEqual(result.user.username, "test_user")
        self.assertEqual(result.auth_date, NOW)

    def test_tampered_data_is_rejected(self):
        value = build_init_data().replace("Test", "Changed")
        with self.assertRaises(TelegramInitDataError):
            validate_telegram_init_data(value, BOT_TOKEN, now=NOW)

    def test_duplicate_fields_are_rejected(self):
        value = f"{build_init_data()}&auth_date={NOW}"
        with self.assertRaises(TelegramInitDataError):
            validate_telegram_init_data(value, BOT_TOKEN, now=NOW)

    def test_expired_and_future_data_are_rejected(self):
        with self.assertRaises(TelegramInitDataError):
            validate_telegram_init_data(
                build_init_data(auth_date=NOW - 601),
                BOT_TOKEN,
                now=NOW,
                max_age_seconds=600,
            )
        with self.assertRaises(TelegramInitDataError):
            validate_telegram_init_data(build_init_data(auth_date=NOW + 31), BOT_TOKEN, now=NOW)

    def test_invalid_user_is_rejected_after_signature_validation(self):
        with self.assertRaises(TelegramInitDataError):
            validate_telegram_init_data(
                build_init_data(user={"id": -1, "first_name": ""}),
                BOT_TOKEN,
                now=NOW,
            )


class WebAppSessionTests(unittest.TestCase):
    def setUp(self):
        self.user = TelegramUser(id=42, first_name="Test", username="test_user")

    def test_session_round_trip(self):
        token = create_webapp_session(self.user, SESSION_SECRET, ttl_seconds=3_600, now=NOW)
        session = parse_webapp_session(token, SESSION_SECRET, now=NOW + 60)
        self.assertEqual(session.user, self.user)
        self.assertEqual(session.expires_at, NOW + 3_600)

    def test_tampered_and_expired_sessions_are_rejected(self):
        token = create_webapp_session(self.user, SESSION_SECRET, ttl_seconds=300, now=NOW)
        with self.assertRaises(WebAppSessionError):
            parse_webapp_session(f"{token[:-1]}x", SESSION_SECRET, now=NOW)
        with self.assertRaises(WebAppSessionError):
            parse_webapp_session(token, SESSION_SECRET, now=NOW + 300)

if __name__ == "__main__":
    unittest.main()
