import threading
import os
from .internal_helpers import _safe_filename 
from .core_pipeline import load_session, get_js_token, get_share_info, build_streaming_url, fetch_full_ts_url, download_ts, convert_ts_to_mp4
from .internal_helpers import TeraBoxError, CancelledError

# ── Config ────────────────────────────────────────────────────────────────────

STORAGE_DIR = "storage"
QUALITIES = ["M3U8_AUTO_1080", "M3U8_AUTO_720", "M3U8_AUTO_480", "M3U8_AUTO_360"] 

# ── Public API ─────────────────────────────────────────────────────────

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
