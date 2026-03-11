import os
import requests
import http
import re
import time
import json
import subprocess
import threading
from .internal_helpers import BASE_URL, _headers, _logid, TeraBoxError, CancelledError
from urllib.parse import unquote, urlparse, urlunparse, urlencode, parse_qs

COOKIES_FILE = "cookies.txt"
 
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
