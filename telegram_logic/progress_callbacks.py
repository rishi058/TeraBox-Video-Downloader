import os
import random
import time
import asyncio
from .helpers import format_size

# — Progress callback for Telethon uploads —————————————————————————————————————————

PROGRESS_UPDATE_INTERVAL = float(os.environ.get("TELEGRAM_PROGRESS_UPDATE_INTERVAL", "5"))
PROGRESS_UPDATE_JITTER = float(os.environ.get("TELEGRAM_PROGRESS_UPDATE_JITTER", "2"))


def _next_progress_delay() -> float:
    jitter = random.uniform(-PROGRESS_UPDATE_JITTER, PROGRESS_UPDATE_JITTER)
    return max(1.0, PROGRESS_UPDATE_INTERVAL + jitter)

def make_download_progress_cb(status_msg, filename, size_str, loop, cancel_btn=None, safe_send=None, chat_id=None):
    """Create a progress callback for the download phase."""
    next_update_at = [time.time() + random.uniform(0, PROGRESS_UPDATE_JITTER)]

    async def _update(text):
        try:
            if safe_send:
                await safe_send(status_msg.edit, text, buttons=cancel_btn, chat_id=chat_id)
            else:
                await status_msg.edit(text, buttons=cancel_btn)
        except Exception:
            pass

    def callback(current, total):
        now = time.time()
        if (now < next_update_at[0]) and (current < total):
            return
        next_update_at[0] = now + _next_progress_delay()
        pct = current / total * 100 if total else 0
        downloaded = format_size(current)
        total_str = format_size(total) if total else size_str
        text = (
            f"📦 **{filename}**\n"
            f"📐 Size: **{total_str}**\n\n"
            f"⬇️ Downloading… **{pct:.0f}%** ({downloaded} / {total_str})"
        )
        asyncio.run_coroutine_threadsafe(_update(text), loop)

    return callback


def make_upload_progress_cb(status_msg, filename, size_str, loop, cancel_btn=None, safe_send=None, chat_id=None):
    """Create a progress callback for Telethon file upload."""
    next_update_at = [time.time() + random.uniform(0, PROGRESS_UPDATE_JITTER)]

    async def _update(text):
        try:
            if safe_send:
                await safe_send(status_msg.edit, text, buttons=cancel_btn, chat_id=chat_id)
            else:
                await status_msg.edit(text, buttons=cancel_btn)
        except Exception:
            pass

    def callback(current, total):
        now = time.time()
        if (now < next_update_at[0]) and (current < total):
            return
        next_update_at[0] = now + _next_progress_delay()
        pct = current / total * 100 if total else 0
        uploaded = format_size(current)
        text = (
            f"📦 **{filename}**\n"
            f"📐 Size: **{size_str}**\n\n"
            f"📤 Uploading… **{pct:.0f}%** ({uploaded} / {size_str})"
        )
        asyncio.run_coroutine_threadsafe(_update(text), loop)

    return callback