import os
import time 
import threading
import asyncio
import logging
from telethon import Button
from telethon.errors import FloodWaitError

from .bot import bot, _cancellable, terabox_queue, _safe_send, active_tasks
from .helpers import format_size, format_duration
from .progress_callbacks import make_download_progress_cb, make_upload_progress_cb
from .thumbnails import prepare_video_preview

from terabox.public_api import prepare_terabox_link, download_terabox_file, TeraBoxError, CancelledError

log = logging.getLogger(__name__)

# — Heart Function —————————————————————————————————————————————————————————————

#! ONLY PUBLIC API
async def process_terabox(event, surl: str) -> None:
    """
    Entry point: process immediately, or queue if flood-gated.
    Users never need to re-send — queued requests auto-process.
    """
    # If currently in flood cooldown → queue immediately
    rem = terabox_queue.flood_remaining()
    if rem > 0:
        await terabox_queue.put(helper, event, surl)
        try:
            await event.respond(
                f"⏳ Bot overloaded! Your request for `{surl}` has been queued "
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
            await helper(event, surl)
        except FloodWaitError as e:
            # Pipeline hit flood → set cooldown, queue, notify user
            terabox_queue.update_flood_until(e.seconds)
            await terabox_queue.put(helper, event, surl)
            try:
                await event.respond(
                    f"⏳ Bot overloaded! Your request for `{surl}` has been queued "
                    f"and will be processed automatically in ~{e.seconds}s."
                )
            except Exception:
                pass


async def helper(event, surl: str) -> None:
    """Inner pipeline, runs under the concurrency semaphore."""
    chat_id = event.chat_id
    task_key = (chat_id, surl)
    total_start = time.time()

    cancel_event = threading.Event()
    active_tasks[task_key] = cancel_event

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


    status = await _safe_send(event.respond, "⏳ Fetching metadata…", buttons=cancel_btn)
    
    try:
        prepared = await asyncio.to_thread(prepare_terabox_link, surl)
    except CancelledError:
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
    except TeraBoxError as e:
        log.error(f"Prepare error for surl={surl}: {e}")
        await _safe_send(status.edit, f"❌ Error: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
        active_tasks.pop(task_key, None)
        return
    except Exception as e:
        log.exception(f"Unexpected prepare error for surl={surl}")
        await _safe_send(status.edit, f"❌ Unexpected error: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
        active_tasks.pop(task_key, None)
        return

    if cancel_event.is_set():
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    filename = prepared["filename"]
    size_str = format_size(prepared["size"])

    await _safe_send(
        status.edit,
        f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n⬇️ Downloading… **0%**",
        buttons=cancel_btn,
    )

    # — Phase 3: Download ——————————————————————————————————————————————————
    loop = asyncio.get_running_loop()
    dl_start = time.time()
    dl_progress_cb = make_download_progress_cb(status, filename, size_str, loop, cancel_btn)
    try:
        filepath = await asyncio.to_thread(download_terabox_file, prepared, cancel_event, dl_progress_cb)
    except CancelledError:
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
    except TeraBoxError as e:
        log.error(f"Download error for surl={surl}: {e}")
        await _safe_send(status.edit, f"❌ Download failed: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
        active_tasks.pop(task_key, None)
        return
    except Exception as e:
        log.exception(f"Unexpected download error for surl={surl}")
        await _safe_send(status.edit, f"❌ Download failed: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
        active_tasks.pop(task_key, None)
        return
    dl_time = time.time() - dl_start

    if cancel_event.is_set():
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    # Use actual file size (compressed TS/MP4) instead of original API size
    size_str = format_size(os.path.getsize(filepath))
    await _safe_send(status.edit, f"📦 **{filename}**\n\n🖼 Preparing video preview…")
    try:
        preview = await prepare_video_preview(filepath)
    except Exception as exc:
        log.warning("Preview generation failed for %s: %s", surl, exc)
        preview = None

    # Upload directly to the user. Traditional /get has no reusable direct URL,
    # so it is not added to the random URL catalogue.
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
        log.info(f"Direct upload cancelled by user for surl={surl}")
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
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
