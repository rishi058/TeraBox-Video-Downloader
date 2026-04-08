from telethon import events
import logging
from ..bot import bot, active_tasks

log = logging.getLogger(__name__)

@bot.on(events.CallbackQuery(data=b"cancel_download"))
async def handle_cancel(event):
    chat_id = event.chat_id
    sender_id = event.sender_id
    log.info(
        f"Received cancel_download: chat_id={chat_id}, sender_id={sender_id}, "
        f"active_tasks keys={list(active_tasks.keys())}"
    )

    # Match on chat_id OR sender_id — Telethon CallbackQuery.chat_id
    # can differ from the original message's event.chat_id in some contexts.
    tasks = {
        k: v for k, v in active_tasks.items()
        if k[0] == chat_id or k[0] == sender_id
    }
    cancelled = [v for v in tasks.values() if not v.is_set()]
    for ev in cancelled:
        ev.set()
    if cancelled:
        await event.answer(f"🚫 Cancelling {len(cancelled)} task(s)...")
    else:
        await event.answer("Nothing to cancel.")