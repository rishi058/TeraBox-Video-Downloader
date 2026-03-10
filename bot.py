import os
import re
import logging
from telegram import Update  
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters,
)
from terabox import process_terabox_link, TeraBoxError

from dotenv import load_dotenv
load_dotenv()


# ─── Configuration ────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
MAX_TG_SIZE = 50 * 1024 * 1024  # Telegram 50 MB limit for bot uploads

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO,
)
log = logging.getLogger(__name__)

# Regex to match TeraBox share URLs and extract the SURL
# Handles: /s/1XXXX, /sharing/link?surl=XXXX, /wap/share/filelist?surl=XXXX
TERA_URL_RE = re.compile(
    r"https?://(?:www\.|dm\.|dl\.)?(?:1024tera|1024terabox|terabox|teraboxapp|4funbox)\.com"
    r"(?:/s/1(?P<surl_path>[A-Za-z0-9_-]+)"
    r"|/(?:sharing/link|wap/share/filelist)\?[^#]*surl=(?P<surl_param>[A-Za-z0-9_-]+))",
    re.IGNORECASE,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_surl(text: str) -> str | None:
    """Extract SURL from a TeraBox URL in the message text."""
    m = TERA_URL_RE.search(text)
    if m:
        return m.group("surl_path") or m.group("surl_param")
    return None


# ─── Telegram handlers ────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a TeraBox link and I'll send you the video!\n\n"
        "Supported formats:\n"
        "• https://1024tera.com/s/1XXXX\n"
        "• https://www.1024tera.com/sharing/link?surl=XXXX\n"
        "• https://teraboxapp.com/s/1XXXX"
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    surl = extract_surl(text)

    if not surl:
        await update.message.reply_text("Please send a valid TeraBox link.")
        return

    status = await update.message.reply_text(f"⏳ Processing link (surl: {surl})...")

    try:
        result = process_terabox_link(surl)
    except TeraBoxError as e:
        log.error(f"TeraBox error for surl={surl}: {e}")
        await status.edit_text(f"❌ Error: {e}")
        return
    except Exception as e:
        log.exception(f"Unexpected error processing surl={surl}")
        await status.edit_text(f"❌ Unexpected error: {e}")
        return

    filepath = result["filepath"]
    filesize = os.path.getsize(filepath)

    if filesize > MAX_TG_SIZE:
        await status.edit_text(
            f"❌ File too large for Telegram ({filesize / 1024 / 1024:.1f} MB > 50 MB limit)."
        )
        return

    await status.edit_text("📤 Uploading video...")

    try:
        with open(filepath, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                filename=result["filename"],
                caption=result["filename"],
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
                pool_timeout=60,
            )
        await status.delete()
    except Exception as e:
        log.error(f"Upload error for surl={surl}: {e}")
        await status.edit_text(f"❌ Upload failed: {e}")
    # Files are kept in videos/ for caching — no cleanup


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        print("ERROR: Set your bot token!")
        print("Set BOT_TOKEN environment variable")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started! Waiting for messages...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
