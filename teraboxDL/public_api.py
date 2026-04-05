import threading
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
import random
import shutil
from urllib.parse import urlparse
from terabox.internal_helpers import _safe_filename, TeraBoxError, CancelledError

STORAGE_DIR = "storage"
CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB — much less overhead than 256KB

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
        pool_connections=4,
        pool_maxsize=4,
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

def _download_video(download_url: str, download_path: str, cancel_event: threading.Event | None = None, progress_callback=None) -> None:
    session = _build_session()

    # Set Referer to the download URL's origin (some CDNs check this)
    parsed = urlparse(download_url)
    session.headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    
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
            if attempt == 2:
                raise TeraBoxError(f"Chunk failed after 3 attempts: {e}")

            backoff = (2 ** attempt) + random.uniform(1.0, 3.0)
            print(f"\n [Retry {attempt + 1} - sleep {backoff:.1f}s]", end="", flush=True)
            time.sleep(backoff)