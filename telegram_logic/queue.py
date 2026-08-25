import os
import random
import time
import asyncio
import logging
from telethon.errors import FloodWaitError

log = logging.getLogger(__name__)

class MessageQueue:
    def __init__(self, concurrency_limit: int = 20):
        self.semaphore = asyncio.Semaphore(concurrency_limit)
        self.global_send_rate = float(os.environ.get("TELEGRAM_GLOBAL_SEND_RATE", "28"))
        self.private_chat_rate = float(os.environ.get("TELEGRAM_PRIVATE_CHAT_RATE", "1"))
        self.group_chat_rate = float(os.environ.get("TELEGRAM_GROUP_CHAT_RATE", "0.333"))
        self.queue_item_delay = float(os.environ.get("TELEGRAM_QUEUE_ITEM_DELAY", "2"))
        self.queue_item_jitter = float(os.environ.get("TELEGRAM_QUEUE_ITEM_JITTER", "1"))
        self.rate_notice_threshold = float(os.environ.get("TELEGRAM_RATE_NOTICE_THRESHOLD", "2"))
        self.rate_notice_cooldown = float(os.environ.get("TELEGRAM_RATE_NOTICE_COOLDOWN", "20"))
        self._global_flood_until = 0.0
        self._chat_flood_until = {}
        self._global_next_send_at = 0.0
        self._chat_next_send_at = {}
        self._rate_notice_until = {}
        self._global_rate_lock = asyncio.Lock()
        self._chat_rate_locks = {}
        self._queue = None
        self._worker_task = None
        self._monitor_task = None

    def _chat_key(self, chat_id=None):
        return str(chat_id) if chat_id is not None else None

    def _chat_rate(self, chat_kind: str | None = None) -> float:
        return self.group_chat_rate if chat_kind == "group" else self.private_chat_rate

    def _queue_item_sleep(self) -> float:
        jitter = random.uniform(-self.queue_item_jitter, self.queue_item_jitter)
        return max(0.0, self.queue_item_delay + jitter)

    def update_flood_until(self, seconds: int, chat_id=None) -> None:
        """Extend the flood-wait cooldown for one chat, or globally if unknown."""
        new_until = time.monotonic() + seconds
        chat_key = self._chat_key(chat_id)
        if chat_key is None:
            self._global_flood_until = max(self._global_flood_until, new_until)
            return
        self._chat_flood_until[chat_key] = max(
            self._chat_flood_until.get(chat_key, 0.0), new_until
        )

    def flood_remaining(self, chat_id=None) -> int:
        """Seconds remaining in the current flood cooldown (0 if none)."""
        now = time.monotonic()
        global_rem = self._global_flood_until - now
        chat_key = self._chat_key(chat_id)
        chat_rem = 0.0
        if chat_key is not None:
            chat_rem = self._chat_flood_until.get(chat_key, 0.0) - now
        return max(0, int(max(global_rem, chat_rem)))

    async def _wait_for_flood_window(self, chat_id=None) -> None:
        rem = self.flood_remaining(chat_id)
        if rem > 0:
            log.info(f"[Flood limiter] Sleeping {rem}s for chat={chat_id or 'global'}")
            await asyncio.sleep(rem)

    def send_wait_info(self, chat_id=None, chat_kind: str | None = None, include_chat_rate: bool = True):
        now = time.monotonic()
        global_wait = max(0.0, self._global_next_send_at - now)
        scope = "global"
        wait = global_wait
        rate = self.global_send_rate

        chat_key = self._chat_key(chat_id)
        if include_chat_rate and chat_key is not None:
            chat_wait = max(0.0, self._chat_next_send_at.get(chat_key, 0.0) - now)
            if chat_wait >= wait:
                scope = "group" if chat_kind == "group" else "chat"
                wait = chat_wait
                rate = self._chat_rate(chat_kind)

        return scope, wait, rate

    def should_send_rate_notice(self, chat_id, scope: str, wait: float) -> bool:
        if chat_id is None or wait < self.rate_notice_threshold:
            return False

        now = time.monotonic()
        key = (self._chat_key(chat_id), scope)
        if self._rate_notice_until.get(key, 0.0) > now:
            return False

        self._rate_notice_until[key] = now + self.rate_notice_cooldown
        return True

    async def _wait_for_send_slot(self, chat_id=None, chat_kind: str | None = None, include_chat_rate: bool = True) -> None:
        chat_key = self._chat_key(chat_id)
        if include_chat_rate and chat_key is not None:
            chat_rate = self._chat_rate(chat_kind)
            chat_interval = 1.0 / chat_rate if chat_rate > 0 else 0.0
            lock = self._chat_rate_locks.setdefault(chat_key, asyncio.Lock())
            async with lock:
                now = time.monotonic()
                wait = self._chat_next_send_at.get(chat_key, 0.0) - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                self._chat_next_send_at[chat_key] = now + chat_interval

        global_interval = 1.0 / self.global_send_rate if self.global_send_rate > 0 else 0.0
        async with self._global_rate_lock:
            now = time.monotonic()
            wait = self._global_next_send_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._global_next_send_at = now + global_interval

    async def _ensure_queue_worker(self) -> None:
        """Lazily start the background queue worker."""
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker())
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._queue_monitor())

    async def _queue_monitor(self) -> None:
        """Background task: periodically logs a non-empty queue size."""
        while True:
            if self._queue is not None:
                qsize = self._queue.qsize()
                if qsize > 0:
                    log.info(f"[Queue Monitor] Items in queue: {qsize}")
            await asyncio.sleep(3)

    async def put(self, process_callable, event, url: str, *args):
        await self._ensure_queue_worker()
        await self._queue.put((process_callable, event, url, args))

    async def _queue_worker(self) -> None:
        """Background task: drains the flood queue after cooldown expires."""
        log.info("[Queue] Flood-wait queue worker started.")
        while True:
            process_callable, event, url, args = await self._queue.get()
            try:
                # Wait until flood cooldown is over
                chat_id = getattr(event, "chat_id", None)
                rem = self.flood_remaining(chat_id)
                if rem > 0:
                    log.info(f"[Queue worker] Sleeping {rem}s for chat={chat_id} flood cooldown…")
                    await asyncio.sleep(rem)

                await asyncio.sleep(self._queue_item_sleep())

                # Process under the concurrency semaphore
                async with self.semaphore:
                    if process_callable:
                        await process_callable(event, url, *args)
                    else:
                        log.error("process_callable not set correctly in MessageQueue")

            except FloodWaitError as e:
                self.update_flood_until(e.seconds, getattr(event, "chat_id", None))
                log.warning(
                    f"[Queue worker] Hit FloodWait again ({e.seconds}s), "
                    f"re-queuing url={url}"
                )
                await self._queue.put((process_callable, event, url, args))
            except Exception as ex:
                log.error(f"[Queue worker] Error for url={url}: {ex}")
                try:
                    await event.respond(f"❌ Failed to process `{url}`: {ex}")
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    async def safe_send(
        self,
        coro_func,
        *args,
        chat_id=None,
        chat_kind: str | None = None,
        include_chat_rate: bool = True,
        max_retries=3,
        **kwargs,
    ):
        """
        Call a Telegram API coroutine; on FloodWaitError, update the target
        chat cooldown when known, sleep the required duration, and retry.
        Used for mid-pipeline calls where we must wait in place.
        """
        for attempt in range(1, max_retries + 1):
            try:
                await self._wait_for_flood_window(chat_id)
                await self._wait_for_send_slot(chat_id, chat_kind, include_chat_rate)
                return await coro_func(*args, **kwargs)
            except FloodWaitError as e:
                self.update_flood_until(e.seconds, chat_id)
                log.warning(
                    f"[FloodWaitError] must wait {e.seconds}s for chat={chat_id or 'global'} "
                    f"(attempt {attempt}/{max_retries})"
                )
                if attempt == max_retries:
                    raise
                await asyncio.sleep(e.seconds)
        return await coro_func(*args, **kwargs)