import threading
import os
import requests
import shutil
from .internal_helpers import _safe_filename, BYTES_PER_MB
from .core_pipeline import load_session, get_js_token, get_share_info, discover_all_hls_chunks, download_all_chunks, concatenate_chunks_ffmpeg
from .internal_helpers import TeraBoxError, CancelledError

# ── Config ────────────────────────────────────────────────────────────────────

STORAGE_DIR = "storage"
QUALITY = "M3U8_AUTO_1080"

# ── Public API ─────────────────────────────────────────────────────────

def prepare_terabox_link(surl: str) -> dict:
    """
    Fetch file metadata for a TeraBox SURL.

    Returns a dict with keys:
        filename, size, fs_id, shareid, uk, sign, timestamp, session, surl

    Raises TeraBoxError on any failure.
    """
    temp_session = requests.Session()
    
    print("[1] Extracting jsToken...")
    js_token = get_js_token(temp_session, surl)
    print(f"    jsToken: {js_token[:30]}...")

    print("[2] Fetching share info...")
    info = get_share_info(temp_session, js_token, surl)
    files = info.get("list", [])
    if not files:
        print("    No files in share.")
        raise TeraBoxError("No files found in this share")
        
    print(f"    Found {len(files)} file(s)\n")
    f = files[0]
    
    return {
        "filename": f["server_filename"],
        "size": int(f.get("size", 0)),
        "fs_id": f["fs_id"],
        "shareid": info["shareid"],
        "uk": info["uk"],
        "sign": info["sign"],
        "timestamp": info["timestamp"],
        "session": load_session(),
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
    size = prepared["size"]
    
    safe = _safe_filename(filename)
    os.makedirs(STORAGE_DIR, exist_ok=True)
    mp4_path = os.path.join(STORAGE_DIR, safe if safe.lower().endswith(".mp4") else safe + ".mp4")
    tmp_dir = os.path.join(STORAGE_DIR, safe.rsplit(".", 1)[0] + "_segments")

    # Re-use an already-downloaded local copy to avoid re-downloading
    if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 1024:
        print(f"    Done! {mp4_path}")
        return mp4_path

    print(f"  [1] {filename} ({size / BYTES_PER_MB:.1f} MB)")

    try:
        print(f"    Using quality {QUALITY}...")
        
        # Step 1: Scan for all distinct TS chunks spanning the video
        chunks = discover_all_hls_chunks(
            session, prepared["shareid"], prepared["uk"], 
            prepared["sign"], prepared["timestamp"], prepared["fs_id"], 
            QUALITY, surl=surl, cancel_event=cancel_event
        )
        
        # Step 2: Download every chunk
        download_all_chunks(session, chunks, tmp_dir, surl=surl, cancel_event=cancel_event, progress_callback=progress_callback)
        
        # Step 3: Concat & Remux to MP4
        concatenate_chunks_ffmpeg(tmp_dir, chunks, mp4_path, cancel_event=cancel_event)
        
        final_size = os.path.getsize(mp4_path) / BYTES_PER_MB
        print(f"    Done! {mp4_path}. Video Size: {final_size:.1f} MB")
        
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return mp4_path

    except Exception as e:
        print(f"    Failed: {e}")
        # Clean up partial files
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if os.path.exists(mp4_path) and os.path.getsize(mp4_path) < 1024:
            os.remove(mp4_path)
            
        if isinstance(e, CancelledError):
            raise
        raise TeraBoxError(f"Download failed: {e}") from e
