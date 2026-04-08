"""
TeraBox Video Downloader via Streaming CDN.

Fetches M3U8 playlist -> downloads ALL HLS segments ->
concatenates raw TS bytes -> converts to MP4.
"""
import re
import time
import random
import subprocess
import os
import shutil
from urllib.parse import unquote, urlparse, urlunparse, urlencode, parse_qs
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SURL = "H7Cy40dAq4eLQ_hKVxaWAA"
BASE_DOMAIN = "dm.1024tera.com"
BASE_URL = f"https://{BASE_DOMAIN}"
STORAGE_DIR = "storage"
QUALITY = "M3U8_AUTO_1080"
BYTES_PER_MB = 1048576

# Domain substring used to filter session cookies. If TeraBox migrates to a
# different domain in the future, update this constant (and the domain kwarg in
# load_session) so cookie handling keeps working.
COOKIE_DOMAIN = "1024tera"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def _logid():
    return str(random.randint(400_000_000_000_000_000, 999_999_999_999_999_999))


def _cookie_str(session):
    return "; ".join(
        f"{c.name}={c.value}" for c in session.cookies
        if COOKIE_DOMAIN in (c.domain or "")
    )


def _headers(session, referer=None):
    hdrs = {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": referer or f"{BASE_URL}/wap/share/filelist?surl={SURL}",
    }
    cookie_str = _cookie_str(session)
    if cookie_str:
        hdrs["Cookie"] = cookie_str
    return hdrs


def _safe_filename(name):
    return re.sub(r'[\\/*?:"<>|]', '_', name)


# ── Core Pipeline ─────────────────────────────────────────────────────────────
def load_session() -> requests.Session:
    session = requests.Session()
    cookie_str = os.environ.get("COOKIES", "").strip()
    count = 0
    if cookie_str:
        for c in cookie_str.split(";"):
            if "=" in c:
                k, v = c.strip().split("=", 1)
                session.cookies.set(k, v, domain="1024tera.com")
                count += 1
    print(f"[+] Loaded {count} cookies")
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


def discover_all_hls_chunks(session, shareid, uk, sign, timestamp, fs_id, quality) -> list:
    """
    Budget-capped randomized chunk discovery.

    The TeraBox streaming API IGNORES the `start` parameter and returns a
    random chunk per request (verified empirically — see README).  We treat
    this as a Coupon Collector problem but cap the total API requests to
    avoid rate-limiting / shadow-bans.

    Stopping rules (whichever fires first):
      1. EARLY STOP  — is_complete() AND no_new_max streak >= max(10, max_idx)
      2. BUDGET       — req_count >= max(30, max_known_idx * 3), hard cap 100
    """
    req_count = 0
    fail_streak = 0

    def query_random_chunk():
        """Single API request → list of (chunk_idx, full_url, ts_size)."""
        nonlocal req_count, fail_streak
        req_count += 1
        url = build_streaming_url(shareid, uk, sign, timestamp, fs_id, quality)

        try:
            text = session.get(url, headers=_headers(session), timeout=60).text.strip()
            fail_streak = 0  # Reset on successful request
        except requests.RequestException:
            fail_streak += 1
            backoff = (2 ** fail_streak) + random.uniform(0.5, 1.5)
            time.sleep(backoff)
            return []

        if not text.startswith("#EXTM3U"):
            time.sleep(random.uniform(0.5, 1.5))
            return []

        segs = [l.strip() for l in text.split("\n") if l.strip() and not l.startswith("#")]
        if not segs:
            return []

        results = []
        for seg_url in segs:
            parsed = urlparse(seg_url)
            p = parse_qs(parsed.query, keep_blank_values=True)
            ts_size = int(p.get("ts_size", ["0"])[0])
            if ts_size <= 0:
                continue
            m = re.search(r'_(\d+)_ts/', parsed.path)
            if not m:
                continue
            chunk_idx = int(m.group(1))
            p["range"] = [f"0-{ts_size - 1}"]
            p["len"] = [str(ts_size)]
            full_url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in p.items()})))
            results.append((chunk_idx, full_url, ts_size))

        time.sleep(random.uniform(0.3, 0.8))
        return results

    def is_complete():
        """Check if we have every chunk in a contiguous range starting near 0."""
        if not known:
            return False
        if min(known) > 1:
            return False
        return len(known) == max(known) - min(known) + 1

    print("    Scanning for chunks (random polling)...", flush=True)
    known = {}   # chunk_idx → (chunk_idx, full_url, ts_size)
    no_new_max_streak = 0
    max_known_idx = -1

    while True:
        results = query_random_chunk()

        for item in results:
            if item[0] not in known:
                known[item[0]] = item

        current_max = max(known.keys()) if known else -1

        if current_max > max_known_idx:
            max_known_idx = current_max
            no_new_max_streak = 0
        else:
            no_new_max_streak += 1

        # ── Stop rule 1: confident we have everything ────────────────
        if known and is_complete():
            confidence = max(10, max_known_idx)
            if no_new_max_streak >= confidence:
                print(f"      ✓ All chunks collected (streak: {no_new_max_streak})", flush=True)
                break

        # ── Stop rule 2: budget exhausted ────────────────────────────
        budget = min(100, max(30, max_known_idx * 3)) if max_known_idx > 0 else 30
        if req_count >= budget:
            print(f"      ✓ Budget reached ({budget} reqs)", flush=True)
            break

        # ── Progress log every 10 requests ───────────────────────────
        if req_count % 10 == 0:
            print(f"      ... {req_count} reqs, {len(known)} chunks found"
                  f" (max: {max_known_idx}, streak: {no_new_max_streak})", flush=True)

    if not known:
        raise RuntimeError("Could not find any video chunks from the API.")

    first_idx, last_idx = min(known), max(known)
    total = last_idx - first_idx + 1
    missing = [i for i in range(first_idx, last_idx + 1) if i not in known]

    if missing:
        print(f"      ⚠ Missing chunks: {missing}", flush=True)

    print(f"      ✓ Done — {len(known)}/{total} chunks | API requests: {req_count}", flush=True)

    return [known[i] for i in sorted(known)]


def _download_segment(session, url, path, expected_size) -> None:
    for attempt in range(5):
        try:
            r = session.get(url, headers=_headers(session), stream=True, timeout=120)
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    f.write(chunk)
            actual = os.path.getsize(path)
            if actual < 512:
                raise RuntimeError("Segment too small (< 512 bytes)")
            # Lenient size check: warn if actual size deviates > 20% from
            # expected, but only hard-fail on extremely small files.
            if expected_size > 0 and abs(actual - expected_size) / expected_size > 0.20:
                print(f" ⚠ size mismatch (expected {expected_size}, got {actual})", end="", flush=True)
            return
        except Exception as e:
            if attempt == 4:
                raise RuntimeError(f"Chunk failed after 5 attempts: {e}")
            backoff = (2 ** attempt) + random.uniform(1.0, 3.0)
            print(f" [Retry {attempt + 1} - sleep {backoff:.1f}s]", end="", flush=True)
            time.sleep(backoff)


def download_all_chunks(session, chunks, tmp_dir) -> None:
    os.makedirs(tmp_dir, exist_ok=True)
    total = len(chunks)
    print(f"    Downloading {total} chunk(s)...")
    
    for i, (idx, url, ts_size) in enumerate(chunks, 1):
        seg_path = os.path.join(tmp_dir, f"chunk_{idx:03d}.ts")
        print(f"      [{i}/{total}] Chunk {idx} ({ts_size / BYTES_PER_MB:.1f} MB)...", end="", flush=True)
        _download_segment(session, url, seg_path, ts_size)
        print()


def concatenate_chunks_ffmpeg(tmp_dir, chunks, mp4_path) -> None:
    print("    Demuxing and merging chunks via ffmpeg...")
    concat_txt = os.path.join(tmp_dir, "concat.txt")
    
    with open(concat_txt, "w", encoding="utf-8") as f:
        for idx, _, _ in chunks:
            full_ts_path = os.path.abspath(os.path.join(tmp_dir, f"chunk_{idx:03d}.ts"))
            # ffmpeg concat demuxer requires forward slashes on Windows
            full_ts_path = full_ts_path.replace("\\", "/") 
            f.write(f"file '{full_ts_path}'\n")

    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", mp4_path],
        capture_output=True, text=True, timeout=1200
    )
    if proc.returncode != 0 or not os.path.exists(mp4_path):
        err = "\n".join(proc.stderr.strip().split("\n")[-5:])
        raise RuntimeError(f"ffmpeg conversion failed (exit {proc.returncode}):\n{err}")


# ── Main ──────────────────────────────────────────────────────────────────────
def download_video():
    # No Need to send our info in cookies to extract jsToken & INFO
    temp_session = requests.Session()

    print("[1] Extracting jsToken...")
    js_token = get_js_token(temp_session)
    print(f"    jsToken: {js_token[:30]}...")

    print("[2] Fetching share info...")
    info = get_share_info(temp_session, js_token)
    files = info.get("list", [])
    shareid, uk = info["shareid"], info["uk"]
    sign, timestamp = info["sign"], info["timestamp"]

    if not files:
        print("    No files in share.")
        return

    print(f"    Found {len(files)} file(s)\n")
    
    # Need session with cookies to get streaming-url
    session = load_session()

    for i, f in enumerate(files):
        name = f["server_filename"]
        fs_id = f["fs_id"]
        size_mb = int(f.get("size", 0)) / BYTES_PER_MB
        safe = _safe_filename(name)
        os.makedirs(STORAGE_DIR, exist_ok=True)
        mp4_path = os.path.join(STORAGE_DIR, safe if safe.lower().endswith(".mp4") else safe + ".mp4")
        tmp_dir = os.path.join(STORAGE_DIR, safe.rsplit(".", 1)[0] + "_segments")

        print(f"  [{i+1}] {name} ({size_mb:.1f} MB)")

        try:
            print(f"    Using quality {QUALITY}...")

            # Step 1: Scan for all distinct TS chunks spanning the video
            chunks = discover_all_hls_chunks(session, shareid, uk, sign, timestamp, fs_id, QUALITY)

            # Step 2: Download every chunk
            download_all_chunks(session, chunks, tmp_dir)

            # Step 3: Concat & Remux to MP4
            concatenate_chunks_ffmpeg(tmp_dir, chunks, mp4_path)

            expected_total = sum(ts for _, _, ts in chunks) / BYTES_PER_MB
            final_size = os.path.getsize(mp4_path) / BYTES_PER_MB
            print(f"    Done! {mp4_path}")
            print(f"    Expected (raw TS): {expected_total:.1f} MB → Actual (MP4): {final_size:.1f} MB")
            shutil.rmtree(tmp_dir, ignore_errors=True)

        except Exception as e:
            print(f"    Failed: {e}")
            # Clean up partial files
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if os.path.exists(mp4_path) and os.path.getsize(mp4_path) < 1024:
                os.remove(mp4_path)

    print("\nDone!")


if __name__ == "__main__":
    download_video()
