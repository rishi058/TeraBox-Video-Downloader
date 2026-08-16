import os
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn
from telethon import events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeChats,
    BotCommandScopeDefault,
    BotCommandScopePeer,
    BotCommandScopeUsers,
)
from telegram_logic.bot import bot
from telegram_logic.terabox_trad import process_terabox
from telegram_logic.terabox_exp import process_terabox_experimental
from telegram_logic.diskwala import process_diskwala
from telegram_logic.helpers import extract_all_surls, extract_all_terabox_url_exp
from diskwalaDL.public_api import extract_all_diskwala_urls
from firebase_db.users import track_user, get_user_mode
from firebase_db.cache import (
    initialize_runtime_cache_async,
    runtime_cache_refresh_loop,
    shutdown_runtime_cache,
)

# — Global User Tracker ——————————————————————————————————————————————————————————————————

@bot.on(events.NewMessage)
async def global_tracker(event):
    username = None

    if getattr(event.sender, 'username', None):
        username = event.sender.username
    elif getattr(event.chat, 'username', None):
        username = event.chat.username

    try:
        track_user(event.chat_id, username)
    except Exception as e:
        log.error(f"[global_tracker] Unexpected error in track_user: {e}")
    # Does not raise StopPropagation, allowing other handlers to execute

import telegram_logic.commands  # registers all @bot.on(...) handlers  # noqa: F401

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_ID = int(os.environ.get("APP_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO,
)
log = logging.getLogger(__name__)

# — Wrong-source hints ————————————————————————————————————————————————————————————————————————
DISKWALA_IN_TERABOX_MODE = (
    "🔗 That looks like a **Diskwala** link, but your current mode downloads **TeraBox** videos.\n\n"
    "➡️ Use the **/dw** command:\n`/dw <link>`\n\n"
    "…or switch your default mode to **dw** from /settings."
)

TERABOX_IN_DISKWALA_MODE = (
    "🔗 That looks like a **TeraBox** link, but your current mode is **dw** (Diskwala).\n\n"
    "➡️ Use **/exp**, **/exphd** or **/get**:\n`/exp <link>`\n\n"
    "…or switch your default mode from /settings."
)

# — Basic Message Handler ————————————————————————————————————————————————————————————————————

@bot.on(events.NewMessage)
async def handle_message(event):
    text = event.raw_text or ""
    if text.startswith("/"):
        return  # Let command handlers deal with commands
    
    # Get mode based on user-id..
    try:
        mode = get_user_mode(event.chat_id)
    except Exception as e:
        log.error(f"[handle_message] DB error fetching user mode: {e}")
        await event.respond("⚠️ Database error. Please try again later.")
        return

    if mode == 'get':
        surls = extract_all_surls(text)
        if not surls:
            if extract_all_diskwala_urls(text):
                await event.respond(DISKWALA_IN_TERABOX_MODE)
            return  # silently ignore non-TeraBox messages
        try:
            log.info(f"Message redirected to [get] mode")
            await asyncio.gather(*[process_terabox(event, surl) for surl in surls])
        except Exception as e:
            log.error(f"Unhandled error in handle_message: {e}")

    if mode == 'exp':
        terabox_url_list = extract_all_terabox_url_exp(text)
        if not terabox_url_list:
            if extract_all_diskwala_urls(text):
                await event.respond(DISKWALA_IN_TERABOX_MODE)
            return  # silently ignore non-TeraBox messages
        try:
            log.info(f"Message redirected to [exp] mode")
            await asyncio.gather(*[process_terabox_experimental(event, surl) for surl in terabox_url_list])
        except Exception as e:
            log.error(f"Unhandled error in handle_message: {e}")

    if mode == 'exphd':
        terabox_url_list = extract_all_terabox_url_exp(text)
        if not terabox_url_list:
            if extract_all_diskwala_urls(text):
                await event.respond(DISKWALA_IN_TERABOX_MODE)
            return  # silently ignore non-TeraBox messages
        try:
            log.info(f"Message redirected to [exphd] mode")
            await asyncio.gather(*[process_terabox_experimental(event, surl, is_hd=True) for surl in terabox_url_list])
        except Exception as e:
            log.error(f"Unhandled error in handle_message: {e}")

    if mode == 'dw':
        diskwala_url_list = extract_all_diskwala_urls(text)
        if not diskwala_url_list:
            if extract_all_terabox_url_exp(text):
                await event.respond(TERABOX_IN_DISKWALA_MODE)
            return  # silently ignore non-Diskwala messages
        try:
            log.info(f"Message redirected to [dw] mode")
            await asyncio.gather(*[process_diskwala(event, url) for url in diskwala_url_list])
        except Exception as e:
            log.error(f"Unhandled error in handle_message: {e}")

    return
# — Telegram bot runner ——————————————————————————————————————————————————————————————————————

async def run_bot() -> None:
    if not BOT_TOKEN or not APP_ID or not API_HASH:
        log.error("ERROR: Set BOT_TOKEN, APP_ID, and API_HASH in your .env file!")
        return

    await bot.start(bot_token=BOT_TOKEN)

    default_commands = [ 
        BotCommand(command="start", description="Start BOT"),
        BotCommand(command="exp", description="[Experimental] Download TeraBox video"), 
        BotCommand(command="exphd", description="[Experimental] Download HD TeraBox video"), 
        BotCommand(command="get", description="Download TeraBox video [Unstable]"),
        BotCommand(command="dw", description="Download Diskwala video"),
        BotCommand(command="random", description="Get a random video"),
        BotCommand(command="settings", description="View Details"),
        BotCommand(command="op", description="Send feedback to admin"),
    ]

    # Explicit public scopes prevent a previously registered admin command list
    # from leaking through a broader Telegram scope.
    for scope in (
        BotCommandScopeDefault(),
        BotCommandScopeUsers(),
        BotCommandScopeChats(),
    ):
        await bot(SetBotCommandsRequest(
            scope=scope,
            lang_code="",
            commands=default_commands,
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

    async def cache_lifecycle():
        cache_ready = await initialize_runtime_cache_async()
        if not cache_ready:
            log.warning("Runtime media cache is empty; /random and cache hits are unavailable.")
        else:
            log.info("Runtime media cache is ready.")
        await runtime_cache_refresh_loop()

    cache_task = asyncio.create_task(cache_lifecycle())
    try:
        await bot.run_until_disconnected()
    finally:
        cache_task.cancel()
        try:
            await cache_task
        except asyncio.CancelledError:
            pass
        shutdown_runtime_cache()


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
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
