import os
import time 
import threading
import asyncio
import logging
from telethon import TelegramClient, Button

from .helpers import format_size, format_duration
from .caching import _cache_put, _cache_get
from .progress_callbacks import make_download_progress_cb, make_upload_progress_cb

from terabox.public_api import prepare_terabox_link, download_terabox_file, TeraBoxError, CancelledError

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

# — Configuration —————————————————————————————————————————————————————————————
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_ID = int(os.environ.get("APP_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
STORAGE_GROUP_ID = int(os.environ.get("STORAGE_GROUP_ID", "0"))

# — Active-task tracking (for cancel) ————————————————————————————————————————————
active_tasks: dict[tuple[int, str], threading.Event] = {}

# — Bot Setup ————————————————————————————————————————————————————————————— 

bot = TelegramClient(
    "terabox_bot",
    APP_ID,
    API_HASH,
    connection_retries=5,
    retry_delay=2,
    auto_reconnect=True,
)

# — Cache helpers ——————————————————————————————————————————————————————————————

async def find_cached_video(surl: str):
    """
    Look up surl in the local cache file, then fetch the message directly by ID.
    Returns the Telethon Message object if found, otherwise None.
    """
    if not STORAGE_GROUP_ID:
        return None
    msg_id = _cache_get(surl)
    if msg_id is None:
        return None
    try:
        msg = await bot.get_messages(STORAGE_GROUP_ID, ids=msg_id)
        if msg and (msg.video or (
            msg.document
            and msg.document.mime_type
            and "video" in msg.document.mime_type
        )):
            return msg
        return None
    except Exception as e:
        log.warning(f"Cache fetch failed for surl={surl} msg_id={msg_id}: {e}")
        return None
    
async def upload_to_storage(filepath: str, filename: str, progress_cb=None):
    """
    Upload a file to the storage group.
    Caption is set to the video filename.
    Returns the sent Message.
    """
    return await bot.send_file(
        STORAGE_GROUP_ID,
        filepath,
        caption=filename,
        supports_streaming=True,
        progress_callback=progress_cb,
    )

# — Heart Function —————————————————————————————————————————————————————————————

async def _process_terabox(event, surl: str) -> None:
    """Core pipeline: cache → download → upload → deliver."""
    chat_id = event.chat_id
    task_key = (chat_id, surl)
    total_start = time.time()

    cancel_event = threading.Event()
    active_tasks[task_key] = cancel_event

    cancel_btn = [[Button.inline("❌ Cancel", data="cancel_download")]]

    status = await event.respond(f"🔍 Checking cache for `{surl}`…")

    # — Phase 1: Cache lookup ——————————————————————————————————————————————
    cached_msg = await find_cached_video(surl)
    if cached_msg is not None:
        try:
            f = cached_msg.file
            fname = (f.name if f and f.name else surl)
            fsize = (format_size(f.size) if f and f.size else "N/A")
            caption = f"⚡ **From cache**\n\n📦 `{fname}`\n📐 Size: **{fsize}**"
            await bot.send_file(
                chat_id, cached_msg.media,
                caption=caption, supports_streaming=True, reply_to=event.message.id,
            )
            await status.delete()
        except Exception as e:
            log.warning(f"Cache re-send failed for surl={surl}: {e}")
            await status.edit("❌ Failed to send cached video.")
        active_tasks.pop(task_key, None)
        return

    # — Phase 2: Prepare metadata ——————————————————————————————————————————
    await status.edit(f"⏳ Fetching metadata for `{surl}`…", buttons=cancel_btn)
    try:
        prepared = await asyncio.to_thread(prepare_terabox_link, surl)
    except CancelledError:
        await status.edit("🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
    except TeraBoxError as e:
        log.error(f"Prepare error for surl={surl}: {e}")
        await status.edit(f"❌ Error: {e}")
        active_tasks.pop(task_key, None)
        return
    except Exception as e:
        log.exception(f"Unexpected prepare error for surl={surl}")
        await status.edit(f"❌ Unexpected error: {e}")
        active_tasks.pop(task_key, None)
        return

    if cancel_event.is_set():
        await status.edit("🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    filename = prepared["filename"]
    size_str = format_size(prepared["size"])

    await status.edit(
        f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n⬇️ Downloading… **0%**",
        buttons=cancel_btn,
    )

    # — Phase 3: Download ——————————————————————————————————————————————————
    loop = asyncio.get_event_loop()
    dl_start = time.time()
    dl_progress_cb = make_download_progress_cb(status, filename, size_str, loop)
    try:
        filepath = await asyncio.to_thread(download_terabox_file, prepared, cancel_event, dl_progress_cb)
    except CancelledError:
        await status.edit("🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
    except TeraBoxError as e:
        log.error(f"Download error for surl={surl}: {e}")
        await status.edit(f"❌ Download failed: {e}")
        active_tasks.pop(task_key, None)
        return
    except Exception as e:
        log.exception(f"Unexpected download error for surl={surl}")
        await status.edit(f"❌ Download failed: {e}")
        active_tasks.pop(task_key, None)
        return
    dl_time = time.time() - dl_start

    if cancel_event.is_set():
        await status.edit("🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    # Use actual file size (compressed TS/MP4) instead of original API size
    size_str = format_size(os.path.getsize(filepath))

    # — Phase 4: Upload to storage group (cache) ———————————————————————————
    up_start = time.time()
    storage_msg = None

    if STORAGE_GROUP_ID:
        await status.edit(
            f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n📤 Uploading to cache… **0%**"
        )
        progress_cb = make_upload_progress_cb(status, filename, size_str, loop)
        try:
            storage_msg = await upload_to_storage(filepath, filename, progress_cb)
            if storage_msg is not None:
                _cache_put(surl, storage_msg.id)
        except Exception as e:
            log.error(f"Storage upload failed for surl={surl}: {e}")
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
            sent_video = await bot.send_file(
                chat_id,
                storage_msg.media,
                caption=_build_caption(dl_time, up_time, total_time),
                supports_streaming=True,
                reply_to=event.message.id,
            )
        except Exception as e:
            log.warning(f"Re-send from storage failed for surl={surl}, sending directly: {e}")

    if sent_video is None:
        await status.edit(
            f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n📤 Uploading… **0%**"
        )
        progress_cb = make_upload_progress_cb(status, filename, size_str, loop)
        up_start = time.time()
        try:
            sent_video = await bot.send_file(
                chat_id,
                filepath,
                caption=f"📦 `{filename}`\n📐 Size: **{size_str}**",
                supports_streaming=True,
                progress_callback=progress_cb,
                reply_to=event.message.id,
            )
            up_time = time.time() - up_start
            total_time = time.time() - total_start
            try:
                await sent_video.edit(_build_caption(dl_time, up_time, total_time))
            except Exception:
                pass
        except Exception as e:
            log.error(f"Direct upload failed for surl={surl}: {e}")
            await status.edit(f"❌ Upload failed: {e}")
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
        await status.delete()
    except Exception:
        pass

    active_tasks.pop(task_key, None)