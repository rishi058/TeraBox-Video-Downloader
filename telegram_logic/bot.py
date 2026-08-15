import os
import time 
import threading
import asyncio
import logging
from telethon import TelegramClient

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

from .queue import MessageQueue

# — Concurrency & Flood-Wait Queue ————————————————————————————————————————————
# We still need a semaphore because:
# 1. Unbounded concurrency (e.g. 50 links) will instantly trigger FloodWait before any work gets done.
# 2. Downloading/Uploading 50 videos concurrently will crash a low-spec VPS (OOM or CPU exhaustion).
# 10 is a good high-capacity limit that balances speed with server stability.
terabox_queue = MessageQueue(concurrency_limit=20)

async def _safe_send(*args, **kwargs):
    return await terabox_queue.safe_send(*args, **kwargs)

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

