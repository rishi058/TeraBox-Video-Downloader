import os
import threading
import asyncio
import logging
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

from .queue import MessageQueue

# — Concurrency & Flood-Wait Queue ————————————————————————————————————————————
# We still need a semaphore because:
# 1. Unbounded concurrency (e.g. 50 links) will instantly trigger FloodWait before any work gets done.
# 2. Downloading/Uploading 50 videos concurrently will crash a low-spec VPS (OOM or CPU exhaustion).
# Keep this high enough to saturate the VPS, but low enough to avoid OOM/disk thrash.
terabox_queue = MessageQueue(concurrency_limit=int(os.environ.get("TELEGRAM_CONCURRENCY_LIMIT", "20")))

def _infer_chat_id(coro_func, args):
    owner = getattr(coro_func, "__self__", None)
    for attr in ("chat_id",):
        chat_id = getattr(owner, attr, None)
        if chat_id is not None:
            return chat_id
    name = getattr(coro_func, "__name__", "")
    if name in {"send_file", "send_message"} and args:
        return args[0]
    return None

def _infer_chat_kind(coro_func):
    owner = getattr(coro_func, "__self__", None)
    if getattr(owner, "is_group", False):
        return "group"
    return None

async def _safe_send(*args, **kwargs):
    if not args:
        return await terabox_queue.safe_send(*args, **kwargs)

    coro_func = args[0]
    name = getattr(coro_func, "__name__", "")
    chat_id = kwargs.setdefault("chat_id", _infer_chat_id(coro_func, args[1:]))
    kwargs.setdefault("chat_kind", _infer_chat_kind(coro_func))
    kwargs.setdefault("include_chat_rate", name not in {"edit", "delete"})

    notify_methods = {"respond", "reply", "send_file", "send_message"}
    include_chat_rate = kwargs.get("include_chat_rate", True)
    chat_kind = kwargs.get("chat_kind")
    scope, wait, rate = terabox_queue.send_wait_info(chat_id, chat_kind, include_chat_rate)
    if name in notify_methods and terabox_queue.should_send_rate_notice(chat_id, scope, wait):
        try:
            await bot.send_message(
                chat_id,
                f"⏳ Message rate limit reached ({scope}: {rate:g} msg/sec). "
                f"Kindly wait ~{int(wait) + 1}s and don't message again; "
                "each new message can increase your wait time.",
            )
        except FloodWaitError as exc:
            terabox_queue.update_flood_until(exc.seconds, chat_id)
        except Exception:
            pass

    result = await terabox_queue.safe_send(*args, **kwargs)
    if name == "send_file":
        schedule_message_deletion(result, chat_id)
    return result

# — Configuration —————————————————————————————————————————————————————————————
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_ID = int(os.environ.get("APP_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")

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
    flood_sleep_threshold=0,
)


MEDIA_DELIVERY_NOTICE = (
    "⚠️ **Media will be automatically deleted after 30 minutes.**\n"
    "📤 **Forward it to your private groups.**"
)
MEDIA_AUTO_DELETE_SECONDS = max(
    0, int(os.environ.get("TELEGRAM_MEDIA_AUTO_DELETE_SECONDS", "1800"))
)


async def _delete_media_after(message, chat_id) -> None:
    await asyncio.sleep(MEDIA_AUTO_DELETE_SECONDS)
    try:
        await terabox_queue.safe_send(
            message.delete,
            chat_id=chat_id,
            include_chat_rate=False,
        )
    except Exception as exc:
        log.info("Could not auto-delete media in chat=%s: %s", chat_id, exc)


def schedule_message_deletion(result, chat_id) -> None:
    """Schedule deletion for each delivered media or notice message."""
    if MEDIA_AUTO_DELETE_SECONDS <= 0:
        return
    messages = result if isinstance(result, (list, tuple)) else (result,)
    for message in messages:
        if hasattr(message, "delete"):
            asyncio.create_task(_delete_media_after(message, chat_id))


async def send_media_notice(event) -> None:
    """Send one expiring delivery notice for an incoming user message."""
    try:
        notice = await terabox_queue.safe_send(
            event.respond,
            MEDIA_DELIVERY_NOTICE,
            chat_id=event.chat_id,
            chat_kind="group" if getattr(event, "is_group", False) else None,
        )
        schedule_message_deletion(notice, event.chat_id)
    except Exception as exc:
        log.info("Could not send media notice in chat=%s: %s", event.chat_id, exc)


async def _cancellable(coro, cancel_event: threading.Event, poll_interval: float = 0.5):
    """
    Run `coro` as a task while polling `cancel_event` (threading.Event).
    If the event is set, cancel the task immediately.
    Raises asyncio.CancelledError on cancellation.
    """
    task = asyncio.ensure_future(coro)
    while not task.done():
        if cancel_event.is_set():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise asyncio.CancelledError("Upload cancelled by user")
        await asyncio.sleep(poll_interval)
    return task.result()

