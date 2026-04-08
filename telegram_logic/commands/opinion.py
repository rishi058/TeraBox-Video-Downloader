import os
import logging
from telethon import events
from ..bot import bot

log = logging.getLogger(__name__)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))


@bot.on(events.NewMessage(pattern=r"^/op(?:\s+([\s\S]+))?$"))
async def cmd_opinion(event):
    log.info(f"Received /op command from chat {event.chat_id}")

    message_text = (event.pattern_match.group(1) or "").strip()

    if not message_text:
        await event.respond(
            "**Usage:** `/op <your message>`\n"
            "Share your opinion, feedback, or info with the admin."
        )
        return

    if not ADMIN_ID:
        log.error("ADMIN_ID not set — cannot forward opinion.")
        await event.respond("⚠️ Admin not configured. Please try again later.")
        return

    sender = await event.get_sender()
    username = getattr(sender, "username", None)
    username_display = f"@{username}" if username else "N/A"

    forward_text = (
        f"📩 **Msg from a user**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **User:** {username_display}\n"
        f"🆔 **Chat ID:** `{event.chat_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 {message_text}"
    )

    try:
        await bot.send_message(ADMIN_ID, forward_text, parse_mode="md")
    except Exception as e:
        log.error(f"Failed to forward opinion to admin: {e}")
        await event.respond("⚠️ Something went wrong. Please try again later.")
        return

    await event.respond("✅ **Opinion submitted.** Thank you for your feedback!")
    raise events.StopPropagation
