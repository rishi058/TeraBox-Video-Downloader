import os
import time 
import threading
import asyncio
import logging
from telethon import Button
from telethon.errors import FloodWaitError

from .bot import bot, _find_cached_video, _pre_upload_file, _upload_to_storage, _cancellable, terabox_queue, _safe_send, active_tasks, STORAGE_GROUP_ID
from .helpers import format_size, format_duration, extract_surl
from .caching import add_to_cache
from .progress_callbacks import make_download_progress_cb, make_upload_progress_cb

from terabox.public_api import TeraBoxError, CancelledError
from teraboxDL.public_api import download_terabox_file_experimental
from teraboxDL.terabox_dl import get_video_info

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

# — Heart Function —————————————————————————————————————————————————————————————

#! ONLY PUBLIC API
async def process_terabox_experimental(event, terabox_url: str, is_hd: bool = False) -> None:
    # If currently in flood cooldown → queue immediately
    rem = terabox_queue.flood_remaining()
    if rem > 0:
        await terabox_queue.put(helper, event, terabox_url, is_hd)
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
            await helper(event, terabox_url, is_hd)
        except FloodWaitError as e:
            # Pipeline hit flood → set cooldown, queue, notify user
            terabox_queue.update_flood_until(e.seconds)
            await terabox_queue.put(helper, event, terabox_url, is_hd)
            try:
                await event.respond(
                    f"⏳ Bot overloaded! Your request has been queued "
                    f"and will be processed automatically in ~{e.seconds}s."
                )
            except Exception:
                pass


async def helper(event, terabox_url: str, is_hd: bool) -> None:
    """Inner pipeline, runs under the concurrency semaphore."""
    chat_id = event.chat_id
    surl = extract_surl(terabox_url)
    user_mode = "exphd" if is_hd else "exp"
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

    # — Phase 1: Cache lookup ——————————————————————————————————————————————
    status = await _safe_send(event.respond, f"🔍 Checking cache for `{surl}`…")

    cached_msg = await _find_cached_video(surl, user_mode)
    if cached_msg is not None:
        try:
            f = cached_msg.file
            fname = (f.name if f and f.name else surl)
            caption = f"📦 `{fname}`"
            await _safe_send(
                bot.send_file,
                chat_id, cached_msg.media,
                caption=caption, supports_streaming=True, reply_to=event.message.id,
            )
            await _safe_send(status.delete)
        except Exception as e:
            log.warning(f"re-send failed for surl={surl}: {e}")
            await _safe_send(status.edit, "❌ Failed to send video.")
        active_tasks.pop(task_key, None)
        return

    # — Phase 2: Prepare metadata ——————————————————————————————————————————
    await _safe_send(status.edit, f"⏳ Fetching metadata…", buttons=cancel_btn)

    #! GET FILE INFO
    try:
        info = await asyncio.to_thread(get_video_info, terabox_url, is_hd)
    except Exception as e:
        log.error(f"Metadata fetch failed for surl={surl}: {e}")
        await _safe_send(status.edit, f"❌ Failed to get video info: {e}")
        active_tasks.pop(task_key, None)
        return

    download_url = info["download_url"]
    filename = info["filename"]
    size_str = format_size(info["size"])

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
        filepath = await asyncio.to_thread(download_terabox_file_experimental, download_url, filename, cancel_event, dl_progress_cb)
    except CancelledError:
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
    except TeraBoxError as e:
        log.error(f"Download error for surl={surl}: {e}")
        await _safe_send(status.edit, f"❌ Download failed: {e}")
        active_tasks.pop(task_key, None)
        return
    except Exception as e:
        log.exception(f"Unexpected download error for surl={surl}")
        await _safe_send(status.edit, f"❌ Download failed: {e}")
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

    # — Phase 4: Upload to storage group (cache) ———————————————————————————
    if cancel_event.is_set():
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    up_start = time.time()
    storage_msg = None
    input_file = None  # reusable Telegram upload handle

    if STORAGE_GROUP_ID:
        await _safe_send(
            status.edit,
            f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n📤 Uploading **0%**",
            buttons=cancel_btn,
        )
        progress_cb = make_upload_progress_cb(status, filename, size_str, loop, cancel_btn)
        try:
            # Upload file bytes to Telegram ONCE → get reusable InputFile handle
            input_file = await _cancellable(_pre_upload_file(filepath, progress_cb), cancel_event)
            storage_msg = await _cancellable(_upload_to_storage(input_file, filename), cancel_event)
            if storage_msg is not None:
                await asyncio.to_thread(add_to_cache, surl, storage_msg.id, user_mode)
        except asyncio.CancelledError:
            log.info(f"Upload cancelled by user for surl={surl}")
            _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
            await _safe_send(status.edit, "🚫 Cancelled.")
            active_tasks.pop(task_key, None)
            return
        except Exception as e:
            log.error(f"Storage upload failed for surl={surl}: {e}")
            input_file = None  # clear so fallback re-uploads from disk
            # storage_msg stays None → fall back to direct upload below

    # — Phase 5: Deliver to user ———————————————————————————————————————————
    def _build_caption(dl_t: float, up_t: float, total_t: float) -> str:
        return (
            f"📦 `{filename}`\n"
            f"📐 Size: **{size_str}**\n\n"
            f"⬇️ Download: **{format_duration(dl_t)}**\n"
            f"📤 Upload: **{format_duration(up_t)}**\n"
            f"⏱️ Total: **{format_duration(total_t)}**"
        )

    sent_video = None

    if storage_msg is not None:
        up_time = time.time() - up_start
        total_time = time.time() - total_start
        try:
            sent_video = await _safe_send(
                bot.send_file,
                chat_id,
                storage_msg.media,
                caption=_build_caption(dl_time, up_time, total_time),
                supports_streaming=True,
                reply_to=event.message.id,
            )
        except Exception as e:
            log.warning(f"Re-send from storage failed for surl={surl}, sending directly: {e}")

    if sent_video is None:
        # Use the pre-uploaded handle if available, otherwise fall back to disk
        upload_source = input_file if input_file else filepath
        needs_progress = input_file is None  # only show progress if re-uploading from disk

        if needs_progress:
            await _safe_send(
                status.edit,
                f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n📤 Uploading… **0%**",
                buttons=cancel_btn,
            )
        progress_cb = make_upload_progress_cb(status, filename, size_str, loop, cancel_btn) if needs_progress else None
        up_start = time.time()
        try:
            kwargs = {}
            if progress_cb:
                kwargs["progress_callback"] = progress_cb
            sent_video = await _cancellable(
                _safe_send(
                    bot.send_file,
                    chat_id,
                    upload_source,
                    caption=f"📦 `{filename}`\n📐 Size: **{size_str}**",
                    supports_streaming=True,
                    reply_to=event.message.id,
                    **kwargs,
                ),
                cancel_event,
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
            await _safe_send(status.edit, f"❌ Upload failed: {e}")
            active_tasks.pop(task_key, None)
            return

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
