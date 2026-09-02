import asyncio
import math
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections import defaultdict, deque
from urllib.parse import parse_qs, unquote, urlparse

import requests
from telethon import Button, events
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeFilename

from firebase_db.cache import add_to_cache, get_random_cache_record, get_runtime_cache_stats
from terabox.internal_helpers import _safe_filename
from terabox.public_api import CancelledError, TeraBoxError
from teraboxDL.public_api import download_terabox_file_experimental
from teraboxDL.terabox_dl import get_video_info
from diskwalaDL.public_api import get_diskwala_info
from flezen.public_api import get_flezen_info
from ..bot import _cancellable, _safe_send, active_tasks, bot, terabox_queue
from ..helpers import format_duration, format_size
from ..progress_callbacks import make_download_progress_cb, make_upload_progress_cb
from ..thumbnails import prepare_video_preview

log = logging.getLogger(__name__)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
RANDOM_LIMIT_PER_MINUTE = int(os.environ.get("RANDOM_LIMIT_PER_MINUTE", "6"))
RANDOM_LIMIT_WINDOW_SECONDS = 60
_random_usage = defaultdict(deque)


def _refresh_download_info(source_url: str, mode: str) -> dict | None:
    """Call the correct proxy to get a fresh download URL based on cache mode.

    Returns a dict with download_url/filename/size/thumbnail_url on success,
    or None if the mode has no proxy (e.g. 'get').
    """
    if mode in ("exp", "exphd"):
        return get_video_info(source_url, is_hd=(mode == "exphd"))
    if mode == "dw":
        return get_diskwala_info(source_url)
    if mode == "fz":
        return get_flezen_info(source_url)
    # 'get' mode is native download — no proxy available
    return None


def _random_wait_seconds(user_id: int | None) -> int:
    if not user_id or (ADMIN_ID and user_id == ADMIN_ID):
        return 0

    now = time.monotonic()
    timestamps = _random_usage[user_id]
    while timestamps and now - timestamps[0] >= RANDOM_LIMIT_WINDOW_SECONDS:
        timestamps.popleft()

    if len(timestamps) >= RANDOM_LIMIT_PER_MINUTE:
        return max(1, math.ceil(RANDOM_LIMIT_WINDOW_SECONDS - (now - timestamps[0])))

    timestamps.append(now)
    return 0


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
    """Resolve display data from local cache only; performs no DB/proxy call."""
    cached_url = record["download_url"]
    filename = str(record.get("filename") or "").strip()
    if not filename or filename == "unknown":
        filename = _filename_from_url(cached_url)
    return {
        "filename": filename,
        "download_url": cached_url,
        "size": int(record.get("file_size") or 0),
        "thumbnail_url": record.get("thumbnail_url"),
    }


@bot.on(events.NewMessage(pattern=r"^/random(?:@\S+)?$"))
async def cmd_random(event):
    log.info("Received /random command from chat %s", event.chat_id)
    wait_seconds = _random_wait_seconds(event.sender_id or event.chat_id)
    if wait_seconds > 0:
        await _safe_send(
            event.respond,
            f"⚠️ Your /random per-minute limit is reached. Kindly wait {wait_seconds}s "
            "and don't message again; each new message can increase your wait time.",
        )
        raise events.StopPropagation

    # Share the same fair, per-chat and global limits as link requests.
    # /random is deliberately queued because it can download and upload media.
    await terabox_queue.submit(_deliver_random, event, "/random")
    raise events.StopPropagation


async def _deliver_random(event, _queue_url: str = "/random"):
    waiting_status = None
    task_id = f"random:{event.message.id}"
    task_key = (event.chat_id, task_id)
    cancel_event = active_tasks.get(task_key)
    if cancel_event is None:
        cancel_event = threading.Event()
        active_tasks[task_key] = cancel_event
    elif cancel_event.is_set():
        active_tasks.pop(task_key, None)
        return
    cancel_btn = [[Button.inline("❌ Cancel", data=f"cancel:{task_id}")]]
    try:
        record = await asyncio.to_thread(get_random_cache_record)
    except Exception as exc:
        log.error("[/random] DB error: %s", exc)
        await event.respond("⚠️ **Database error.** Please try again in a moment.")
        active_tasks.pop(task_key, None)
        return

    if not record and get_runtime_cache_stats()["loading"]:
        active_tasks[task_key] = cancel_event
        waiting_status = await _safe_send(event.respond, "⏳ Media cache is warming up…", buttons=cancel_btn)
        # The first 20-shard batch normally arrives within a few seconds.
        for _ in range(10):
            if cancel_event.is_set():
                await _safe_send(waiting_status.edit, "🚫 Cancelled.")
                active_tasks.pop(task_key, None)
                return
            await asyncio.sleep(0.5)
            record = get_random_cache_record()
            if record:
                break

    if not record:
        message = "⏳ Media cache is still loading. Please run /random again shortly."
        if waiting_status:
            await _safe_send(waiting_status.edit, message)
            active_tasks.pop(task_key, None)
        else:
            await event.respond("📭 No videos yet. Send a supported link first!")
            active_tasks.pop(task_key, None)
        return

    if waiting_status:
        status = waiting_status
        await _safe_send(status.edit, "⏳ Fetching video information…", buttons=cancel_btn)
    else:
        status = await _safe_send(event.respond, "⏳ Fetching video information…", buttons=cancel_btn)
    loop = asyncio.get_running_loop()
    total_start = time.time()
    work_dir = None
    preview = None
    retry_for_flood = False

    try:
        info = await asyncio.to_thread(_resolve_media_info, record)
        if cancel_event.is_set():
            await _safe_send(status.edit, "🚫 Cancelled.")
            return
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
            buttons=cancel_btn,
        )
        dl_start = time.time()
        dl_cb = make_download_progress_cb(
            status, filename, expected_size, loop, cancel_btn, safe_send=_safe_send, chat_id=event.chat_id
        )
        filepath = await asyncio.to_thread(
            download_terabox_file_experimental,
            download_url,
            filename,
            cancel_event,
            dl_cb,
            output_path,
        )
        dl_time = time.time() - dl_start
        if cancel_event.is_set():
            await _safe_send(status.edit, "🚫 Cancelled.")
            return
        file_size = os.path.getsize(filepath)
        if file_size <= 0:
            raise TeraBoxError("Downloaded file is empty")
        size_str = format_size(file_size)

        await _safe_send(status.edit, f"📦 **{filename}**\n\n🖼 Preparing video preview…", buttons=cancel_btn)
        try:
            preview = await prepare_video_preview(filepath, info.get("thumbnail_url"))
        except Exception as exc:
            log.warning("Random preview generation failed: %s", exc)

        if cancel_event.is_set():
            await _safe_send(status.edit, "🚫 Cancelled.")
            return

        await _safe_send(
            status.edit,
            f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n📤 Uploading… **0%**",
            buttons=cancel_btn,
        )
        up_start = time.time()
        up_cb = make_upload_progress_cb(
            status, filename, size_str, loop, cancel_btn, safe_send=_safe_send, chat_id=event.chat_id
        )
        sent = None
        for upload_attempt in range(2):
            try:
                sent = await _cancellable(
                    _safe_send(
                        bot.send_file,
                        event.chat_id,
                        filepath,
                        caption=f"📦 `{filename}`\n📐 Size: **{size_str}**",
                        thumb=preview.thumbnail_path if preview else None,
                        attributes=(
                            preview.attributes(filename)
                            if preview else [DocumentAttributeFilename(filename)]
                        ),
                        supports_streaming=True,
                        reply_to=event.message.id,
                        progress_callback=up_cb,
                        max_retries=1,
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
                        buttons=cancel_btn,
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if sent is None:
            raise RuntimeError("Telegram upload did not return a message")
        up_time = time.time() - up_start
        total_time = time.time() - total_start
        try:
            await _safe_send(
                sent.edit,
                f"📦 `{filename}`\n📐 Size: **{size_str}**\n\n"
                f"⬇️ Download: **{format_duration(dl_time)}**\n"
                f"📤 Upload: **{format_duration(up_time)}**\n"
                f"⏱️ Total: **{format_duration(total_time)}**",
            )
        except Exception:
            pass
        try:
            await _safe_send(status.delete)
        except Exception:
            pass
    except asyncio.CancelledError:
        log.info("Random delivery cancelled by user for task=%s", task_id)
        await _safe_send(status.edit, "🚫 Cancelled.")
    except (TeraBoxError, CancelledError) as exc:
        if cancel_event.is_set():
            log.info("Random download cancelled by user for task=%s", task_id)
            await _safe_send(status.edit, "🚫 Cancelled.")
            return

        # The cached download_url has expired. Try to refresh it via the proxy
        # using the record's source_url, then retry the download once.
        source_url = record.get("source_url")
        record_mode = record.get("_mode", "exp")
        if not source_url or record_mode == "get":
            log.warning("Random URL download failed (no source_url or native mode to refresh): %s", exc)
            await _safe_send(status.edit, "⚠️ This random video is no longer available. Try again!")
            return

        log.info("Random cached URL expired; refreshing via proxy for source=%s mode=%s", source_url, record_mode)
        await _safe_send(status.edit, "♻️ Cached link expired. Refreshing metadata…", buttons=cancel_btn)
        try:
            refreshed_info = await asyncio.to_thread(_refresh_download_info, source_url, record_mode)
            if not refreshed_info:
                raise TeraBoxError(f"No proxy available for mode '{record_mode}'")
            download_url = refreshed_info["download_url"]
            filename = refreshed_info.get("filename") or filename
            refreshed_size = int(refreshed_info.get("size") or 0)
            thumbnail_url = refreshed_info.get("thumbnail_url")
            expected_size = format_size(refreshed_size) if refreshed_size else "Unknown"

            # Clean up the failed attempt's work_dir and create a fresh one
            if work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
            work_dir = tempfile.mkdtemp(prefix="random_", dir="storage")
            disk_name = _safe_filename(filename) or "random_video.mp4"
            if not disk_name.lower().endswith(".mp4"):
                disk_name += ".mp4"
            output_path = os.path.join(work_dir, disk_name)

            await _safe_send(
                status.edit,
                f"📦 **{filename}**\n📐 Size: **{expected_size}**\n\n⬇️ Downloading… **0%**",
                buttons=cancel_btn,
            )
            dl_start = time.time()
            dl_cb = make_download_progress_cb(
                status, filename, expected_size, loop, cancel_btn, safe_send=_safe_send, chat_id=event.chat_id
            )
            filepath = await asyncio.to_thread(
                download_terabox_file_experimental,
                download_url,
                filename,
                cancel_event,
                dl_cb,
                output_path,
            )
            dl_time = time.time() - dl_start

            if cancel_event.is_set():
                await _safe_send(status.edit, "🚫 Cancelled.")
                return

            file_size = os.path.getsize(filepath)
            if file_size <= 0:
                raise TeraBoxError("Downloaded file is empty")
            size_str = format_size(file_size)

            # Update the DB with the fresh download_url
            await asyncio.to_thread(
                add_to_cache, source_url, download_url, record_mode,
                filename, file_size, thumbnail_url,
            )
            log.info("Refreshed download_url cached for source=%s mode=%s", source_url, record_mode)

            # Continue with preview + upload (same as the happy path)
            await _safe_send(status.edit, f"📦 **{filename}**\n\n🖼 Preparing video preview…", buttons=cancel_btn)
            try:
                preview = await prepare_video_preview(filepath, thumbnail_url)
            except Exception as prev_exc:
                log.warning("Random preview generation failed: %s", prev_exc)

            if cancel_event.is_set():
                await _safe_send(status.edit, "🚫 Cancelled.")
                return

            await _safe_send(
                status.edit,
                f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n📤 Uploading… **0%**",
                buttons=cancel_btn,
            )
            up_start = time.time()
            up_cb = make_upload_progress_cb(
                status, filename, size_str, loop, cancel_btn, safe_send=_safe_send, chat_id=event.chat_id
            )
            sent = await _cancellable(
                _safe_send(
                    bot.send_file,
                    event.chat_id,
                    filepath,
                    caption=f"📦 `{filename}`\n📐 Size: **{size_str}**",
                    thumb=preview.thumbnail_path if preview else None,
                    attributes=(
                        preview.attributes(filename)
                        if preview else [DocumentAttributeFilename(filename)]
                    ),
                        supports_streaming=True,
                        reply_to=event.message.id,
                        progress_callback=up_cb,
                        max_retries=1,
                ),
                cancel_event,
            )
            up_time = time.time() - up_start
            total_time = time.time() - total_start
            try:
                await _safe_send(
                    sent.edit,
                    f"📦 `{filename}`\n📐 Size: **{size_str}**\n\n"
                    f"⬇️ Download: **{format_duration(dl_time)}**\n"
                    f"📤 Upload: **{format_duration(up_time)}**\n"
                    f"⏱️ Total: **{format_duration(total_time)}**",
                )
            except Exception:
                pass
            try:
                await _safe_send(status.delete)
            except Exception:
                pass
        except (CancelledError, asyncio.CancelledError):
            await _safe_send(status.edit, "🚫 Cancelled.")
        except FloodWaitError:
            raise
        except Exception as retry_exc:
            log.warning("Random URL refresh+retry also failed: %s", retry_exc)
            await _safe_send(status.edit, "⚠️ This random video is no longer available. Try again!")
    except FloodWaitError:
        retry_for_flood = True
        raise
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
        if preview:
            preview.cleanup()
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        if not retry_for_flood:
            active_tasks.pop(task_key, None)
