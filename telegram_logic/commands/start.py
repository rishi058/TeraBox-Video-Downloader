from ..bot import bot
from telethon import events

@bot.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    await event.respond(
        "Send me a TeraBox link and I'll send you the video!\n\n"
        "Supported formats:\n"
        "• https://1024tera.com/s/1XXXX\n"
        "• https://www.1024tera.com/sharing/link?surl=XXXX\n"
        "• https://teraboxapp.com/s/1XXXX"
    )
    raise events.StopPropagation