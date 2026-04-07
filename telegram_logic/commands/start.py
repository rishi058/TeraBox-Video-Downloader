from ..bot import bot
from telethon import events
import logging

log = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "🚀 **Welcome!**\n\n"
    "⚙️ **Commands:**\n"
    "**/get** <link>  Default download\n"
    "**/exp** <link>  Backup method\n"
    "**/expHD** <link>  HD quality\n\n"
    "🎲 **/random**  Get a random video\n"
    "🔧 **/settings**  Change default mode\n\n"
    "📥 Give me **TeraBox link(s)** (paste or forward them), I'll send the videos.\n\n"
    "💡 You can also just send a link without any command, I'll use your default setting."
)

@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    log.info(f"Received /start command from chat {event.chat_id}")
    await event.respond(WELCOME_MESSAGE)
    raise events.StopPropagation