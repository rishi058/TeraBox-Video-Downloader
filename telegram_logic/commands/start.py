from ..bot import bot
from telethon import events
import logging

log = logging.getLogger(__name__)

@bot.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    log.info(f"Received /start command from chat {event.chat_id}")
    await event.respond(
        "Send me a TeraBox link and I'll send you the video!\n\n"
        "Supported formats:\n"
        "• https://1024tera.com/s/1XXXX\n"
        "• https://www.1024tera.com/sharing/link?surl=XXXX\n"
        "• https://teraboxapp.com/s/1XXXX"
    )
    raise events.StopPropagation