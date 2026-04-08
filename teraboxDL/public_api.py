import threading
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
import random
import shutil
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from terabox.internal_helpers import _safe_filename, TeraBoxError, CancelledError

STORAGE_DIR = "storage"
CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB per read chunk within each part

# Number of parallel parts to split a single download into.
# 4 connections → ~4x throughput on CDNs that allow range requests.
PARALLEL_PARTS = 4

# Browser-identical headers — this is the #1 reason for throttling.
# TeraBox CDN checks User-Agent and throttles python-requests to ~100KB/s.
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}


def _build_session() -> requests.Session:
    """Create a requests session that mimics a real browser."""
    session = requests.Session()
    session.headers.update(_BROWSER_HEADERS)

    # Robust adapter: connection pool + auto-retry on transport errors
    adapter = HTTPAdapter(
        pool_connections=PARALLEL_PARTS,
        pool_maxsize=PARALLEL_PARTS,
        max_retries=Retry(total=0),  # we handle retries ourselves
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def download_terabox_file_experimental(
    download_url: str,
    filename: str,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> str:
    """
    Download the video described by download_url and filename.

    Returns the absolute path to a local MP4 file.
    Raises TeraBoxError or CancelledError.
    """
    
    safe = _safe_filename(filename)
    os.makedirs(STORAGE_DIR, exist_ok=True)
    mp4_path = os.path.join(STORAGE_DIR, safe if safe.lower().endswith(".mp4") else safe + ".mp4")
   
    try:
        _download_video(download_url, mp4_path, cancel_event, progress_callback)

        print()  # newline after progress
        print(f"    Download Completed! {mp4_path}")
        return mp4_path

    except Exception as e:
        print(f"\n    Failed: {e}")
        if os.path.exists(mp4_path):
            os.remove(mp4_path)
            
        if isinstance(e, CancelledError):
            raise
        raise TeraBoxError(f"Download failed: {e}") from e

#!--------------PRIVATE HELPERS----------------

def _check_range_support(session: requests.Session, download_url: str) -> int:
    """
    HEAD the URL to get content-length and check if the server supports
    HTTP Range requests. Returns total_size (0 if unknown/no range support).
    """
    try:
        r = session.head(download_url, timeout=15, allow_redirects=True)
        r.raise_for_status()
        accepts_ranges = r.headers.get("Accept-Ranges", "").lower()
        content_length = int(r.headers.get("Content-Length", 0))
        if accepts_ranges == "bytes" and content_length > 0:
            return content_length
    except Exception:
        pass
    return 0  # fallback → single-stream download


def _download_part(
    session: requests.Session,
    download_url: str,
    byte_start: int,
    byte_end: int,
    part_path: str,
    part_index: int,
    progress_lock: threading.Lock,
    shared_progress: list,          # [done_bytes]
    total_size: int,
    start_time: float,
    cancel_event: threading.Event | None,
    progress_callback,
) -> None:
    """Download a single byte-range part of the file to part_path."""
    # Set Referer on per-part session (CDN may throttle/reject without it)
    parsed = urlparse(download_url)
    session.headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"

    headers = {"Range": f"bytes={byte_start}-{byte_end}"}
    for attempt in range(4):
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Download cancelled")
        try:
            r = session.get(download_url, headers=headers, stream=True, timeout=120)
            r.raise_for_status()
            with open(part_path, "wb") as f:
                for chunk in r.iter_content(CHUNK_SIZE):
                    if cancel_event and cancel_event.is_set():
                        raise CancelledError("Download cancelled")
                    f.write(chunk)
                    with progress_lock:
                        shared_progress[0] += len(chunk)
                        done = shared_progress[0]

                    elapsed = time.time() - start_time
                    speed = (done / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                    done_mb = done / (1024 * 1024)
                    if total_size > 0:
                        total_mb = total_size / (1024 * 1024)
                        pct = (done / total_size) * 100
                        print(
                            f"\r    Downloading: {done_mb:.2f} / {total_mb:.2f} MB"
                            f"  ({pct:.0f}%)  {speed:.1f} MB/s  [part {part_index+1}/{PARALLEL_PARTS}]",
                            end="", flush=True,
                        )
                    else:
                        print(f"\r    Downloading: {done_mb:.2f} MB  {speed:.1f} MB/s", end="", flush=True)

                    if progress_callback:
                        progress_callback(done, total_size)
            return  # success
        except CancelledError:
            raise
        except Exception as e:
            if attempt == 3:
                raise TeraBoxError(f"Part {part_index} failed after 4 attempts: {e}")
            backoff = (2 ** attempt) + random.uniform(0.5, 2.0)
            print(f"\n [Part {part_index} retry {attempt+1} – sleep {backoff:.1f}s]", end="", flush=True)
            time.sleep(backoff)


def _download_video_multipart(
    session: requests.Session,
    download_url: str,
    download_path: str,
    total_size: int,
    cancel_event: threading.Event | None,
    progress_callback,
) -> None:
    """Split the file into PARALLEL_PARTS byte ranges and download concurrently."""
    part_size = total_size // PARALLEL_PARTS
    ranges = []
    for i in range(PARALLEL_PARTS):
        start = i * part_size
        end = (start + part_size - 1) if i < PARALLEL_PARTS - 1 else (total_size - 1)
        ranges.append((start, end))

    part_dir = download_path + ".parts"
    os.makedirs(part_dir, exist_ok=True)
    part_paths = [os.path.join(part_dir, f"part_{i}") for i in range(PARALLEL_PARTS)]

    progress_lock = threading.Lock()
    shared_progress = [0]  # mutable list so threads can update
    start_time = time.time()

    try:
        with ThreadPoolExecutor(max_workers=PARALLEL_PARTS) as executor:
            futures = {
                executor.submit(
                    _download_part,
                    _build_session(),          # each part gets its own session/connection
                    download_url,
                    ranges[i][0], ranges[i][1],
                    part_paths[i],
                    i,
                    progress_lock,
                    shared_progress,
                    total_size,
                    start_time,
                    cancel_event,
                    progress_callback,
                ): i
                for i in range(PARALLEL_PARTS)
            }
            for future in as_completed(futures):
                future.result()  # re-raise any exception from the part thread

        # Stitch parts together
        with open(download_path, "wb") as out:
            for part_path in part_paths:
                with open(part_path, "rb") as p:
                    shutil.copyfileobj(p, out)
    finally:
        # Clean up temp parts regardless of success/failure
        for pp in part_paths:
            if os.path.exists(pp):
                try:
                    os.remove(pp)
                except Exception:
                    pass
        if os.path.exists(part_dir):
            try:
                os.rmdir(part_dir)
            except Exception:
                pass


def _download_video(
    download_url: str,
    download_path: str,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> None:
    session = _build_session()

    # Set Referer to the download URL's origin (some CDNs check this)
    parsed = urlparse(download_url)
    session.headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"

    # Check if the CDN supports byte-range requests
    total_size = _check_range_support(session, download_url)

    if total_size > 0:
        print(f"    [MultiPart] Server supports Range. Splitting into {PARALLEL_PARTS} parts ({total_size/(1024*1024):.1f} MB total).")
        _download_video_multipart(session, download_url, download_path, total_size, cancel_event, progress_callback)

        actual = os.path.getsize(download_path)
        if actual < total_size * 0.95:
            raise TeraBoxError(
                f"Incomplete download: got {actual} bytes, expected {total_size}"
            )
        return

    # ---- Fallback: single-stream download (server doesn't support Range) ----
    print("    [SingleStream] Server does not support Range requests. Falling back to single stream.")
    for attempt in range(4):
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Download cancelled")
        try:
            r = session.get(download_url, stream=True, timeout=120)
            r.raise_for_status()

            total_size = int(r.headers.get("content-length", 0))
            done_size = 0
            start_time = time.time()

            with open(download_path, "wb") as f:
                for chunk in r.iter_content(CHUNK_SIZE):
                    if cancel_event and cancel_event.is_set():
                        raise CancelledError("Download cancelled")
                    f.write(chunk)
                    done_size += len(chunk)

                    # Progress display
                    done_mb = done_size / (1024 * 1024)
                    elapsed = time.time() - start_time
                    speed = (done_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                    if total_size > 0:
                        total_mb = total_size / (1024 * 1024)
                        pct = (done_size / total_size) * 100
                        print(f"\r    Downloading: {done_mb:.2f} / {total_mb:.2f} MB  ({pct:.0f}%)  {speed:.1f} MB/s", end="", flush=True)
                    else:
                        print(f"\r    Downloading: {done_mb:.2f} MB  {speed:.1f} MB/s", end="", flush=True)

                    if progress_callback:
                        progress_callback(done_size, total_size)
            
            actual = os.path.getsize(download_path)
            if actual < 512:
                raise TeraBoxError("Segment too small (< 512 bytes)")
            return
        except CancelledError:
            raise
        except Exception as e:
            if attempt == 3:
                raise TeraBoxError(f"Chunk failed after 4 attempts: {e}")

            backoff = (2 ** attempt) + random.uniform(1.0, 3.0)
            print(f"\n [Retry {attempt + 1} - sleep {backoff:.1f}s]", end="", flush=True)
            time.sleep(backoff)