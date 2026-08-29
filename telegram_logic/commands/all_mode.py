import logging
from telethon import events
from ..bot import bot
from ..auto_route import process_auto

log = logging.getLogger(__name__)


@bot.on(events.NewMessage(pattern=r"(?i)^/all(?:@\S+)?(?:\s+([\s\S]+))?$"))
async def cmd_all(event):
    log.info(f"Received /all command from chat {event.chat_id}")

    arg = (event.pattern_match.group(1) or "").strip()

    found = bool(arg) and await process_auto(event, arg)
    if not found:
        await event.respond(
            "Usage: `/all <TeraBox / Diskwala / Flezen URL>`\n\n"
            "Auto-detects the link type. TeraBox links use your TeraBox "
            "engine preference from /settings.\n\n"
            "Example:\n`/all https://terabox.com/s/1abc123`"
        )

    raise events.StopPropagation
