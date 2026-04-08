import re
import random
import requests
import os 
from dotenv import load_dotenv
load_dotenv()

BASE_DOMAIN = "dm.1024tera.com"
BASE_URL = f"https://{BASE_DOMAIN}"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
]

BYTES_PER_MB = 1048576

# ── Cookie Helper ─────────────────────────────────────────────────────────

CookiesList = []

def load_cookies_from_env():
    CookiesList.clear()
    # assume upto 10 cookies are in env
    for idx in range(1, 10):
        try:
            cookie = os.getenv(f"COOKIES{idx}")
            if cookie:
                CookiesList.append(cookie)
        except Exception as e:
            break

load_cookies_from_env()  

# ── Custom Exceptions ─────────────────────────────────────────────────────────
class TeraBoxError(Exception):
    """Raised for known, expected TeraBox errors."""


class CancelledError(Exception):
    """Raised when a download is cancelled."""

# ── Internal Helpers ──────────────────────────────────────────────────────────
def _logid() -> str:
    return str(random.randint(400_000_000_000_000_000, 999_999_999_999_999_999))


def _cookie_str(session: requests.Session) -> str:
    return "; ".join(
        f"{c.name}={c.value}" for c in session.cookies
        if "1024tera" in (c.domain or "")
    )


def _headers(session: requests.Session, surl: str = "") -> dict:
    hdrs = {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": f"{BASE_URL}/wap/share/filelist?surl={surl}" if surl else f"{BASE_URL}/wap/share/filelist",
    }
    cookie_str = _cookie_str(session)
    if cookie_str:
        hdrs["Cookie"] = cookie_str
    return hdrs

def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)
