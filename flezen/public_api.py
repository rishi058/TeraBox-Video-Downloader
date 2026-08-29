"""
flezen/public_api.py
~~~~~~~~~~~~~~~~~~~~~
Client for the Flezen scraper proxy.

The proxy exposes a single POST endpoint that, given a Flezen share URL,
resolves it to a direct downloadable video URL plus file metadata.

    POST <FLEZEN_PROXY_URL>
    Headers : x-api-key: <FLEZEN_API_KEY>
    Body    : {"url": "https://flezen.com/s/<share-token>"}

Success response (200):
    {
        "time_taken_in_sec": 2.419,
        "n": "1778286021.mp4",
        "s": 10573313,
        "v": 1,
        "u": "https://dl.fzcdn.cloud/cf/...bin",
        "t": "video",
        "ua": "Aug 17, 2026",
        "ub": "Premium seller "
    }
"""
import os
import re
import logging

import requests

log = logging.getLogger(__name__)

FLEZEN_PROXY_URL = os.getenv("FLEZEN_PROXY_URL")
FLEZEN_API_KEY = os.getenv("FLEZEN_API_KEY")

# Flezen share links embed a token after /s/, e.g.
# https://flezen.com/s/da1895hbjlnspd09usgyrtxdsjxuis
_LINK_ID_RE = re.compile(r"/s/([A-Za-z0-9]{8,})")

# A full Flezen URL sitting anywhere inside a block of text.
FLEZEN_URL_RE = re.compile(r"https?://\S*flezen\.com/s/[A-Za-z0-9]+\S*", re.IGNORECASE)


class FlezenError(Exception):
    """Raised when the Flezen proxy fails or returns unusable data."""


def extract_flezen_id(text: str) -> str | None:
    """Return the Flezen share token found in `text`, or None."""
    m = _LINK_ID_RE.search(text or "")
    return m.group(1) if m else None


def extract_all_flezen_urls(text: str) -> list[str]:
    """Extract all unique Flezen URLs (that carry a share token) from `text`."""
    seen: set[str] = set()
    urls: list[str] = []
    for m in FLEZEN_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(").,]}\"'")  # trim trailing punctuation
        if url not in seen and extract_flezen_id(url):
            seen.add(url)
            urls.append(url)
    return urls


def _extract_error_detail(resp: "requests.Response") -> str:
    """Pull a human-readable message out of a non-200 proxy response."""
    try:
        detail = resp.json().get("detail")
    except Exception:
        return (resp.text or "")[:200] or f"HTTP {resp.status_code}"
    if isinstance(detail, list):
        # FastAPI validation error: list of {loc, msg, ...}
        return "; ".join(str(d.get("msg", d)) for d in detail)
    return str(detail) if detail else f"HTTP {resp.status_code}"


# ── Public API ───────────────────────────────────────────────────────────────

def get_flezen_info(flezen_url: str) -> dict:
    """
    Resolve a Flezen share URL to downloadable video info.

    Returns filename, size and download URL. Raises FlezenError on any failure.
    """
    if not FLEZEN_PROXY_URL:
        raise FlezenError("FLEZEN_PROXY_URL not set in environment")
    if not FLEZEN_API_KEY:
        raise FlezenError("FLEZEN_API_KEY not set in environment")

    log.info("Requesting Flezen metadata from proxy…")
    try:
        resp = requests.post(
            FLEZEN_PROXY_URL,
            json={"url": flezen_url},
            headers={"x-api-key": FLEZEN_API_KEY},
            timeout=600,
        )
    except requests.RequestException as e:
        raise FlezenError(f"Could not reach Flezen proxy: {e}") from e

    if resp.status_code != 200:
        raise FlezenError(_extract_error_detail(resp))

    try:
        data = resp.json()
    except ValueError as e:
        raise FlezenError(f"Invalid JSON from Flezen proxy: {e}") from e

    log.info(f"Flezen proxy resolved in {data.get('time_taken_in_sec', '?')}s")

    download_url = data.get("u")
    if not download_url:
        raise FlezenError(f"No download URL in Flezen response: {data}")

    return {
        "filename": data.get("n") or "flezen_video.mp4",
        "size": int(data.get("s") or 0),
        "download_url": download_url,
        "thumbnail_url": None,
    }
