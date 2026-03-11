import os
import re
import asyncio
import time
import threading
import logging
from telethon import TelegramClient, events, Button
from terabox import (
    prepare_terabox_link, download_terabox_file,
    TeraBoxError, CancelledError,
)

from dotenv import load_dotenv
load_dotenv()


# ─── Configuration ────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_ID = int(os.environ.get("APP_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO,
)
log = logging.getLogger(__name__)

# Regex to match TeraBox share URLs and extract the SURL
TERA_URL_RE = re.compile(
    r"https?://(?:www\.|dm\.|dl\.)?(?:1024tera|1024terabox|terabox|teraboxapp|4funbox)\.com"
    r"(?:/s/1(?P<surl_path>[A-Za-z0-9_-]+)"
    r"|/(?:sharing/link|wap/share/filelist)\?[^#]*surl=(?P<surl_param>[A-Za-z0-9_-]+))",
    re.IGNORECASE,
)

# ─── Active-task tracking (for cancel) ────────────────────────────────────────
active_tasks: dict[int, threading.Event] = {}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_surl(text: str) -> str | None:
    """Extract SURL from a TeraBox URL in the message text."""
    m = TERA_URL_RE.search(text)
    if m:
        return m.group("surl_path") or m.group("surl_param")
    return None


def format_size(size_bytes: int) -> str:
    """Format bytes into a human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    else:
        return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 1:
        return f"{seconds:.1f}s"
    minutes = int(seconds) // 60
    secs = seconds - (minutes * 60)
    if minutes > 0:
        return f"{minutes}m {secs:.1f}s"
    return f"{secs:.1f}s"


# ─── Progress callback for Telethon uploads ───────────────────────────────────

def make_upload_progress_cb(status_msg, filename, size_str, loop):
    """Create a progress callback for Telethon file upload."""
    last_update = [0.0]  # track last update time to avoid flooding

    async def _update(text):
        try:
            await status_msg.edit(text)
        except Exception:
            pass

    def callback(current, total):
        now = time.time()
        if now - last_update[0] < 3:  # update at most every 3 seconds
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


# ─── Bot setup ────────────────────────────────────────────────────────────────

bot = TelegramClient("terabox_bot", APP_ID, API_HASH)


@bot.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    await event.respond(
        "Send me a TeraBox link and I'll send you the video!\n\n"
        "Supported formats:\n"
        "• https://1024tera.com/s/1XXXX\n"
        "• https://www.1024tera.com/sharing/link?surl=XXXX\n"
        "• https://teraboxapp.com/s/1XXXX"
    )
    raise events.StopPropagation


@bot.on(events.CallbackQuery(data=b"cancel_download"))
async def handle_cancel(event):
    chat_id = event.chat_id
    cancel_event = active_tasks.get(chat_id)
    if cancel_event and not cancel_event.is_set():
        cancel_event.set()
        await event.answer("🚫 Cancelling...")
    else:
        await event.answer("Nothing to cancel.")


@bot.on(events.NewMessage)
async def handle_message(event):
    text = event.raw_text or ""
    surl = extract_surl(text)

    if not surl:
        return  # silently ignore non-TeraBox messages

    chat_id = event.chat_id
    total_start = time.time()

    # Cancel event for this task
    cancel_event = threading.Event()
    active_tasks[chat_id] = cancel_event

    cancel_btn = [[Button.inline("❌ Cancel", data="cancel_download")]]

    status = await event.respond(
        f"⏳ Processing link (`{surl}`)...",
        buttons=cancel_btn,
    )

    # ── Phase 1: Prepare (fetch metadata + dlink) ────────────────────────
    try:
        prepared = await asyncio.to_thread(prepare_terabox_link, surl)
    except CancelledError:
        await status.edit("🚫 Cancelled.")
        active_tasks.pop(chat_id, None)
        return
    except TeraBoxError as e:
        log.error(f"TeraBox error for surl={surl}: {e}")
        await status.edit(f"❌ Error: {e}")
        active_tasks.pop(chat_id, None)
        return
    except Exception as e:
        log.exception(f"Unexpected error preparing surl={surl}")
        await status.edit(f"❌ Unexpected error: {e}")
        active_tasks.pop(chat_id, None)
        return

    if cancel_event.is_set():
        await status.edit("🚫 Cancelled.")
        active_tasks.pop(chat_id, None)
        return

    filename = prepared["filename"]
    filesize = prepared["size"]
    cached = prepared["cached_path"] is not None

    size_str = format_size(filesize)
    cache_tag = "  _(cached)_" if cached else ""
    await status.edit(
        f"📦 **{filename}**\n"
        f"📐 Size: **{size_str}**{cache_tag}\n\n"
        f"⬇️ Downloading...",
        buttons=cancel_btn,
    )

    # ── Phase 2: Download ────────────────────────────────────────────────
    dl_start = time.time()
    try:
        filepath = await asyncio.to_thread(
            download_terabox_file, prepared, cancel_event
        )
    except CancelledError:
        await status.edit("🚫 Cancelled.")
        active_tasks.pop(chat_id, None)
        return
    except TeraBoxError as e:
        log.error(f"Download error for surl={surl}: {e}")
        await status.edit(f"❌ Download failed: {e}")
        active_tasks.pop(chat_id, None)
        return
    except Exception as e:
        log.exception(f"Unexpected download error for surl={surl}")
        await status.edit(f"❌ Download failed: {e}")
        active_tasks.pop(chat_id, None)
        return
    dl_time = time.time() - dl_start

    if cancel_event.is_set():
        await status.edit("🚫 Cancelled.")
        active_tasks.pop(chat_id, None)
        return

    # ── Phase 3: Upload (Telethon — supports up to 2 GB) ────────────────
    await status.edit(
        f"📦 **{filename}**\n"
        f"📐 Size: **{size_str}**\n\n"
        f"📤 Uploading to Telegram... **0%**",
    )

    loop = asyncio.get_event_loop()
    progress_cb = make_upload_progress_cb(status, filename, size_str, loop)

    up_start = time.time()
    try:
        await bot.send_file(
            event.chat_id,
            filepath,
            caption=filename,
            supports_streaming=True,
            progress_callback=progress_cb,
            reply_to=event.message.id,
        )
    except Exception as e:
        log.error(f"Upload error for surl={surl}: {e}")
        await status.edit(f"❌ Upload failed: {e}")
        active_tasks.pop(chat_id, None)
        return
    up_time = time.time() - up_start
    total_time = time.time() - total_start

    # ── Phase 4: Summary ─────────────────────────────────────────────────
    dl_label = "cached" if cached else format_duration(dl_time)
    summary = (
        f"✅ **Done!**\n\n"
        f"📦 `{filename}`\n"
        f"📐 Size: **{size_str}**\n\n"
        f"⬇️ Download: **{dl_label}**\n"
        f"📤 Upload: **{format_duration(up_time)}**\n"
        f"⏱️ Total: **{format_duration(total_time)}**"
    )
    await status.edit(summary)
    active_tasks.pop(chat_id, None)


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    if not BOT_TOKEN or not APP_ID or not API_HASH:
        print("ERROR: Set BOT_TOKEN, APP_ID, and API_HASH in your .env file!")
        return

    await bot.start(bot_token=BOT_TOKEN)
    log.info("Bot started! Waiting for messages...")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
