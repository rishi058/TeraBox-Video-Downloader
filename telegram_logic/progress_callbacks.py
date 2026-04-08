import time
import asyncio
from .helpers import format_size

# — Progress callback for Telethon uploads —————————————————————————————————————————

def make_download_progress_cb(status_msg, filename, size_str, loop, cancel_btn=None):
    """Create a progress callback for the download phase."""
    last_update = [0.0]

    async def _update(text):
        try:
            await status_msg.edit(text, buttons=cancel_btn)
        except Exception:
            pass

    def callback(current, total):
        now = time.time()
        # Update every 3 seconds, or when the transfer is complete (current == total)
        if (now - last_update[0] < 5) and (current < total):
            return
        last_update[0] = now
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


def make_upload_progress_cb(status_msg, filename, size_str, loop, cancel_btn=None):
    """Create a progress callback for Telethon file upload."""
    last_update = [0.0]  # track last update time to avoid flooding

    async def _update(text):
        try:
            await status_msg.edit(text, buttons=cancel_btn)
        except Exception:
            pass

    def callback(current, total):
        now = time.time()
        # Update every 3 seconds, or when the transfer is complete (current == total)
        if (now - last_update[0] < 5) and (current < total):
            return
        last_update[0] = now
        pct = current / total * 100 if total else 0
        uploaded = format_size(current)
        text = (
            f"📦 **{filename}**\n"
            f"📐 Size: **{size_str}**\n\n"
            f"📤 Uploading… **{pct:.0f}%** ({uploaded} / {size_str})"
        )
        asyncio.run_coroutine_threadsafe(_update(text), loop)

    return callback