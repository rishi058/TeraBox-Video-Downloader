import os
import asyncio
import random
import logging
from telethon import events
from ..bot import bot
from ..caching import get_cache
from ..helpers import format_size

from dotenv import load_dotenv
load_dotenv()
STORAGE_GROUP_ID = int(os.getenv("STORAGE_GROUP_ID", "0"))

log = logging.getLogger(__name__)

@bot.on(events.NewMessage(pattern="/random"))
async def cmd_random(event):
    data = await asyncio.to_thread(get_cache)
    if not data:
        await event.respond("📭 No videos yet. Send a TeraBox link first!")
        raise events.StopPropagation

    surl, msg_id = random.choice(list(data.items()))
    cached_msg = None
    if STORAGE_GROUP_ID:
        try:
            cached_msg = await bot.get_messages(STORAGE_GROUP_ID, ids=msg_id)
            if cached_msg and not (cached_msg.video or (
                cached_msg.document
                and cached_msg.document.mime_type
                and "video" in cached_msg.document.mime_type
            )):
                cached_msg = None
        except Exception as e:
            log.warning(f"Random fetch failed for surl={surl} msg_id={msg_id}: {e}")
            cached_msg = None

    if cached_msg is None:
        await event.respond("⚠️ Could not retrieve random video. Try again!")
        raise events.StopPropagation

    f = cached_msg.file
    fname = (f.name if f and f.name else surl)
    caption = f"📦 `{fname}`"
    await bot.send_file(
        event.chat_id, cached_msg.media,
        caption=caption, supports_streaming=True, reply_to=event.message.id,
    )
    raise events.StopPropagation

