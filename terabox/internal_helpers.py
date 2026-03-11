import re
import random
import requests

BASE_DOMAIN = "dm.1024tera.com"
BASE_URL = f"https://{BASE_DOMAIN}"

UA = (
    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Mobile Safari/537.36"
)

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
    return {
        "User-Agent": UA,
        "Cookie": _cookie_str(session),
        "Referer": f"{BASE_URL}/wap/share/filelist?surl={surl}" if surl else BASE_URL,
    }


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)
