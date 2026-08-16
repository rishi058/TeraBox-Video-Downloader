import os
import time
import threading
import asyncio
import logging
from urllib.parse import unquote, urlparse
from telethon import Button
from telethon.errors import FloodWaitError

from .bot import (
    bot, _cancellable, terabox_queue, _safe_send, active_tasks,
)
from .helpers import format_size, format_duration
from firebase_db.cache import add_to_cache, search_in_cache
from .progress_callbacks import make_download_progress_cb, make_upload_progress_cb
from .thumbnails import prepare_video_preview

from terabox.public_api import TeraBoxError, CancelledError
from teraboxDL.public_api import download_terabox_file_experimental
from diskwalaDL.public_api import get_diskwala_info, extract_diskwala_id, DiskwalaError

log = logging.getLogger(__name__)

# Diskwala shares its own cache bucket / user mode.
DW_MODE = "dw"


# — Heart Function —————————————————————————————————————————————————————————————

#! ONLY PUBLIC API
async def process_diskwala(event, diskwala_url: str) -> None:
    # If currently in flood cooldown → queue immediately
    rem = terabox_queue.flood_remaining()
    if rem > 0:
        await terabox_queue.put(_dw_helper, event, diskwala_url)
        try:
            await event.respond(
                "⏳ Bot overloaded! Your request has been queued "
                f"and will be processed automatically in ~{rem}s."
            )
        except FloodWaitError as e:
            terabox_queue.update_flood_until(e.seconds)
        except Exception:
            pass
        return

    # Try processing normally under the semaphore
    async with terabox_queue.semaphore:
        try:
            await _dw_helper(event, diskwala_url)
        except FloodWaitError as e:
            # Pipeline hit flood → set cooldown, queue, notify user
            terabox_queue.update_flood_until(e.seconds)
            await terabox_queue.put(_dw_helper, event, diskwala_url)
            try:
                await event.respond(
                    f"⏳ Bot overloaded! Your request has been queued "
                    f"and will be processed automatically in ~{e.seconds}s."
                )
            except Exception:
                pass


async def _dw_helper(event, diskwala_url: str) -> None:
    """Inner pipeline, runs under the concurrency semaphore."""
    chat_id = event.chat_id
    link_id = extract_diskwala_id(diskwala_url) or diskwala_url
    cache_source_url = (
        f"https://www.diskwala.com/app/{link_id}"
        if extract_diskwala_id(diskwala_url)
        else diskwala_url
    )
    user_mode = DW_MODE
    task_key = (chat_id, link_id)
    total_start = time.time()

    cancel_event = threading.Event()
    active_tasks[task_key] = cancel_event

    cancel_btn = [[Button.inline("❌ Cancel", data=f"cancel:{link_id}")]]

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
    cached = search_in_cache(cache_source_url, user_mode)
    using_cached_url = cached is not None
    should_update_cache = not using_cached_url

    def _fallback_filename(url: str) -> str:
        name = unquote(os.path.basename(urlparse(url).path)).strip()
        if "#" in name:
            name = name.rsplit("#", 1)[-1]
        return name if name and "." in name else "cached_video.mp4"

    if cached:
        download_url = cached["download_url"]
        filename = cached.get("filename") or _fallback_filename(download_url)
        file_size = int(cached.get("file_size") or 0)
        thumbnail_url = cached.get("thumbnail_url")
    else:
        await _safe_send(status.edit, "⏳ Fetching metadata…", buttons=cancel_btn)
        try:
            info = await asyncio.to_thread(get_diskwala_info, diskwala_url)
        except Exception as e:
            log.error(f"Diskwala metadata fetch failed for {link_id}: {e}")
            await _safe_send(status.edit, f"❌ Failed to get video info: {e}")
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
        callback = make_download_progress_cb(status, filename, size_str, loop, cancel_btn)
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
            log.error("Download error for %s: %s", link_id, first_error)
            await _safe_send(status.edit, f"❌ Download failed: {first_error}")
            active_tasks.pop(task_key, None)
            return

        log.info("Cached URL failed for %s; refreshing provider metadata", link_id)
        await _safe_send(status.edit, "♻️ Cached link expired. Refreshing metadata…", buttons=cancel_btn)
        try:
            info = await asyncio.to_thread(get_diskwala_info, diskwala_url)
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
            log.error("Refreshed download failed for %s: %s", link_id, retry_error)
            await _safe_send(status.edit, f"❌ Download failed: {retry_error}")
            active_tasks.pop(task_key, None)
            return

    dl_time = time.time() - dl_start
    if should_update_cache:
        await asyncio.to_thread(
            add_to_cache, cache_source_url, download_url, user_mode, filename,
            os.path.getsize(filepath), thumbnail_url,
        )

    if cancel_event.is_set():
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    # Use actual file size on disk instead of the API-reported size
    size_str = format_size(os.path.getsize(filepath))
    await _safe_send(status.edit, f"📦 **{filename}**\n\n🖼 Preparing video preview…")
    try:
        preview = await prepare_video_preview(filepath, thumbnail_url)
    except Exception as exc:
        log.warning("Preview generation failed for %s: %s", link_id, exc)
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
    progress_cb = make_upload_progress_cb(status, filename, size_str, loop, cancel_btn)
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
            ), cancel_event,
        )
        up_time = time.time() - up_start
        total_time = time.time() - total_start
        try:
            await _safe_send(sent_video.edit, _build_caption(dl_time, up_time, total_time))
        except Exception:
            pass
    except asyncio.CancelledError:
        log.info(f"Direct upload cancelled by user for {link_id}")
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
    except Exception as e:
        log.error(f"Direct upload failed for {link_id}: {e}")
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, f"❌ Upload failed: {e}")
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
