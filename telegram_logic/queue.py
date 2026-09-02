import asyncio
import logging
import math
import os
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable

from telethon.errors import FloodWaitError

log = logging.getLogger(__name__)


@dataclass
class QueueItem:
    process_callable: Callable[..., Any]
    event: Any
    url: str
    args: tuple[Any, ...]
    chat_key: str
    group_user_key: tuple[str, str] | None
    enqueued_at: float
    retries: int = 0


class MessageQueue:
    """Fair, bounded request scheduler with global and per-chat concurrency."""

    def __init__(self, concurrency_limit: int = 20):
        self.concurrency_limit = max(1, concurrency_limit)
        self.per_chat_concurrency = max(1, int(os.environ.get("PER_CHAT_CONCURRENCY", "1")))
        self.per_chat_pending_limit = max(0, int(os.environ.get("PER_CHAT_PENDING_LIMIT", "20")))
        self.global_pending_limit = max(0, int(os.environ.get("GLOBAL_PENDING_LIMIT", "500")))
        self.per_user_group_pending_limit = max(
            0, int(os.environ.get("PER_USER_GROUP_PENDING_LIMIT", "5"))
        )
        self.max_queue_wait = max(
            0.0, float(os.environ.get("TELEGRAM_QUEUE_MAX_WAIT_SECONDS", "1800"))
        )

        # Semaphores enforce rolling concurrency. Scheduler counters prevent a
        # large number of tasks from accumulating while waiting on them.
        self.semaphore = asyncio.Semaphore(self.concurrency_limit)
        self._chat_semaphores: dict[str, asyncio.Semaphore] = {}
        self._active_total = 0
        self._chat_active: dict[str, int] = defaultdict(int)

        self.global_send_rate = float(os.environ.get("TELEGRAM_GLOBAL_SEND_RATE", "28"))
        self.private_chat_rate = float(os.environ.get("TELEGRAM_PRIVATE_CHAT_RATE", "1"))
        self.group_chat_rate = float(os.environ.get("TELEGRAM_GROUP_CHAT_RATE", "0.333"))
        self.queue_item_delay = float(os.environ.get("TELEGRAM_QUEUE_ITEM_DELAY", "2"))
        self.queue_item_jitter = float(os.environ.get("TELEGRAM_QUEUE_ITEM_JITTER", "1"))
        self.rate_notice_threshold = float(os.environ.get("TELEGRAM_RATE_NOTICE_THRESHOLD", "2"))
        self.rate_notice_cooldown = float(os.environ.get("TELEGRAM_RATE_NOTICE_COOLDOWN", "20"))

        self._global_flood_until = 0.0
        self._chat_flood_until: dict[str, float] = {}
        self._global_next_send_at = 0.0
        self._chat_next_send_at: dict[str, float] = {}
        self._rate_notice_until: dict[tuple[str, str], float] = {}
        self._global_rate_lock = asyncio.Lock()
        self._chat_rate_locks: dict[str, asyncio.Lock] = {}

        self._chat_queues: dict[str, deque[QueueItem]] = defaultdict(deque)
        self._ready_chats: deque[str] = deque()
        self._ready_chat_set: set[str] = set()
        self._pending_total = 0
        self._group_user_pending: dict[tuple[str, str], int] = defaultdict(int)
        self._wake_event = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None

    def _chat_key(self, chat_id=None) -> str:
        return str(chat_id) if chat_id is not None else "__unknown_chat__"

    @staticmethod
    def _is_group_event(event: Any) -> bool:
        return bool(getattr(event, "is_group", False))

    def _group_user_key(self, event: Any, chat_key: str) -> tuple[str, str] | None:
        if not self._is_group_event(event):
            return None
        sender_id = getattr(event, "sender_id", None)
        return (chat_key, str(sender_id)) if sender_id is not None else None

    def _chat_rate(self, chat_kind: str | None = None) -> float:
        return self.group_chat_rate if chat_kind == "group" else self.private_chat_rate

    def _queue_item_sleep(self) -> float:
        jitter = random.uniform(-self.queue_item_jitter, self.queue_item_jitter)
        return max(0.0, self.queue_item_delay + jitter)

    def update_flood_until(self, seconds: int, chat_id=None) -> None:
        """Extend a chat flood-wait cooldown, or the global one when unknown."""
        new_until = time.monotonic() + seconds
        if chat_id is None:
            self._global_flood_until = max(self._global_flood_until, new_until)
        else:
            chat_key = self._chat_key(chat_id)
            self._chat_flood_until[chat_key] = max(
                self._chat_flood_until.get(chat_key, 0.0), new_until
            )
        self._wake_event.set()

    def flood_remaining(self, chat_id=None) -> int:
        """Seconds remaining in the applicable flood cooldown (0 if none)."""
        return max(0, math.ceil(self._flood_remaining_seconds(chat_id)))

    def _flood_remaining_seconds(self, chat_id=None) -> float:
        now = time.monotonic()
        global_rem = self._global_flood_until - now
        chat_rem = self._chat_flood_until.get(self._chat_key(chat_id), 0.0) - now
        return max(0.0, global_rem, chat_rem)

    async def _wait_for_flood_window(self, chat_id=None) -> None:
        rem = self._flood_remaining_seconds(chat_id)
        if rem > 0:
            log.info("[Flood limiter] Sleeping %ss for chat=%s", rem, chat_id or "global")
            await asyncio.sleep(rem)

    def send_wait_info(self, chat_id=None, chat_kind: str | None = None, include_chat_rate: bool = True):
        now = time.monotonic()
        global_wait = max(0.0, self._global_next_send_at - now)
        scope, wait, rate = "global", global_wait, self.global_send_rate
        chat_key = self._chat_key(chat_id)
        if include_chat_rate and chat_id is not None:
            chat_wait = max(0.0, self._chat_next_send_at.get(chat_key, 0.0) - now)
            if chat_wait >= wait:
                scope = "group" if chat_kind == "group" else "chat"
                wait, rate = chat_wait, self._chat_rate(chat_kind)
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
        if include_chat_rate and chat_id is not None:
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
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker())
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._queue_monitor())

    def _enqueue(self, item: QueueItem, *, force: bool = False) -> str | None:
        queue = self._chat_queues[item.chat_key]
        if not force:
            if self.per_chat_pending_limit and len(queue) >= self.per_chat_pending_limit:
                return "chat"
            if self.global_pending_limit and self._pending_total >= self.global_pending_limit:
                return "global"
            if (
                item.group_user_key is not None
                and self.per_user_group_pending_limit
                and self._group_user_pending[item.group_user_key] >= self.per_user_group_pending_limit
            ):
                return "user"

        queue.append(item)
        self._pending_total += 1
        if item.group_user_key is not None:
            self._group_user_pending[item.group_user_key] += 1
        if item.chat_key not in self._ready_chat_set:
            self._ready_chats.append(item.chat_key)
            self._ready_chat_set.add(item.chat_key)
        self._wake_event.set()
        return None

    def _pop_pending(self, chat_key: str) -> QueueItem:
        item = self._chat_queues[chat_key].popleft()
        self._pending_total -= 1
        if item.group_user_key is not None:
            self._group_user_pending[item.group_user_key] -= 1
            if self._group_user_pending[item.group_user_key] <= 0:
                self._group_user_pending.pop(item.group_user_key, None)
        return item

    async def _notify_overflow(self, event: Any, scope: str) -> None:
        try:
            await event.respond(
                "⚠️ Queue is full. This link will not get processed. "
                "Try again after some time."
            )
        except FloodWaitError as exc:
            self.update_flood_until(exc.seconds, getattr(event, "chat_id", None))
        except Exception:
            log.debug("Could not send queue-overflow notice", exc_info=True)

    async def _notify_expired(self, event: Any) -> None:
        try:
            await event.respond(
                "⌛ Your request waited too long in the queue and has expired. Please submit it again."
            )
        except FloodWaitError as exc:
            self.update_flood_until(exc.seconds, getattr(event, "chat_id", None))
        except Exception:
            log.debug("Could not send queue-expiry notice", exc_info=True)

    async def submit(self, process_callable, event, url: str, *args) -> bool:
        """Queue a request. Returns False only after notifying a rejected caller."""
        await self._ensure_queue_worker()
        chat_id = getattr(event, "chat_id", None)
        item = QueueItem(
            process_callable=process_callable,
            event=event,
            url=url,
            args=args,
            chat_key=self._chat_key(chat_id),
            group_user_key=self._group_user_key(event, self._chat_key(chat_id)),
            enqueued_at=time.monotonic(),
        )
        overflow_scope = self._enqueue(item)
        if overflow_scope is not None:
            await self._notify_overflow(event, overflow_scope)
            return False
        return True

    async def put(self, process_callable, event, url: str, *args) -> bool:
        """Backward-compatible alias for submit()."""
        return await self.submit(process_callable, event, url, *args)

    def _expire_head_items(self, chat_key: str) -> bool:
        if self.max_queue_wait <= 0:
            return False
        queue = self._chat_queues[chat_key]
        expired_any = False
        now = time.monotonic()
        while queue and now - queue[0].enqueued_at >= self.max_queue_wait:
            item = self._pop_pending(chat_key)
            asyncio.create_task(self._notify_expired(item.event))
            log.info("[Queue] Expired request chat=%s url=%s", chat_key, item.url)
            expired_any = True
        return expired_any

    async def _dispatch_ready(self) -> float | None:
        """Start eligible requests round-robin; return the earliest required wake-up."""
        next_wake_delay: float | None = None
        # Queue expiry must be checked even when all processing slots are busy.
        # Otherwise a long-running active job could keep expired jobs in memory.
        if self.max_queue_wait > 0:
            for chat_key in tuple(self._ready_chats):
                queue = self._chat_queues.get(chat_key)
                if not queue:
                    continue
                self._expire_head_items(chat_key)
                if queue:
                    expiry_delay = max(
                        0.0,
                        self.max_queue_wait - (time.monotonic() - queue[0].enqueued_at),
                    )
                    next_wake_delay = (
                        expiry_delay
                        if next_wake_delay is None
                        else min(next_wake_delay, expiry_delay)
                    )

        while self._active_total < self.concurrency_limit and self._ready_chats:
            round_size = len(self._ready_chats)
            made_progress = False
            for _ in range(round_size):
                if self._active_total >= self.concurrency_limit:
                    break
                chat_key = self._ready_chats.popleft()
                queue = self._chat_queues.get(chat_key)
                if not queue:
                    self._ready_chat_set.discard(chat_key)
                    self._chat_queues.pop(chat_key, None)
                    continue

                made_progress |= self._expire_head_items(chat_key)
                if not queue:
                    self._ready_chat_set.discard(chat_key)
                    self._chat_queues.pop(chat_key, None)
                    continue

                chat_id = getattr(queue[0].event, "chat_id", None)
                if self.max_queue_wait > 0:
                    expiry_delay = max(
                        0.0,
                        self.max_queue_wait - (time.monotonic() - queue[0].enqueued_at),
                    )
                    next_wake_delay = (
                        expiry_delay
                        if next_wake_delay is None
                        else min(next_wake_delay, expiry_delay)
                    )

                flood_delay = self._flood_remaining_seconds(chat_id)
                if flood_delay > 0 or self._chat_active[chat_key] >= self.per_chat_concurrency:
                    if flood_delay > 0:
                        next_wake_delay = (
                            flood_delay
                            if next_wake_delay is None
                            else min(next_wake_delay, flood_delay)
                        )
                    self._ready_chats.append(chat_key)
                    continue

                # Capacity is checked above, so these rolling semaphore acquires
                # do not wait. They remain the authoritative concurrency guard.
                await self.semaphore.acquire()
                chat_semaphore = self._chat_semaphores.setdefault(
                    chat_key, asyncio.Semaphore(self.per_chat_concurrency)
                )
                await chat_semaphore.acquire()
                item = self._pop_pending(chat_key)
                self._active_total += 1
                self._chat_active[chat_key] += 1
                if queue:
                    self._ready_chats.append(chat_key)
                else:
                    self._ready_chat_set.discard(chat_key)
                    self._chat_queues.pop(chat_key, None)
                asyncio.create_task(self._run_item(item, chat_semaphore))
                made_progress = True

            if not made_progress:
                break
        return next_wake_delay

    async def _queue_worker(self) -> None:
        log.info("[Queue] Fair request scheduler started.")
        while True:
            await self._wake_event.wait()
            self._wake_event.clear()
            wake_delay = await self._dispatch_ready()
            try:
                if wake_delay is None:
                    await self._wake_event.wait()
                else:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=wake_delay)
            except asyncio.TimeoutError:
                self._wake_event.set()

    async def _run_item(self, item: QueueItem, chat_semaphore: asyncio.Semaphore) -> None:
        try:
            if item.retries:
                # A requeued flood-wait request does not hold a slot while waiting.
                await asyncio.sleep(self._queue_item_sleep())
            await item.process_callable(item.event, item.url, *item.args)
        except FloodWaitError as exc:
            self.update_flood_until(exc.seconds, getattr(item.event, "chat_id", None))
            item.retries += 1
            # This is a new queue wait, not time spent in the completed part of
            # the pipeline. It must receive a fresh expiry window.
            item.enqueued_at = time.monotonic()
            # Preserve an accepted request even if other callers filled the pending
            # limit while this task was running; active work is still globally bounded.
            self._enqueue(item, force=True)
            log.warning(
                "[Queue] FloodWait %ss; requeued chat=%s url=%s",
                exc.seconds,
                item.chat_key,
                item.url,
            )
        except Exception:
            log.exception("[Queue] Error processing url=%s", item.url)
            try:
                await item.event.respond(f"❌ Failed to process `{item.url}`.")
            except Exception:
                pass
        finally:
            self._active_total -= 1
            self._chat_active[item.chat_key] -= 1
            if self._chat_active[item.chat_key] <= 0:
                self._chat_active.pop(item.chat_key, None)
            chat_semaphore.release()
            self.semaphore.release()
            self._wake_event.set()

    async def _queue_monitor(self) -> None:
        while True:
            if self._pending_total:
                log.info(
                    "[Queue Monitor] pending=%s active=%s chats=%s",
                    self._pending_total,
                    self._active_total,
                    len(self._chat_queues),
                )
            await asyncio.sleep(3)

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
        """Run a Telegram API call under flood and send-rate controls."""
        for attempt in range(1, max_retries + 1):
            try:
                await self._wait_for_flood_window(chat_id)
                await self._wait_for_send_slot(chat_id, chat_kind, include_chat_rate)
                return await coro_func(*args, **kwargs)
            except FloodWaitError as exc:
                self.update_flood_until(exc.seconds, chat_id)
                log.warning(
                    "[FloodWaitError] wait %ss for chat=%s (attempt %s/%s)",
                    exc.seconds,
                    chat_id or "global",
                    attempt,
                    max_retries,
                )
                if attempt == max_retries:
                    raise
                await asyncio.sleep(exc.seconds)
        return await coro_func(*args, **kwargs)
