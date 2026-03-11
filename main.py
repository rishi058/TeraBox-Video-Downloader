import os
import asyncio
import logging
from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telegram_logic.bot import bot, _process_terabox
from telegram_logic.helpers import extract_all_surls
import telegram_logic.commands  # registers all @bot.on(...) handlers  # noqa: F401

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_ID = int(os.environ.get("APP_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
STORAGE_GROUP_ID = int(os.environ.get("STORAGE_GROUP_ID", "0"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO,
)
log = logging.getLogger(__name__)

# — Basic Message Handler ————————————————————————————————————————————————————————————————————

@bot.on(events.NewMessage)
async def handle_message(event):
    text = event.raw_text or ""
    surls = extract_all_surls(text)
    if not surls:
        return  # silently ignore non-TeraBox messages
    await asyncio.gather(*[_process_terabox(event, surl) for surl in surls])


# — Entry point ————————————————————————————————————————————————————————————————————

async def main() -> None:
    if not BOT_TOKEN or not APP_ID or not API_HASH:
        print("ERROR: Set BOT_TOKEN, APP_ID, and API_HASH in your .env file!")
        return

    if not STORAGE_GROUP_ID:
        log.warning("STORAGE_GROUP_ID not set — caching disabled, videos will be sent directly.")

    await bot.start(bot_token=BOT_TOKEN)

    # Pre-cache entities so Telethon can resolve STORAGE_GROUP_ID and other
    # peers on fresh / ephemeral deployments (e.g. Render) where the session
    # file doesn't persist between redeploys.
    if STORAGE_GROUP_ID:
        try:
            await bot.get_dialogs(limit=200)
            log.info("Dialogs cached — storage entity should now be resolvable.")
        except Exception as e:
            log.warning(f"Could not pre-cache dialogs: {e}")

    # If SESSION_STRING is not set, print the current session string so it can
    # be saved as an environment variable on the deployment platform.
    if not os.environ.get("SESSION_STRING"):
        from telethon.sessions import StringSession as _SS
        if isinstance(bot.session, _SS):
            log.info(
                "Set this as SESSION_STRING on your deployment platform to persist "
                "the entity cache across redeploys:\n%s",
                bot.session.save(),
            )

    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="get", description="Download a TeraBox video"),
            BotCommand(command="random", description="Get a random cached video"),
            BotCommand(command="info", description="Show chat and user info"),
        ],
    ))
    log.info("Bot commands registered.")
    log.info("Bot started! Waiting for messages... (Ctrl+C to stop)")

    try:
        await bot.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    finally:
        log.info("Shutting down...")
        if bot.is_connected():
            await bot.disconnect()
        log.info("Bye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
