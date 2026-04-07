import os
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn
from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault, BotCommandScopePeer
from telegram_logic.bot import bot
from telegram_logic.terabox_trad import process_terabox
from telegram_logic.terabox_exp import process_terabox_experimental 
from telegram_logic.helpers import extract_all_surls, extract_all_terabox_url
from telegram_logic.database import track_user, get_user_mode

# — Global User Tracker ——————————————————————————————————————————————————————————————————

@bot.on(events.NewMessage)
async def global_tracker(event):
    username = None

    if getattr(event.sender, 'username', None):
        username = event.sender.username
    elif getattr(event.chat, 'username', None):
        username = event.chat.username

    track_user(event.chat_id, username)
    # Does not raise StopPropagation, allowing other handlers to execute

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
    if text.startswith("/"):
        return  # Let command handlers deal with commands
    
    # Get mode based on user-id..
    mode = get_user_mode(event.chat_id)

    if mode == 'get':
        surls = extract_all_surls(text)
        if not surls:
            return  # silently ignore non-TeraBox messages
        try:
            log.info(f"Message redirected to [get] mode")
            await asyncio.gather(*[process_terabox(event, surl) for surl in surls])
        except Exception as e:
            log.error(f"Unhandled error in handle_message: {e}")

    if mode == 'exp':
        terabox_url_list = extract_all_terabox_url(text)
        if not terabox_url_list:
            return  # silently ignore non-TeraBox messages
        try:
            log.info(f"Message redirected to [exp] mode")
            await asyncio.gather(*[process_terabox_experimental(event, surl) for surl in terabox_url_list])
        except Exception as e:
            log.error(f"Unhandled error in handle_message: {e}")

    if mode == 'exphd':
        terabox_url_list = extract_all_terabox_url(text)
        if not terabox_url_list:
            return  # silently ignore non-TeraBox messages
        try:
            log.info(f"Message redirected to [exphd] mode")
            await asyncio.gather(*[process_terabox_experimental(event, surl, is_hd=True) for surl in terabox_url_list])
        except Exception as e:
            log.error(f"Unhandled error in handle_message: {e}")
    
    return
# — Telegram bot runner ——————————————————————————————————————————————————————————————————————

async def run_bot() -> None:
    if not BOT_TOKEN or not APP_ID or not API_HASH:
        log.error("ERROR: Set BOT_TOKEN, APP_ID, and API_HASH in your .env file!")
        return

    if not STORAGE_GROUP_ID:
        log.warning("STORAGE_GROUP_ID not set — caching disabled, videos will be sent directly.")

    await bot.start(bot_token=BOT_TOKEN)

    default_commands = [
        BotCommand(command="start", description="Start BOT"),
        BotCommand(command="get", description="Download TeraBox video"),
        BotCommand(command="random", description="Get a random video"), 
        BotCommand(command="settings", description="View Details"),
        BotCommand(command="exp", description="[Experimental] Download TeraBox video"), 
        BotCommand(command="exphd", description="[Experimental] Download HD TeraBox video"), 
    ]

    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=default_commands
    ))

    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if admin_id:
        try:
            admin_peer = await bot.get_input_entity(admin_id)
            admin_commands_list = default_commands + [
                BotCommand(command="recent", description="[Admin] Show recent users"),
                BotCommand(command="broadcast", description="[Admin] Broadcast message"),
            ]
            await bot(SetBotCommandsRequest(
                scope=BotCommandScopePeer(peer=admin_peer),
                lang_code="",
                commands=admin_commands_list
            ))
            log.info("Admin commands registered.")
        except Exception as e:
            log.error(f"Failed to set admin commands. You may need to send a message to the bot first. Error: {e}")

    log.info("Bot commands registered.")
    log.info("Bot started! Waiting for messages...")

    await bot.run_until_disconnected()


# — FastAPI app ———————————————————————————————————————————————————————————————————————————————

@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_task = asyncio.create_task(run_bot())
    yield
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    if bot.is_connected():
        await bot.disconnect()
    log.info("Bye!")

app = FastAPI(lifespan=lifespan)

@app.get("/ping")
async def ping():
    return "pong"

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
