"""
TeraBox Video Downloader via Streaming CDN.

Fetches M3U8 playlist -> extracts CDN segment URL with auth tokens ->
modifies byte range to download full TS file -> converts TS to MP4.
"""
import re
import json
import http.cookiejar
import time
import random
import subprocess
import os
from urllib.parse import unquote, urlparse, urlunparse, urlencode, parse_qs
import requests

# ── Config ────────────────────────────────────────────────────────────────────
# SURL = "nOvK6r4RyVtnYKOxmoqp0w"
SURL = "YnhCurm3PCfnk15_PzlJYg"
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


# ── Helpers ───────────────────────────────────────────────────────────────────
def _logid():
    return str(random.randint(400_000_000_000_000_000, 999_999_999_999_999_999))


def _cookie_str(session):
    return "; ".join(
        f"{c.name}={c.value}" for c in session.cookies
        if "1024tera" in (c.domain or "")
    )


def _headers(session, referer=None):
    return {
        "User-Agent": UA,
        "Cookie": _cookie_str(session),
        "Referer": referer or f"{BASE_URL}/wap/share/filelist?surl={SURL}",
    }


def _safe_filename(name):
    return re.sub(r'[\\/*?:"<>|]', '_', name)


# ── Core Pipeline ─────────────────────────────────────────────────────────────
def load_session() -> requests.Session:
    session = requests.Session()
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(COOKIES_FILE, ignore_discard=True, ignore_expires=True)
    for c in jar:
        session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    print(f"[+] Loaded {sum(1 for _ in jar)} cookies")
    return session


def get_js_token(session) -> str:
    url = f"{BASE_URL}/wap/share/filelist?surl={SURL}&clearCache=1"
    html = session.get(url, headers=_headers(session), timeout=60).text
    m = re.search(r'fn%28%22([A-Fa-f0-9]+)%22%29', html)
    if m:
        return m.group(1)
    m = re.search(r'eval\(decodeURIComponent\(`([^`]+)`\)\)', html)
    if m:
        m2 = re.search(r'fn\("([A-Fa-f0-9]+)"\)', unquote(m.group(1)))
        if m2:
            return m2.group(1)
    raise RuntimeError("Could not extract jsToken from share page")


def get_share_info(session, js_token) -> dict:
    params = {
        "app_id": "250528", "shorturl": f"1{SURL}", "root": "1",
        "web": "1", "channel": "dubox", "clienttype": "0",
        "jsToken": js_token, "t": str(int(time.time())), "dp-logid": _logid(),
    }
    hdrs = _headers(session)
    hdrs.update({"Accept": "application/json, text/plain, */*", "Origin": BASE_URL})
    data = session.get(
        f"{BASE_URL}/api/shorturlinfo", params=params, headers=hdrs, timeout=60
    ).json()
    if data.get("errno") != 0:
        raise RuntimeError(f"shorturlinfo failed: errno={data.get('errno')}")
    return data


def build_streaming_url(shareid, uk, sign, timestamp, fs_id, quality) -> str:
    return f"{BASE_URL}/share/streaming?" + urlencode({
        "uk": str(uk), "shareid": str(shareid), "type": quality,
        "fid": str(fs_id), "sign": sign, "timestamp": str(timestamp),
        "jsToken": "", "esl": "1", "isplayer": "1", "ehps": "1",
        "clienttype": "0", "app_id": "250528", "web": "1",
        "channel": "dubox", "dp-logid": _logid(),
    })


def fetch_full_ts_url(session, streaming_url) -> tuple[str, int]:
    """Fetch M3U8, extract a segment URL, rewrite range to cover full TS file.
    Returns (full_download_url, ts_size) or raises on failure."""
    r = session.get(streaming_url, headers=_headers(session), timeout=60)
    r.raise_for_status()
    text = r.text.strip()

    if not text.startswith("#EXTM3U"):
        # Could be a JSON error
        try:
            err = json.loads(text)
            raise RuntimeError(f"API error: errno={err.get('errno')}, {err.get('errmsg', '')}")
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError(f"Unexpected response (not M3U8): {text[:200]}")

    segments = [l.strip() for l in text.split("\n") if l.strip() and not l.startswith("#")]
    if not segments:
        raise RuntimeError("M3U8 contains no segment URLs")

    parsed = urlparse(segments[0])
    params = parse_qs(parsed.query, keep_blank_values=True)
    ts_size = int(params.get("ts_size", ["0"])[0])
    if ts_size <= 0:
        raise RuntimeError("Could not determine ts_size from segment URL")

    # Rewrite range to cover entire file
    params["range"] = [f"0-{ts_size - 1}"]
    params["len"] = [str(ts_size)]
    full_url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in params.items()})))
    return full_url, ts_size


def download_ts(session, url, ts_path, expected_size) -> None:
    """Stream-download a TS file with progress display."""
    r = session.get(url, headers=_headers(session), stream=True, timeout=300)
    r.raise_for_status()
    total = int(r.headers.get("Content-Length", expected_size))
    done = 0
    with open(ts_path, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            f.write(chunk)
            done += len(chunk)
            pct = done * 100 // total if total else 0
            print(f"\r    Downloading: {done / 1048576:.1f} / {total / 1048576:.1f} MB ({pct}%)",
                  end="", flush=True)
    print()
    if os.path.getsize(ts_path) < 1024:
        os.remove(ts_path)
        raise RuntimeError("Downloaded file too small, likely an error response")


def convert_ts_to_mp4(ts_path, mp4_path) -> None:
    """Remux TS -> MP4 via ffmpeg (stream copy, no re-encode)."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", ts_path, "-c", "copy", mp4_path],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0 or not os.path.exists(mp4_path):
        err = "\n".join(proc.stderr.strip().split("\n")[-3:])
        raise RuntimeError(f"ffmpeg conversion failed (exit {proc.returncode}):\n{err}")
    os.remove(ts_path)


# ── Main ──────────────────────────────────────────────────────────────────────
def download_video(surl=SURL):
    session = load_session()

    print("[1] Extracting jsToken...")
    js_token = get_js_token(session)
    print(f"    jsToken: {js_token[:30]}...")

    print("[2] Fetching share info...")
    info = get_share_info(session, js_token)
    files = info.get("list", [])
    shareid, uk = info["shareid"], info["uk"]
    sign, timestamp = info["sign"], info["timestamp"]

    if not files:
        print("    No files in share.")
        return

    print(f"    Found {len(files)} file(s)\n")

    for i, f in enumerate(files):
        name = f["server_filename"]
        fs_id = f["fs_id"]
        size_mb = int(f.get("size", 0)) / 1048576
        safe = _safe_filename(name)
        os.makedirs(STORAGE_DIR, exist_ok=True)
        mp4_path = os.path.join(STORAGE_DIR, safe if safe.lower().endswith(".mp4") else safe + ".mp4")
        ts_path = os.path.join(STORAGE_DIR, safe.rsplit(".", 1)[0] + ".ts" if "." in safe else safe + ".ts")

        print(f"  [{i+1}] {name} ({size_mb:.1f} MB)")

        # Try each quality level as fallback
        for quality in QUALITIES:
            try:
                print(f"    Trying {quality}...")
                stream_url = build_streaming_url(shareid, uk, sign, timestamp, fs_id, quality)
                full_url, ts_size = fetch_full_ts_url(session, stream_url)
                print(f"    Full TS: {ts_size / 1048576:.1f} MB")

                download_ts(session, full_url, ts_path, ts_size)
                convert_ts_to_mp4(ts_path, mp4_path)

                final_size = os.path.getsize(mp4_path) / 1048576
                print(f"    Done! {mp4_path} ({final_size:.1f} MB)")
                break  # Success — skip lower qualities

            except Exception as e:
                print(f"    [{quality}] Failed: {e}")
                # Clean up partial files
                for p in (ts_path, mp4_path):
                    if os.path.exists(p) and os.path.getsize(p) < 1024:
                        os.remove(p)
                continue
        else:
            print(f"    [!] All qualities failed for: {name}")

    print("\nDone!")


if __name__ == "__main__":
    download_video()
