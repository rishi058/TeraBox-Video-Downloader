import asyncio
from telethon import events
import logging
from ..bot import bot
from ..terabox_trad import process_terabox
from ..helpers import extract_all_surls

log = logging.getLogger(__name__)

@bot.on(events.NewMessage(pattern=r"^/get(?:@\S+)?(?:\s+(.+))?$"))
async def cmd_get(event):
    log.info(f"Received /get command from chat {event.chat_id}")

    arg = (event.pattern_match.group(1) or "").strip()

    surls = extract_all_surls(arg) if arg else []

    if not surls:
        await event.respond(
            "Usage: `/get <TeraBox URL>`\n\nExample:\n`/get https://1024tera.com/s/1XXXX`"
        )
        raise events.StopPropagation

    await asyncio.gather(*[process_terabox(event, surl) for surl in surls])
    
    raise events.StopPropagation