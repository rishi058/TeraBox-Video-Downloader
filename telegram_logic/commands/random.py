import asyncio
import logging
import os
import random
import threading
import time
import re
from urllib.parse import unquote, urlparse

import requests
from telethon import events

from firebase_db.cache import get_cache_for_random
from terabox.public_api import CancelledError, TeraBoxError
from teraboxDL.public_api import download_terabox_file_experimental
from ..bot import _cancellable, _safe_send, bot
from ..helpers import format_duration, format_size
from ..progress_callbacks import make_download_progress_cb, make_upload_progress_cb

log = logging.getLogger(__name__)


def _filename_from_url(download_url: str) -> str:
    name = ""
    try:
        response = requests.head(download_url, allow_redirects=True, timeout=15)
        disposition = response.headers.get("Content-Disposition", "")
        encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
        plain = re.search(r'filename="?([^";]+)', disposition, re.IGNORECASE)
        if encoded:
            name = unquote(encoded.group(1))
        elif plain:
            name = plain.group(1).strip()
        if not name:
            name = unquote(os.path.basename(urlparse(response.url).path)).strip()
    except requests.RequestException:
        name = unquote(os.path.basename(urlparse(download_url).path)).strip()
    if not name or "." not in name:
        return "random_video.mp4"
    return os.path.basename(name)[:180]


@bot.on(events.NewMessage(pattern=r"^/random(?:@\S+)?$"))
async def cmd_random(event):
    log.info("Received /random command from chat %s", event.chat_id)
    try:
        records = await asyncio.to_thread(get_cache_for_random)
    except Exception as exc:
        log.error("[/random] DB error: %s", exc)
        await event.respond("⚠️ **Database error.** Please try again in a moment.")
        raise events.StopPropagation

    if not records:
        await event.respond("📭 No videos yet. Send a supported link first!")
        raise events.StopPropagation

    record = random.choice(records)
    download_url = record["download_url"]
    filename = await asyncio.to_thread(_filename_from_url, download_url)
    status = await _safe_send(
        event.respond,
        f"📦 **{filename}**\n\n⬇️ Downloading… **0%**",
    )
    loop = asyncio.get_running_loop()
    cancel_event = threading.Event()
    total_start = time.time()
    filepath = None

    try:
        dl_start = time.time()
        dl_cb = make_download_progress_cb(status, filename, "Unknown", loop)
        filepath = await asyncio.to_thread(
            download_terabox_file_experimental,
            download_url,
            filename,
            cancel_event,
            dl_cb,
        )
        dl_time = time.time() - dl_start
        size_str = format_size(os.path.getsize(filepath))

        await _safe_send(
            status.edit,
            f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n📤 Uploading… **0%**",
        )
        up_start = time.time()
        up_cb = make_upload_progress_cb(status, filename, size_str, loop)
        sent = await _cancellable(
            _safe_send(
                bot.send_file,
                event.chat_id,
                filepath,
                caption=f"📦 `{filename}`\n📐 Size: **{size_str}**",
                supports_streaming=True,
                reply_to=event.message.id,
                progress_callback=up_cb,
            ),
            cancel_event,
        )
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
    except Exception as exc:
        log.exception("Random video delivery failed")
        await _safe_send(status.edit, f"❌ Could not deliver random video: {exc}")
    finally:
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass

    raise events.StopPropagation
