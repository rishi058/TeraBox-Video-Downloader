import time
import asyncio
import logging
from telethon.errors import FloodWaitError

log = logging.getLogger(__name__)

class MessageQueue:
    def __init__(self, concurrency_limit: int = 20):
        self.semaphore = asyncio.Semaphore(concurrency_limit)
        self._flood_until = 0.0
        self._queue = None
        self._worker_task = None
        self._monitor_task = None
        self._process_callable = None

    def set_processor(self, process_callable):
        self._process_callable = process_callable

    def update_flood_until(self, seconds: int) -> None:
        """Extend the global flood-wait cooldown if needed."""
        new_until = time.monotonic() + seconds
        self._flood_until = max(self._flood_until, new_until)

    def flood_remaining(self) -> int:
        """Seconds remaining in the current flood cooldown (0 if none)."""
        rem = self._flood_until - time.monotonic()
        return max(0, int(rem))

    async def _ensure_queue_worker(self) -> None:
        """Lazily start the background queue worker."""
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker())
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._queue_monitor())

    async def _queue_monitor(self) -> None:
        """Background task: periodically logs the queue size."""
        while True:
            if self._queue is not None:
                qsize = self._queue.qsize()
                log.info(f"[Queue Monitor] Items in queue: {qsize}")
            await asyncio.sleep(3)

    async def put(self, event, surl: str):
        await self._ensure_queue_worker()
        await self._queue.put((event, surl))

    async def _queue_worker(self) -> None:
        """Background task: drains the flood queue after cooldown expires."""
        log.info("[Queue] Flood-wait queue worker started.")
        while True:
            event, surl = await self._queue.get()
            try:
                # Wait until flood cooldown is over
                rem = self.flood_remaining()
                if rem > 0:
                    log.info(f"[Queue worker] Sleeping {rem}s for flood cooldown…")
                    await asyncio.sleep(rem)

                #! Small gap between items to be gentle on Telegram API
                await asyncio.sleep(3)

                # Process under the concurrency semaphore
                async with self.semaphore:
                    if self._process_callable:
                        await self._process_callable(event, surl)
                    else:
                        log.error("process_callable not set in MessageQueue")

            except FloodWaitError as e:
                self.update_flood_until(e.seconds)
                log.warning(
                    f"[Queue worker] Hit FloodWait again ({e.seconds}s), "
                    f"re-queuing surl={surl}"
                )
                await self._queue.put((event, surl))
            except Exception as ex:
                log.error(f"[Queue worker] Error for surl={surl}: {ex}")
                try:
                    await event.respond(f"❌ Failed to process `{surl}`: {ex}")
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    async def safe_send(self, coro_func, *args, max_retries=3, **kwargs):
        """
        Call a Telegram API coroutine; on FloodWaitError, update the global
        cooldown, sleep the required duration, and retry.
        Used for mid-pipeline calls where we must wait in place.
        """
        for attempt in range(1, max_retries + 1):
            try:
                return await coro_func(*args, **kwargs)
            except FloodWaitError as e:
                self.update_flood_until(e.seconds)
                log.warning(
                    f"[FloodWaitError] must wait {e.seconds}s "
                    f"(attempt {attempt}/{max_retries})"
                )
                if attempt == max_retries:
                    raise
                await asyncio.sleep(e.seconds)
        return await coro_func(*args, **kwargs)