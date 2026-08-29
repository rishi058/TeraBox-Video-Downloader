import asyncio
import logging
from telethon import events
from ..bot import bot
from ..flezen import process_flezen
from ..helpers import extract_all_terabox_url_exp
from flezen.public_api import extract_all_flezen_urls
from diskwalaDL.public_api import extract_all_diskwala_urls

log = logging.getLogger(__name__)


@bot.on(events.NewMessage(pattern=r"(?i)^/fz(?:@\S+)?(?:\s+([\s\S]+))?$"))
async def cmd_fz(event):
    log.info(f"Received /fz command from chat {event.chat_id}")

    arg = (event.pattern_match.group(1) or "").strip()

    flezen_url_list = extract_all_flezen_urls(arg) if arg else []

    if not flezen_url_list:
        if extract_all_terabox_url_exp(arg):
            await event.respond(
                "🔗 That looks like a **TeraBox** link. Use **/exp**, **/exphd** or **/get**:\n"
                "`/exp <link>`\n\n"
                "…or switch your default mode from /settings."
            )
        elif extract_all_diskwala_urls(arg):
            await event.respond(
                "🔗 That looks like a **Diskwala** link. Use **/dw**:\n"
                "`/dw <link>`\n\n"
                "…or switch your default mode from /settings."
            )
        else:
            await event.respond(
                "Usage: `/fz <Flezen URL>`\n\n"
                "Example:\n`/fz https://flezen.com/s/da1895hbjlnspd09usgyrtxdsjxuis`"
            )
        raise events.StopPropagation

    await asyncio.gather(*[process_flezen(event, url) for url in flezen_url_list])

    raise events.StopPropagation
