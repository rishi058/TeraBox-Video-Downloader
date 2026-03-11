"""
TeraBox Video Downloader via Streaming CDN.

Fetches M3U8 playlist -> extracts CDN segment URL with auth tokens ->
modifies byte range to download full TS file -> converts TS to MP4.

Public API (used by bot.py):
    prepare_terabox_link(surl)  -> dict with file metadata
    download_terabox_file(prepared, cancel_event) -> local mp4 path
"""
import re
import json
import http.cookiejar
import time
import random
import subprocess
import os
import threading
from urllib.parse import unquote, urlparse, urlunparse, urlencode, parse_qs
import requests


# ── Custom Exceptions ─────────────────────────────────────────────────────────
class TeraBoxError(Exception):
    """Raised for known, expected TeraBox errors."""


class CancelledError(Exception):
    """Raised when a download is cancelled."""


# ── Config ────────────────────────────────────────────────────────────────────
BASE_DOMAIN = "dm.1024tera.com"
BASE_URL = f"https://{BASE_DOMAIN}"
COOKIES_FILE = "cookies.txt"
STORAGE_DIR = "storage"
QUALITIES = ["M3U8_AUTO_1080", "M3U8_AUTO_720", "M3U8_AUTO_480", "M3U8_AUTO_360"]

UA = (
    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Mobile Safari/537.36"
)


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


# ── Core Pipeline ─────────────────────────────────────────────────────────────
def load_session() -> requests.Session:
    session = requests.Session()
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(COOKIES_FILE, ignore_discard=True, ignore_expires=True)
    for c in jar:
        session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    return session


def get_js_token(session: requests.Session, surl: str) -> str:
    url = f"{BASE_URL}/wap/share/filelist?surl={surl}&clearCache=1"
    html = session.get(url, headers=_headers(session, surl), timeout=60).text
    m = re.search(r'fn%28%22([A-Fa-f0-9]+)%22%29', html)
    if m:
        return m.group(1)
    m = re.search(r'eval\(decodeURIComponent\(`([^`]+)`\)\)', html)
    if m:
        m2 = re.search(r'fn\("([A-Fa-f0-9]+)"\)', unquote(m.group(1)))
        if m2:
            return m2.group(1)
    raise TeraBoxError("Could not extract jsToken from share page")


def get_share_info(session: requests.Session, js_token: str, surl: str) -> dict:
    params = {
        "app_id": "250528", "shorturl": f"1{surl}", "root": "1",
        "web": "1", "channel": "dubox", "clienttype": "0",
        "jsToken": js_token, "t": str(int(time.time())), "dp-logid": _logid(),
    }
    hdrs = _headers(session, surl)
    hdrs.update({"Accept": "application/json, text/plain, */*", "Origin": BASE_URL})
    data = session.get(
        f"{BASE_URL}/api/shorturlinfo", params=params, headers=hdrs, timeout=60
    ).json()
    if data.get("errno") != 0:
        raise TeraBoxError(f"shorturlinfo failed: errno={data.get('errno')}")
    return data


def build_streaming_url(shareid, uk, sign, timestamp, fs_id, quality: str) -> str:
    return f"{BASE_URL}/share/streaming?" + urlencode({
        "uk": str(uk), "shareid": str(shareid), "type": quality,
        "fid": str(fs_id), "sign": sign, "timestamp": str(timestamp),
        "jsToken": "", "esl": "1", "isplayer": "1", "ehps": "1",
        "clienttype": "0", "app_id": "250528", "web": "1",
        "channel": "dubox", "dp-logid": _logid(),
    })


def fetch_full_ts_url(session: requests.Session, streaming_url: str, surl: str) -> tuple[str, int]:
    """Fetch M3U8, extract a segment URL, rewrite range to cover the full TS file."""
    r = session.get(streaming_url, headers=_headers(session, surl), timeout=60)
    r.raise_for_status()
    text = r.text.strip()

    if not text.startswith("#EXTM3U"):
        try:
            err = json.loads(text)
            raise TeraBoxError(f"API error: errno={err.get('errno')}, {err.get('errmsg', '')}")
        except (json.JSONDecodeError, ValueError):
            raise TeraBoxError(f"Unexpected response (not M3U8): {text[:200]}")

    segments = [ln.strip() for ln in text.split("\n") if ln.strip() and not ln.startswith("#")]
    if not segments:
        raise TeraBoxError("M3U8 contains no segment URLs")

    parsed = urlparse(segments[0])
    params = parse_qs(parsed.query, keep_blank_values=True)
    ts_size = int(params.get("ts_size", ["0"])[0])
    if ts_size <= 0:
        raise TeraBoxError("Could not determine ts_size from segment URL")

    # Rewrite range to cover entire file
    params["range"] = [f"0-{ts_size - 1}"]
    params["len"] = [str(ts_size)]
    full_url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in params.items()})))
    return full_url, ts_size


def download_ts(
    session: requests.Session,
    url: str,
    ts_path: str,
    expected_size: int,
    surl: str = "",
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> None:
    """Stream-download a TS file with optional cancellation support."""
    r = session.get(url, headers=_headers(session, surl), stream=True, timeout=300)
    r.raise_for_status()
    total = int(r.headers.get("Content-Length", expected_size))
    done = 0
    with open(ts_path, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            if cancel_event and cancel_event.is_set():
                raise CancelledError("Download cancelled")
            f.write(chunk)
            done += len(chunk)
            pct = done * 100 // total if total else 0
            print(f"\r    {done / 1048576:.1f} / {total / 1048576:.1f} MB ({pct}%)",
                  end="", flush=True)
            if progress_callback:
                progress_callback(done, total)
    print()
    if os.path.getsize(ts_path) < 1024:
        os.remove(ts_path)
        raise TeraBoxError("Downloaded file too small — likely an error response")


def convert_ts_to_mp4(ts_path: str, mp4_path: str) -> None:
    """Remux TS -> MP4 via ffmpeg (stream copy, no re-encode)."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", ts_path, "-c", "copy", mp4_path],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0 or not os.path.exists(mp4_path):
        err = "\n".join(proc.stderr.strip().split("\n")[-3:])
        raise TeraBoxError(f"ffmpeg failed (exit {proc.returncode}):\n{err}")
    os.remove(ts_path)


# ── Public API ────────────────────────────────────────────────────────────────

def prepare_terabox_link(surl: str) -> dict:
    """
    Fetch file metadata for a TeraBox SURL.

    Returns a dict with keys:
        filename, size, fs_id, shareid, uk, sign, timestamp, session, surl

    Raises TeraBoxError on any failure.
    """
    session = load_session()
    js_token = get_js_token(session, surl)
    info = get_share_info(session, js_token, surl)
    files = info.get("list", [])
    if not files:
        raise TeraBoxError("No files found in this share")
    f = files[0]
    return {
        "filename": f["server_filename"],
        "size": int(f.get("size", 0)),
        "fs_id": f["fs_id"],
        "shareid": info["shareid"],
        "uk": info["uk"],
        "sign": info["sign"],
        "timestamp": info["timestamp"],
        "session": session,
        "surl": surl,
    }


def download_terabox_file(
    prepared: dict,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> str:
    """
    Download the video described by `prepared` (from prepare_terabox_link).

    Returns the absolute path to a local MP4 file.
    Raises TeraBoxError or CancelledError.
    """
    surl = prepared["surl"]
    session = prepared["session"]
    filename = prepared["filename"]
    safe = _safe_filename(filename)
    os.makedirs(STORAGE_DIR, exist_ok=True)
    mp4_path = os.path.join(STORAGE_DIR, safe if safe.lower().endswith(".mp4") else safe + ".mp4")
    ts_path = os.path.join(STORAGE_DIR, (safe.rsplit(".", 1)[0] if "." in safe else safe) + ".ts")

    # Re-use an already-downloaded local copy to avoid re-downloading
    if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 1024:
        return mp4_path

    last_error: Exception | None = None
    for quality in QUALITIES:
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Download cancelled")
        try:
            stream_url = build_streaming_url(
                prepared["shareid"], prepared["uk"],
                prepared["sign"], prepared["timestamp"],
                prepared["fs_id"], quality,
            )
            full_url, ts_size = fetch_full_ts_url(session, stream_url, surl)
            download_ts(session, full_url, ts_path, ts_size, surl=surl, cancel_event=cancel_event, progress_callback=progress_callback)
            convert_ts_to_mp4(ts_path, mp4_path)
            return mp4_path
        except CancelledError:
            if os.path.exists(ts_path):
                try:
                    os.remove(ts_path)
                except Exception:
                    pass
            raise
        except Exception as e:
            last_error = e
            for p in (ts_path, mp4_path):
                if os.path.exists(p) and os.path.getsize(p) < 1024:
                    os.remove(p)

    raise TeraBoxError(f"All quality levels failed — last error: {last_error}")


# ── Standalone entry point ────────────────────────────────────────────────────
def _standalone_download(surl: str) -> None:
    print(f"[1] Preparing link (surl={surl})...")
    prepared = prepare_terabox_link(surl)
    filename = prepared["filename"]
    size_mb = prepared["size"] / 1048576
    print(f"    File: {filename} ({size_mb:.1f} MB)")
    print("[2] Downloading...")
    mp4_path = download_terabox_file(prepared)
    final_mb = os.path.getsize(mp4_path) / 1048576
    print(f"\n    Saved: {mp4_path} ({final_mb:.1f} MB)")


if __name__ == "__main__":
    _standalone_download("nOvK6r4RyVtnYKOxmoqp0w")
