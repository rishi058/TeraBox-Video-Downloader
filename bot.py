import os
import re
import json
import random
import asyncio
import time
import threading
import logging
from telethon import TelegramClient, events, Button
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from terabox import prepare_terabox_link, download_terabox_file, TeraBoxError, CancelledError

from dotenv import load_dotenv
load_dotenv()


# — Configuration —————————————————————————————————————————————————————————————
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_ID = int(os.environ.get("APP_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
STORAGE_GROUP_ID = int(os.environ.get("STORAGE_GROUP_ID", "0"))

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

# — Active-task tracking (for cancel) ————————————————————————————————————————————
active_tasks: dict[tuple[int, str], threading.Event] = {}

# — Local surl->message_id cache (avoids bot SearchRequest restriction) ————————
CACHE_FILE = "cache.json"
_cache_lock = threading.Lock()


def _load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _cache_put(surl: str, message_id: int) -> None:
    with _cache_lock:
        data = _load_cache()
        data[surl] = message_id
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)


def _cache_get(surl: str) -> int | None:
    with _cache_lock:
        return _load_cache().get(surl)

# — Helpers ————————————————————————————————————————————————————————————————————————

def extract_surl(text: str) -> str | None:
    """Extract the first SURL from a TeraBox URL in the message text."""
    m = TERA_URL_RE.search(text)
    if m:
        return m.group("surl_path") or m.group("surl_param")
    return None


def extract_all_surls(text: str) -> list[str]:
    """Extract all unique SURLs from TeraBox URLs in the message text."""
    seen: set[str] = set()
    surls: list[str] = []
    for m in TERA_URL_RE.finditer(text):
        surl = m.group("surl_path") or m.group("surl_param")
        if surl and surl not in seen:
            seen.add(surl)
            surls.append(surl)
    return surls


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


# — Progress callback for Telethon uploads —————————————————————————————————————————

def make_download_progress_cb(status_msg, filename, size_str, loop):
    """Create a progress callback for the download phase."""
    last_update = [0.0]

    async def _update(text):
        try:
            await status_msg.edit(text)
        except Exception:
            pass

    def callback(current, total):
        now = time.time()
        if now - last_update[0] < 3:
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


# — Bot setup ——————————————————————————————————————————————————————————————————————

bot = TelegramClient("terabox_bot", APP_ID, API_HASH)

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


async def upload_to_storage(filepath: str, filename: str, surl: str, progress_cb=None):
    """
    Upload a file to the storage group.
    Caption format: 'surl:<surl>\n<filename>' — used as the cache key.
    Returns the sent Message.
    """
    return await bot.send_file(
        STORAGE_GROUP_ID,
        filepath,
        caption=f"surl:{surl}\n{filename}",
        supports_streaming=True,
        progress_callback=progress_cb,
    )


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


@bot.on(events.NewMessage(pattern="/info"))
async def cmd_info(event):
    sender = await event.get_sender()
    chat = await event.get_chat()

    user_id = sender.id if sender else "N/A"
    username = f"@{sender.username}" if (sender and sender.username) else "none"
    first_name = getattr(sender, "first_name", "") or ""
    last_name = getattr(sender, "last_name", "") or ""
    full_name = (first_name + (" " + last_name if last_name else "")).strip() or "N/A"

    chat_id = chat.id if chat else "N/A"
    chat_title = getattr(chat, "title", None)
    chat_username = getattr(chat, "username", None)

    if chat_title:
        chat_type = "Channel" if getattr(chat, "broadcast", False) else "Group/Supergroup"
        chat_info = (
            f"🏠 **Chat:** {chat_title}\n"
            f"🆔 **Chat ID:** `{chat_id}`\n"
            f"🔗 **Chat username:** {'@' + chat_username if chat_username else 'none'}\n"
            f"📂 **Type:** {chat_type}"
        )
    else:
        chat_info = (
            f"🏠 **Chat:** Private\n"
            f"🆔 **Chat ID:** `{chat_id}`"
        )

    msg_id = event.message.id

    text = (
        "ℹ️ **Info**\n\n"
        "👤 **User**\n"
        f"• ID: `{user_id}`\n"
        f"• Name: {full_name}\n"
        f"• Username: {username}\n\n"
        "💬 **Chat**\n"
        + chat_info + "\n\n"
        f"✉️ **Message ID:** `{msg_id}`"
    )

    await event.respond(text)
    raise events.StopPropagation


@bot.on(events.CallbackQuery(data=b"cancel_download"))
async def handle_cancel(event):
    chat_id = event.chat_id
    tasks = {k: v for k, v in active_tasks.items() if k[0] == chat_id}
    cancelled = [v for v in tasks.values() if not v.is_set()]
    for ev in cancelled:
        ev.set()
    if cancelled:
        await event.answer(f"🚫 Cancelling {len(cancelled)} download(s)...")
    else:
        await event.answer("Nothing to cancel.")


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
            storage_msg = await upload_to_storage(filepath, filename, surl, progress_cb)
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


@bot.on(events.NewMessage(pattern="/random"))
async def cmd_random(event):
    with _cache_lock:
        data = _load_cache()
    if not data:
        await event.respond("📭 No cached videos yet. Send a TeraBox link first!")
        raise events.StopPropagation

    surl, msg_id = random.choice(list(data.items()))
    cached_msg = None
    if STORAGE_GROUP_ID:
        try:
            cached_msg = await bot.get_messages(STORAGE_GROUP_ID, ids=msg_id)
            if cached_msg and not (cached_msg.video or (
                cached_msg.document
                and cached_msg.document.mime_type
                and "video" in cached_msg.document.mime_type
            )):
                cached_msg = None
        except Exception as e:
            log.warning(f"Random cache fetch failed for surl={surl} msg_id={msg_id}: {e}")
            cached_msg = None

    if cached_msg is None:
        await event.respond("⚠️ Could not retrieve a cached video. Try again!")
        raise events.StopPropagation

    f = cached_msg.file
    fname = (f.name if f and f.name else surl)
    fsize = (format_size(f.size) if f and f.size else "N/A")
    caption = f"🎲 **Random from cache**\n\n📦 `{fname}`\n📐 Size: **{fsize}**"
    await bot.send_file(
        event.chat_id, cached_msg.media,
        caption=caption, supports_streaming=True, reply_to=event.message.id,
    )
    raise events.StopPropagation


@bot.on(events.NewMessage(pattern=r"^/get(?:@\S+)?(?:\s+(.+))?$"))
async def cmd_get(event):
    arg = (event.pattern_match.group(1) or "").strip()
    surls = extract_all_surls(arg) if arg else []
    if not surls:
        await event.respond(
            "Usage: `/get <TeraBox URL>`\n\nExample:\n`/get https://1024tera.com/s/1XXXX`"
        )
        raise events.StopPropagation
    await asyncio.gather(*[_process_terabox(event, surl) for surl in surls])
    raise events.StopPropagation


@bot.on(events.NewMessage)
async def handle_message(event):
    text = event.raw_text or ""
    surls = extract_all_surls(text)
    if not surls:
        return  # silently ignore non-TeraBox messages
    await asyncio.gather(*[_process_terabox(event, surl) for surl in surls])

# — Entry point ————————————————————————————————————————————————————————————————————

async def main() -> None:
    if not BOT_TOKEN or not APP_ID or not API_HASH:
        print("ERROR: Set BOT_TOKEN, APP_ID, and API_HASH in your .env file!")
        return

    if not STORAGE_GROUP_ID:
        log.warning("STORAGE_GROUP_ID not set — caching disabled, videos will be sent directly.")

    await bot.start(bot_token=BOT_TOKEN)

    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="get", description="Download a TeraBox video"),
            BotCommand(command="random", description="Get a random cached video"),
            BotCommand(command="info", description="Show chat and user info"),
        ],
    ))
    log.info("Bot commands registered.")
    log.info("Bot started! Waiting for messages... (Ctrl+C to stop)")

    try:
        await bot.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    finally:
        log.info("Shutting down...")
        if bot.is_connected():
            await bot.disconnect()
        log.info("Bye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
