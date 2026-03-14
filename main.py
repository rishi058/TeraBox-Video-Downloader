import os
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn
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
    try:
        await asyncio.gather(*[_process_terabox(event, surl) for surl in surls])
    except Exception as e:
        log.error(f"Unhandled error in handle_message: {e}")


# — Telegram bot runner ——————————————————————————————————————————————————————————————————————

async def run_bot() -> None:
    if not BOT_TOKEN or not APP_ID or not API_HASH:
        log.error("ERROR: Set BOT_TOKEN, APP_ID, and API_HASH in your .env file!")
        return

    if not STORAGE_GROUP_ID:
        log.warning("STORAGE_GROUP_ID not set — caching disabled, videos will be sent directly.")

    await bot.start(bot_token=BOT_TOKEN)

    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="get", description="Download a TeraBox video"),
            BotCommand(command="random", description="Get a random video"),
            BotCommand(command="info", description="Show chat and user info"),
        ],
    ))
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
