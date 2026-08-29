from ..bot import bot
from telethon import events
import logging

log = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "🚀 **Welcome!**\n\n"
    "⚙️ **Commands:**\n"
    "**/exp** <link>  Reliable & Fast [Recommended]\n"
    "**/expHD** <link>  For HD Videos [Slow]\n"
    "**/get** <link>  Unstable [Use for small files]\n"
    "**/dw** <link>  Download Diskwala video\n"
    "**/fz** <link>  Download Flezen video\n"
    "**/all** <link>  Auto-detect link type\n\n"
    "🎲 **/random**  Get a random video\n"
    "🔧 **/settings**  Change default mode [/exp is default]\n\n"
    "📥 Give me **TeraBox**, **Diskwala**, or **Flezen** link(s) (paste or forward them), I'll send the videos.\n\n"
    "💡 You can also just send a link without any command, I'll use your default setting.\n\n"
    "📩 Send feedback to admin using **/op** <your message>"
)

@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    log.info(f"Received /start command from chat {event.chat_id}")
    await event.respond(WELCOME_MESSAGE)
    raise events.StopPropagation