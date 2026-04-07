from telethon import events
import logging
from ..bot import bot, active_tasks

log = logging.getLogger(__name__)

@bot.on(events.CallbackQuery(data=b"cancel_download"))
async def handle_cancel(event):
    log.info(f"Received /cancel_download command from chat {event.chat_id}")

    chat_id = event.chat_id
    tasks = {k: v for k, v in active_tasks.items() if k[0] == chat_id}
    cancelled = [v for v in tasks.values() if not v.is_set()]
    for ev in cancelled:
        ev.set()
    if cancelled:
        await event.answer(f"🚫 Cancelling {len(cancelled)} download(s)...")
    else:
        await event.answer("Nothing to cancel.")