import asyncio
import logging
from telethon import events
from ..bot import bot
from ..helpers import TERA_URL_RE
from ..terabox_exp import process_terabox_experimental

log = logging.getLogger(__name__)

def extract_all_terabox_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for m in TERA_URL_RE.finditer(text):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls

@bot.on(events.NewMessage(pattern=r"^/exp(?:@\S+)?(?:\s+(.+))?$"))
async def cmd_get_exp(event):
    log.info(f"Received /exp command from chat {event.chat_id}")

    arg = (event.pattern_match.group(1) or "").strip()

    terabox_url_list = extract_all_terabox_urls(arg) if arg else []

    if not terabox_url_list:
        await event.respond(
            "Usage: `/exp <TeraBox URL>`\n\nExample:\n`/exp https://1024tera.com/s/1XXXX`"
        )
        raise events.StopPropagation

    await asyncio.gather(*[process_terabox_experimental(event, terabox_url) for terabox_url in terabox_url_list])
    
    raise events.StopPropagation


@bot.on(events.NewMessage(pattern=r"(?i)^/exphd(?:@\S+)?(?:\s+(.+))?$"))
async def cmd_get_exp_hd(event):
    arg = (event.pattern_match.group(1) or "").strip()

    terabox_url_list = extract_all_terabox_urls(arg) if arg else []

    if not terabox_url_list:
        await event.respond(
            "Usage: `/exphd <TeraBox URL>`\n\nExample:\n`/exphd https://1024tera.com/s/1XXXX`"
        )
        raise events.StopPropagation

    await asyncio.gather(*[process_terabox_experimental(event, terabox_url, is_hd=True) for terabox_url in terabox_url_list])

    raise events.StopPropagation