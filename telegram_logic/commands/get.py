import asyncio
from telethon import events
from ..bot import bot, _process_terabox
from ..helpers import extract_all_surls


@bot.on(events.NewMessage(pattern=r"^/get(?:@\S+)?(?:\s+(.+))?$"))
async def cmd_get(event):
    arg = (event.pattern_match.group(1) or "").strip()
    surls = extract_all_surls(arg) if arg else []
    if not surls:
        await event.respond(
            "Usage: `/get <TeraBox URL>`\n\nExample:\n`/get https://1024tera.com/s/1XXXX`"
        )
        raise events.StopPropagation
    await asyncio.gather(*[_process_terabox(event, surl) for surl in surls])
    raise events.StopPropagation