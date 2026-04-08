import os
import requests
import re
import time
import json
import subprocess
import threading
import random
import shutil
from .internal_helpers import BASE_URL, _headers, _logid, TeraBoxError, CancelledError, BYTES_PER_MB, CookiesList
from urllib.parse import unquote, urlparse, urlunparse, urlencode, parse_qs

# ── Core Pipeline ─────────────────────────────────────────────────────────────
def load_session() -> requests.Session:
    session = requests.Session()
    # May user something like rounds-robin to make it more robust, AIM: PREVENT SHADOW BANS
    cookie_str = random.choice(CookiesList)
    count = 0
    if cookie_str:
        for c in cookie_str.split(";"):
            if "=" in c:
                k, v = c.strip().split("=", 1)
                session.cookies.set(k.strip(), v.strip(), domain=".1024tera.com", path="/")
                count += 1
    print(f"[+] Loaded {count} cookies")
    return session


def get_js_token(session: requests.Session, surl: str) -> str:
    url = f"{BASE_URL}/wap/share/filelist?surl={surl}&clearCache=1"

    last_err = "Unknown error"
    for attempt in range(3):
        try:
            html = session.get(url, headers=_headers(session, surl), timeout=60).text

            m = re.search(r'fn%28%22([A-Fa-f0-9]+)%22%29', html)
            if m:
                return m.group(1)

            m = re.search(r'eval\(decodeURIComponent\(`([^`]+)`\)\)', html)
            if m:
                m2 = re.search(r'fn\("([A-Fa-f0-9]+)"\)', unquote(m.group(1)))
                if m2:
                    return m2.group(1)
            
            last_err = "Token patterns not found in HTML"
        except requests.RequestException as e:
            last_err = str(e)
            
        if attempt < 2:
            time.sleep(2)
            
    raise TeraBoxError(f"Could not extract jsToken from share page after 3 attempts: {last_err}")


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


def discover_all_hls_chunks(session: requests.Session, shareid, uk, sign, timestamp, fs_id, quality: str, surl: str = "", cancel_event: threading.Event | None = None) -> list:
    """
    Budget-capped randomized chunk discovery.
    """
    req_count = 0
    fail_streak = 0

    def query_random_chunk():
        nonlocal req_count, fail_streak
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Discovery cancelled")
            
        req_count += 1
        url = build_streaming_url(shareid, uk, sign, timestamp, fs_id, quality)

        try:
            text = session.get(url, headers=_headers(session, surl), timeout=60).text.strip()
            fail_streak = 0
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
        if not known:
            return False
        if min(known) > 1:
            return False
        return len(known) == max(known) - min(known) + 1

    print("    Scanning for chunks (random polling)...", flush=True)
    known = {}
    no_new_max_streak = 0
    max_known_idx = -1

    while True:
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Discovery cancelled")

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

        if known and is_complete():
            confidence = max(10, max_known_idx)
            if no_new_max_streak >= confidence:
                print(f"      ✓ All chunks collected (streak: {no_new_max_streak})", flush=True)
                break

        budget = min(100, max(30, max_known_idx * 3)) if max_known_idx > 0 else 30
        if req_count >= budget:
            print(f"      ✓ Budget reached ({budget} reqs)", flush=True)
            break

        if req_count % 10 == 0:
            print(f"      ... {req_count} reqs, {len(known)} chunks found"
                  f" (max: {max_known_idx}, streak: {no_new_max_streak})", flush=True)

    if not known:
        raise TeraBoxError("Could not find any video chunks from the API.")

    first_idx, last_idx = min(known), max(known)
    total = last_idx - first_idx + 1
    missing = [i for i in range(first_idx, last_idx + 1) if i not in known]

    if missing:
        print(f"      ⚠ Missing chunks: {missing}", flush=True)

    print(f"      ✓ Done — {len(known)}/{total} chunks | API requests: {req_count}", flush=True)

    return [known[i] for i in sorted(known)]


def _download_segment(session: requests.Session, url: str, path: str, expected_size: int, surl: str = "", cancel_event: threading.Event | None = None) -> None:
    for attempt in range(5):
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Download cancelled")
        try:
            r = session.get(url, headers=_headers(session, surl), stream=True, timeout=120)
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    if cancel_event and cancel_event.is_set():
                        raise CancelledError("Download cancelled")
                    f.write(chunk)
            actual = os.path.getsize(path)
            if actual < 512:
                raise TeraBoxError("Segment too small (< 512 bytes)")
            if expected_size > 0 and abs(actual - expected_size) / expected_size > 0.20:
                print(f" ⚠ size mismatch (expected {expected_size}, got {actual})", end="", flush=True)
            return
        except CancelledError:
            raise
        except Exception as e:
            if attempt == 4:
                raise TeraBoxError(f"Chunk failed after 5 attempts: {e}")
            backoff = (2 ** attempt) + random.uniform(1.0, 3.0)
            print(f" [Retry {attempt + 1} - sleep {backoff:.1f}s]", end="", flush=True)
            time.sleep(backoff)


def download_all_chunks(session: requests.Session, chunks: list, tmp_dir: str, surl: str = "", cancel_event: threading.Event | None = None, progress_callback=None) -> None:
    os.makedirs(tmp_dir, exist_ok=True)
    total = len(chunks)
    print(f"    Downloading {total} chunk(s)...")
    
    total_size = sum(ts for _, _, ts in chunks)
    done_size = 0

    for i, (idx, url, ts_size) in enumerate(chunks, 1):
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Download cancelled")
        seg_path = os.path.join(tmp_dir, f"chunk_{idx:03d}.ts")
        
        print(f"      [{i}/{total}] Chunk {idx} ({ts_size / BYTES_PER_MB:.1f} MB)...", end="", flush=True)
        _download_segment(session, url, seg_path, ts_size, surl, cancel_event)
        
        if os.path.exists(seg_path):
            done_size += os.path.getsize(seg_path)
            
        print()
        if progress_callback:
            progress_callback(done_size, total_size)


def concatenate_chunks_ffmpeg(tmp_dir: str, chunks: list, mp4_path: str, cancel_event: threading.Event | None = None) -> None:
    if cancel_event and cancel_event.is_set():
        raise CancelledError("Concatenation cancelled")
        
    print("    Demuxing and merging chunks via ffmpeg...")
    concat_txt = os.path.join(tmp_dir, "concat.txt")
    
    with open(concat_txt, "w", encoding="utf-8") as f:
        for idx, _, _ in chunks:
            full_ts_path = os.path.abspath(os.path.join(tmp_dir, f"chunk_{idx:03d}.ts"))
            full_ts_path = full_ts_path.replace("\\", "/") 
            f.write(f"file '{full_ts_path}'\n")

    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", mp4_path],
        capture_output=True, text=True, timeout=1200
    )
    if proc.returncode != 0 or not os.path.exists(mp4_path):
        err = "\n".join(proc.stderr.strip().split("\n")[-5:])
        raise TeraBoxError(f"ffmpeg conversion failed (exit {proc.returncode}):\n{err}")
