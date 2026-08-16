import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests
from telethon import events
from telethon.tl.types import DocumentAttributeFilename

from diskwalaDL.public_api import get_diskwala_info
from firebase_db.cache import get_random_cache_record
from terabox.internal_helpers import _safe_filename
from terabox.public_api import CancelledError, TeraBoxError
from teraboxDL.public_api import download_terabox_file_experimental
from teraboxDL.terabox_dl import get_video_info
from ..bot import _cancellable, _safe_send, bot, terabox_queue
from ..helpers import format_duration, format_size
from ..progress_callbacks import make_download_progress_cb, make_upload_progress_cb

log = logging.getLogger(__name__)


def _filename_from_url(download_url: str) -> str:
    """Best-effort filename extraction for generic direct URLs."""
    name = ""
    response = None
    try:
        response = requests.head(download_url, allow_redirects=True, timeout=15)
        disposition = response.headers.get("Content-Disposition", "")
        encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
        plain = re.search(r'filename="?([^";]+)', disposition, re.IGNORECASE)
        if encoded:
            name = unquote(encoded.group(1))
        elif plain:
            name = plain.group(1).strip()

        final_url = response.url or download_url
        parsed = urlparse(final_url)
        if not name:
            query = parse_qs(parsed.query)
            for key in ("filename", "file_name", "name", "file"):
                if query.get(key):
                    name = unquote(query[key][0])
                    break
        if not name:
            name = unquote(os.path.basename(parsed.path)).strip()
            # Diskwala CDN paths commonly encode "#actual_filename".
            if "#" in name:
                name = name.rsplit("#", 1)[-1]
    except requests.RequestException:
        parsed = urlparse(download_url)
        name = unquote(os.path.basename(parsed.path)).strip()
        if "#" in name:
            name = name.rsplit("#", 1)[-1]
    finally:
        if response is not None:
            response.close()

    name = os.path.basename(name).strip()
    return name[:180] if name and "." in name else "random_video.mp4"


def _resolve_media_info(record: dict) -> dict:
    """Refresh metadata from the original provider, matching /exp and /dw."""
    source_url = record["source_url"]
    cached_url = record["download_url"]
    mode = record.get("_mode")
    try:
        if mode in ("exp", "exphd"):
            info = get_video_info(source_url, is_hd=(mode == "exphd"))
        elif mode == "dw":
            info = get_diskwala_info(source_url)
        else:
            info = None

        if info and info.get("download_url"):
            filename = str(info.get("filename") or "").strip()
            if not filename or filename == "unknown":
                filename = _filename_from_url(info["download_url"])
            return {
                "filename": filename,
                "download_url": info["download_url"],
                "size": int(info.get("size") or 0),
            }
    except Exception as exc:
        # Stored links may still be valid even when a metadata proxy is down.
        log.warning("Could not refresh random metadata for mode=%s: %s", mode, exc)

    return {
        "filename": _filename_from_url(cached_url),
        "download_url": cached_url,
        "size": 0,
    }


@bot.on(events.NewMessage(pattern=r"^/random(?:@\S+)?$"))
async def cmd_random(event):
    log.info("Received /random command from chat %s", event.chat_id)
    # Share the global download/upload limit with /exp, /exphd and /dw. This
    # prevents a burst of /random calls from exhausting RAM, disk, bandwidth,
    # or Telegram upload slots while still allowing bounded concurrency.
    async with terabox_queue.semaphore:
        await _deliver_random(event)
    raise events.StopPropagation


async def _deliver_random(event):
    try:
        record = await asyncio.to_thread(get_random_cache_record)
    except Exception as exc:
        log.error("[/random] DB error: %s", exc)
        await event.respond("⚠️ **Database error.** Please try again in a moment.")
        raise events.StopPropagation

    if not record:
        await event.respond("📭 No videos yet. Send a supported link first!")
        raise events.StopPropagation

    status = await _safe_send(event.respond, "⏳ Fetching video information…")
    loop = asyncio.get_running_loop()
    cancel_event = threading.Event()
    total_start = time.time()
    work_dir = None

    try:
        info = await asyncio.to_thread(_resolve_media_info, record)
        download_url = info["download_url"]
        filename = info["filename"]
        expected_size = format_size(info["size"]) if info["size"] else "Unknown"

        os.makedirs("storage", exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix="random_", dir="storage")
        disk_name = _safe_filename(filename) or "random_video.mp4"
        # Match the experimental pipeline: stream outputs must use an MP4 path
        # so stream_downloader performs its ffmpeg remux step.
        if not disk_name.lower().endswith(".mp4"):
            disk_name += ".mp4"
        output_path = os.path.join(work_dir, disk_name)

        await _safe_send(
            status.edit,
            f"📦 **{filename}**\n📐 Size: **{expected_size}**\n\n⬇️ Downloading… **0%**",
        )
        dl_start = time.time()
        dl_cb = make_download_progress_cb(status, filename, expected_size, loop)
        filepath = await asyncio.to_thread(
            download_terabox_file_experimental,
            download_url,
            filename,
            cancel_event,
            dl_cb,
            output_path,
        )
        dl_time = time.time() - dl_start
        file_size = os.path.getsize(filepath)
        if file_size <= 0:
            raise TeraBoxError("Downloaded file is empty")
        size_str = format_size(file_size)

        await _safe_send(
            status.edit,
            f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n📤 Uploading… **0%**",
        )
        up_start = time.time()
        up_cb = make_upload_progress_cb(status, filename, size_str, loop)
        sent = None
        for upload_attempt in range(2):
            try:
                sent = await _cancellable(
                    _safe_send(
                        bot.send_file,
                        event.chat_id,
                        filepath,
                        caption=f"📦 `{filename}`\n📐 Size: **{size_str}**",
                        attributes=[DocumentAttributeFilename(filename)],
                        supports_streaming=True,
                        reply_to=event.message.id,
                        progress_callback=up_cb,
                    ),
                    cancel_event,
                )
                break
            except ValueError as exc:
                is_short_read = "read less than" in str(exc)
                file_is_stable = (
                    os.path.exists(filepath)
                    and os.path.getsize(filepath) == file_size
                    and file_size > 0
                )
                if upload_attempt == 0 and is_short_read and file_is_stable:
                    log.warning("Telegram short-read on stable file; retrying upload once")
                    await _safe_send(
                        status.edit,
                        f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n"
                        "📤 Upload interrupted — retrying… **0%**",
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if sent is None:
            raise RuntimeError("Telegram upload did not return a message")
        up_time = time.time() - up_start
        total_time = time.time() - total_start
        await _safe_send(
            sent.edit,
            f"📦 `{filename}`\n📐 Size: **{size_str}**\n\n"
            f"⬇️ Download: **{format_duration(dl_time)}**\n"
            f"📤 Upload: **{format_duration(up_time)}**\n"
            f"⏱️ Total: **{format_duration(total_time)}**",
        )
        await _safe_send(status.delete)
    except (TeraBoxError, CancelledError) as exc:
        log.warning("Random URL download failed: %s", exc)
        await _safe_send(status.edit, "⚠️ This random video is no longer available. Try again!")
    except ValueError as exc:
        if "read less than" in str(exc):
            log.warning("Random upload file changed or became incomplete: %s", exc)
            await _safe_send(
                status.edit,
                "⚠️ The downloaded file became incomplete before upload. Please run /random again.",
            )
        else:
            log.exception("Random upload validation failed")
            await _safe_send(status.edit, "❌ Telegram rejected this video. Try /random again.")
    except Exception as exc:
        log.exception("Random video delivery failed")
        await _safe_send(status.edit, "❌ Could not deliver this random video. Try again!")
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
