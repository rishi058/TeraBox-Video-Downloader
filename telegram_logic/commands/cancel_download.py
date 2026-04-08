from telethon import events
import logging
from ..bot import bot, active_tasks

log = logging.getLogger(__name__)

@bot.on(events.CallbackQuery(pattern=rb"^cancel:"))
async def handle_cancel(event):
    chat_id = event.chat_id
    sender_id = event.sender_id

    # Extract the surl from callback data: "cancel:<surl>"
    data = event.data.decode("utf-8", errors="ignore")
    surl = data.split(":", 1)[1] if ":" in data else None

    log.info(
        f"Received cancel: chat_id={chat_id}, sender_id={sender_id}, "
        f"surl={surl}, active_tasks keys={list(active_tasks.keys())}"
    )

    if not surl:
        await event.answer("⚠️ Invalid cancel request.")
        return

    # Look for the exact task matching (chat_id, surl) or (sender_id, surl)
    cancel_event = active_tasks.get((chat_id, surl)) or active_tasks.get((sender_id, surl))

    if cancel_event and not cancel_event.is_set():
        cancel_event.set()
        await event.answer("🚫 Cancelling this download...")
    else:
        await event.answer("Nothing to cancel.")