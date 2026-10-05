import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_logic.webapp_routes import router
from tests.test_webapp_auth import BOT_TOKEN, build_init_data


class WebAppRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app, base_url="http://testserver")

    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "BOT_TOKEN": BOT_TOKEN,
                "WEBAPP_SESSION_SECRET": "route-test-secret-that-is-definitely-long-enough",
                "WEBAPP_SESSION_TTL_SECONDS": "3600",
                "WEBAPP_COOKIE_SECURE": "false",
                "WEBAPP_ALLOWED_ORIGIN": "http://testserver",
                "TELEGRAM_INIT_DATA_MAX_AGE_SECONDS": "600",
            },
            clear=False,
        )
        self.environment.start()
        self.client.cookies.clear()

    def tearDown(self):
        self.environment.stop()

    def _authenticate(self):
        return self.client.post(
            "/api/auth",
            json={"init_data": build_init_data(auth_date=int(__import__("time").time()))},
            headers={"origin": "http://testserver"},
        )

    def test_authentication_creates_restorable_session(self):
        response = self._authenticate()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies.get("diskwala_webapp_session"))

        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["id"], 42)

    def test_invalid_init_data_returns_generic_forbidden(self):
        response = self.client.post(
            "/api/auth",
            json={"init_data": "auth_date=1&hash=bad"},
            headers={"origin": "http://testserver"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Telegram authentication failed.")

    def test_logout_clears_session(self):
        self.assertEqual(self._authenticate().status_code, 200)
        response = self.client.post(
            "/api/auth/logout",
            headers={"origin": "http://testserver"},
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
