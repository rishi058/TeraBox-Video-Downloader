"""
diskwalaDL/public_api.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Client for the Diskwala scraper proxy.

The proxy exposes a single POST endpoint that, given a Diskwala share URL,
resolves it to a direct downloadable video URL plus file metadata.

    POST <DISKWALA_PROXY_URL>
    Headers : x-api-key: <DISKWALA_API_KEY>
    Body    : {"url": "https://www.diskwala.com/app/<24-hex-id>"}

Success response (200):
    {
        "time_taken_in_sec": "20",
        "fileInfo": {
            "name": "video.mp4",
            "type": "video/mp4",
            "size": 4812254,
            "extension": "mp4",
            "url": "https://s3....mp4",
            ...
        }
    }
"""
import os
import re
import logging

import requests

log = logging.getLogger(__name__)

DISKWALA_PROXY_URL = os.getenv("DISKWALA_PROXY_URL")
DISKWALA_API_KEY = os.getenv("DISKWALA_API_KEY")

# Diskwala share links embed a 24-char hex id (a MongoDB ObjectId).
_LINK_ID_RE = re.compile(r"[a-fA-F0-9]{24}")

# A full Diskwala URL sitting anywhere inside a block of text.
DISKWALA_URL_RE = re.compile(r"https?://\S*diskwala\.com/\S+", re.IGNORECASE)


class DiskwalaError(Exception):
    """Raised when the Diskwala proxy fails or returns unusable data."""


def extract_diskwala_id(text: str) -> str | None:
    """Return the 24-hex Diskwala link id found in `text`, or None."""
    m = _LINK_ID_RE.search(text or "")
    return m.group(0) if m else None


def extract_all_diskwala_urls(text: str) -> list[str]:
    """Extract all unique Diskwala URLs (that carry a link id) from `text`."""
    seen: set[str] = set()
    urls: list[str] = []
    for m in DISKWALA_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(").,]}\"'")  # trim trailing punctuation
        if url not in seen and extract_diskwala_id(url):
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

def get_diskwala_info(diskwala_url: str) -> dict:
    """
    Resolve a Diskwala share URL to downloadable video info.

    Returns filename, size, download URL and provider thumbnail URL.
    Raises DiskwalaError on any failure.
    """
    if not DISKWALA_PROXY_URL:
        raise DiskwalaError("DISKWALA_PROXY_URL not set in environment")
    if not DISKWALA_API_KEY:
        raise DiskwalaError("DISKWALA_API_KEY not set in environment")

    log.info("Requesting Diskwala metadata from proxy…")
    try:
        resp = requests.post(
            DISKWALA_PROXY_URL,
            json={"url": diskwala_url},
            headers={"x-api-key": DISKWALA_API_KEY},
            timeout=600,
        )
    except requests.RequestException as e:
        raise DiskwalaError(f"Could not reach Diskwala proxy: {e}") from e

    if resp.status_code != 200:
        raise DiskwalaError(_extract_error_detail(resp))

    try:
        data = resp.json()
    except ValueError as e:
        raise DiskwalaError(f"Invalid JSON from Diskwala proxy: {e}") from e

    log.info(f"Diskwala proxy resolved in {data.get('time_taken_in_sec', '?')}s")

    file_info = data.get("fileInfo") or {}
    download_url = file_info.get("url")
    if not download_url:
        raise DiskwalaError(f"No download URL in Diskwala response: {data}")

    return {
        "filename": file_info.get("name") or "diskwala_video.mp4",
        "size": int(file_info.get("size") or 0),
        "download_url": download_url,
        "thumbnail_url": file_info.get("thumb"),
    }
