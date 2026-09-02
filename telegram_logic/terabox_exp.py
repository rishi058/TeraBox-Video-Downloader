import os
import time 
import threading
import asyncio
import logging
from urllib.parse import unquote, urlparse
from telethon import Button
from telethon.errors import FloodWaitError

from .bot import bot, _cancellable, terabox_queue, _safe_send, active_tasks
from .helpers import format_size, format_duration, extract_surl_exp
from firebase_db.cache import add_to_cache, search_in_cache
from .progress_callbacks import make_download_progress_cb, make_upload_progress_cb
from .thumbnails import prepare_video_preview

from terabox.public_api import TeraBoxError, CancelledError
from teraboxDL.public_api import download_terabox_file_experimental
from teraboxDL.terabox_dl import get_video_info

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

# — Heart Function —————————————————————————————————————————————————————————————

#! ONLY PUBLIC API
async def process_terabox_experimental(event, terabox_url: str, is_hd: bool = False) -> None:
    """Submit a request to the fair, bounded scheduler."""
    await terabox_queue.submit(helper, event, terabox_url, is_hd)


async def helper(event, terabox_url: str, is_hd: bool) -> None:
    """Inner pipeline, runs under the concurrency semaphore."""
    chat_id = event.chat_id
    surl = extract_surl_exp(terabox_url)
    user_mode = "exphd" if is_hd else "exp"
    task_key = (chat_id, surl)
    total_start = time.time()

    cancel_event = active_tasks.get(task_key)
    if cancel_event is None:
        cancel_event = threading.Event()
        active_tasks[task_key] = cancel_event
    elif cancel_event.is_set():
        active_tasks.pop(task_key, None)
        return

    cancel_btn = [[Button.inline("❌ Cancel", data=f"cancel:{surl}")]]

    def _cleanup_files(*paths):
        """Remove temp/downloaded files from disk."""
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    log.info(f"Cleaned up file: {p}")
                except Exception as e:
                    log.warning(f"Could not clean up {p}: {e}")

    status = await _safe_send(event.respond, "🔍 Checking local cache…", buttons=cancel_btn)
    cached = search_in_cache(terabox_url, user_mode)
    using_cached_url = cached is not None
    should_update_cache = not using_cached_url

    def _fallback_filename(url: str) -> str:
        name = unquote(os.path.basename(urlparse(url).path)).strip()
        return name if name and "." in name else "cached_video.mp4"

    if cached:
        download_url = cached["download_url"]
        filename = cached.get("filename") or _fallback_filename(download_url)
        file_size = int(cached.get("file_size") or 0)
        thumbnail_url = cached.get("thumbnail_url")
    else:
        await _safe_send(status.edit, "⏳ Fetching metadata…", buttons=cancel_btn)
        try:
            info = await asyncio.to_thread(get_video_info, terabox_url, is_hd)
        except Exception as e:
            log.error(f"Metadata fetch failed for surl={surl}: {e}")
            await _safe_send(status.edit, f"❌ Failed to get video info: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
            active_tasks.pop(task_key, None)
            return
        download_url = info["download_url"]
        filename = info["filename"]
        file_size = int(info.get("size") or 0)
        thumbnail_url = info.get("thumbnail_url")

    size_str = format_size(file_size) if file_size else "Unknown"
    loop = asyncio.get_running_loop()
    dl_start = time.time()

    async def _download_current_url():
        await _safe_send(
            status.edit,
            f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n⬇️ Downloading… **0%**",
            buttons=cancel_btn,
        )
        callback = make_download_progress_cb(
            status, filename, size_str, loop, cancel_btn, safe_send=_safe_send, chat_id=chat_id
        )
        return await asyncio.to_thread(
            download_terabox_file_experimental,
            download_url,
            filename,
            cancel_event,
            callback,
        )

    try:
        filepath = await _download_current_url()
    except CancelledError:
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
    except Exception as first_error:
        if not using_cached_url:
            log.error(f"Download error for surl={surl}: {first_error}")
            await _safe_send(status.edit, f"❌ Download failed: {first_error}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
            active_tasks.pop(task_key, None)
            return

        # Signed cache URLs may expire. Refresh through the proxy only after a
        # real cache download failure, then retry once.
        log.info("Cached URL failed for %s; refreshing provider metadata", surl)
        await _safe_send(status.edit, "♻️ Cached link expired. Refreshing metadata…", buttons=cancel_btn)
        try:
            info = await asyncio.to_thread(get_video_info, terabox_url, is_hd)
            download_url = info["download_url"]
            filename = info["filename"]
            file_size = int(info.get("size") or 0)
            thumbnail_url = info.get("thumbnail_url")
            size_str = format_size(file_size) if file_size else "Unknown"
            should_update_cache = True
            filepath = await _download_current_url()
        except CancelledError:
            await _safe_send(status.edit, "🚫 Cancelled.")
            active_tasks.pop(task_key, None)
            return
        except Exception as retry_error:
            log.error("Refreshed download failed for surl=%s: %s", surl, retry_error)
            await _safe_send(status.edit, f"❌ Download failed: {retry_error}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
            active_tasks.pop(task_key, None)
            return

    dl_time = time.time() - dl_start
    if should_update_cache:
        await asyncio.to_thread(
            add_to_cache, terabox_url, download_url, user_mode, filename,
            os.path.getsize(filepath), thumbnail_url,
        )

    if cancel_event.is_set():
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    # Use actual file size (compressed TS/MP4) instead of original API size
    size_str = format_size(os.path.getsize(filepath))
    await _safe_send(status.edit, f"📦 **{filename}**\n\n🖼 Preparing video preview…")
    try:
        preview = await prepare_video_preview(filepath, thumbnail_url)
    except Exception as exc:
        log.warning("Preview generation failed for %s: %s", surl, exc)
        preview = None

    # Upload directly to the requesting user; no Telegram storage-group copy.
    def _build_caption(dl_t: float, up_t: float, total_t: float) -> str:
        return (
            f"📦 `{filename}`\n"
            f"📐 Size: **{size_str}**\n\n"
            f"⬇️ Download: **{format_duration(dl_t)}**\n"
            f"📤 Upload: **{format_duration(up_t)}**\n"
            f"⏱️ Total: **{format_duration(total_t)}**"
        )

    await _safe_send(
        status.edit,
        f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n📤 Uploading… **0%**",
        buttons=cancel_btn,
    )
    progress_cb = make_upload_progress_cb(
        status, filename, size_str, loop, cancel_btn, safe_send=_safe_send, chat_id=chat_id
    )
    up_start = time.time()
    try:
        sent_video = await _cancellable(
            _safe_send(
                bot.send_file, chat_id, filepath,
                caption=f"📦 `{filename}`\n📐 Size: **{size_str}**",
                thumb=preview.thumbnail_path if preview else None,
                attributes=preview.attributes(filename) if preview else None,
                supports_streaming=True, reply_to=event.message.id,
                progress_callback=progress_cb,
                max_retries=1,
            ), cancel_event,
        )
        up_time = time.time() - up_start
        total_time = time.time() - total_start
        try:
            await _safe_send(sent_video.edit, _build_caption(dl_time, up_time, total_time))
        except Exception:
            pass
    except asyncio.CancelledError:
        log.info(f"Direct upload cancelled by user for surl={surl}")
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
    except FloodWaitError:
        # Queue-level handling releases the active slot and retries after the
        # chat's flood window. Remove this attempt's downloaded media first.
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        raise
    except Exception as e:
        log.error(f"Direct upload failed for surl={surl}: {e}")
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, f"❌ Upload failed: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
        active_tasks.pop(task_key, None)
        return
    finally:
        if preview:
            preview.cleanup()

    for f_path in (filepath, os.path.splitext(filepath)[0] + ".ts"):
        if os.path.exists(f_path):
            try:
                os.remove(f_path)
                log.info(f"Deleted local file: {f_path}")
            except Exception as e:
                log.warning(f"Could not delete local file {f_path}: {e}")

    try:
        await _safe_send(status.delete)
    except Exception:
        pass

    active_tasks.pop(task_key, None)
