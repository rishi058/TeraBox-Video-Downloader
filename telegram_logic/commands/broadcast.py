import os
import asyncio
import logging
from telethon import events
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
)
from ..bot import bot
from ..database import get_all_users

log = logging.getLogger(__name__)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))


async def _send_to(chat_id: int, reply_msg=None, text: str = None):
    """
    Send a message to a single chat.
    - If reply_msg has media → copy the media (+ caption) with send_file.
    - If reply_msg is text-only (possibly multiline) → send_message with full text.
    - If no reply_msg → send plain text.
    """
    if reply_msg is not None:
        media = reply_msg.media

        # Ignore web-page previews — treat them as plain text
        if isinstance(media, MessageMediaWebPage):
            media = None

        if media is not None:
            # Photo, video, audio, document, sticker, voice, etc.
            caption = reply_msg.message or ""
            await bot.send_file(
                chat_id,
                file=media,
                caption=caption,
                parse_mode="md",
            )
        else:
            # Plain text (potentially multiline)
            await bot.send_message(chat_id, reply_msg.message, parse_mode="md")
    else:
        await bot.send_message(chat_id, text, parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/broadcast(?:\s+([\s\S]+))?$"))
async def cmd_broadcast(event):
    log.info(f"Received /broadcast command from chat {event.chat_id}")

    # Only visible to admin
    if not ADMIN_ID or (event.sender_id != ADMIN_ID and event.chat_id != ADMIN_ID):
        return

    # Inline text (single or multiline via escaped newlines in some clients)
    inline_text = (event.pattern_match.group(1) or "").strip()
    reply_msg = await event.get_reply_message()

    if not inline_text and not reply_msg:
        await event.respond(
            "**Usage:**\n"
            "• `/broadcast <message>` — broadcast plain text (single line)\n"
            "• Reply to any message with `/broadcast` — broadcast text, photo, "
            "video, audio, document, sticker, or any other media to all users"
        )
        return

    users = get_all_users()
    if not users:
        await event.respond("No users found to broadcast.")
        return

    status = await event.respond(f"📡 Starting broadcast to **{len(users)}** users/groups…")

    success_count = 0
    fail_count = 0

    for chat_id_str in users:
        try:
            chat_id = int(chat_id_str)
            await _send_to(
                chat_id,
                reply_msg=reply_msg if reply_msg else None,
                text=inline_text if not reply_msg else None,
            )
            success_count += 1
            await asyncio.sleep(0.3)  # Rate-limit protection

        except FloodWaitError as e:
            log.warning(f"FloodWait during broadcast, sleeping {e.seconds}s")
            await asyncio.sleep(e.seconds)
            # Retry once after the flood wait
            try:
                await _send_to(
                    chat_id,
                    reply_msg=reply_msg if reply_msg else None,
                    text=inline_text if not reply_msg else None,
                )
                success_count += 1
            except Exception:
                fail_count += 1

        except Exception as e:
            log.warning(f"Failed to broadcast to {chat_id_str}: {e}")
            fail_count += 1

    await status.edit(
        f"✅ Broadcast complete!\n"
        f"• Delivered: **{success_count}**\n"
        f"• Failed: **{fail_count}**"
    )
    raise events.StopPropagation
