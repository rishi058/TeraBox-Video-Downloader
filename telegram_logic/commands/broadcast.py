import os
import asyncio
import logging
from telethon import events
from telethon.errors import FloodWaitError
from ..bot import bot
from ..database import get_all_users

log = logging.getLogger(__name__)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

@bot.on(events.NewMessage(pattern=r"^/broadcast(?: |$)(.*)"))
async def cmd_broadcast(event):
    log.info(f"Received /broadcast command from chat {event.chat_id}")

    # Only visible to admin
    if not ADMIN_ID or (event.sender_id != ADMIN_ID and event.chat_id != ADMIN_ID):
        # Ignore silently for non-admins
        return

    msg_text = event.pattern_match.group(1).strip()
    is_reply = False
    reply_msg = None
    
    if not msg_text:
        # Check if it's a reply
        reply_msg = await event.get_reply_message()
        if not reply_msg:
            await event.respond("Usage: `/broadcast <message>` or reply to a message with `/broadcast`.")
            return
        is_reply = True

    users = get_all_users()
    if not users:
        await event.respond("No users found to broadcast.")
        return

    status = await event.respond(f"Starting broadcast to {len(users)} users/groups...")
    
    success_count = 0
    fail_count = 0

    for chat_id_str in users:
        try:
            chat_id = int(chat_id_str)
            if is_reply:
                await bot.forward_messages(chat_id, reply_msg.id, event.chat_id)
            else:
                await bot.send_message(chat_id, msg_text)
            success_count += 1
            await asyncio.sleep(0.3)  # Rate limit protection to prevent FloodWait
        except FloodWaitError as e:
            log.warning(f"FloodWait during broadcast, sleeping {e.seconds}s")
            await asyncio.sleep(e.seconds)
            # Retry this user once after waiting
            try:
                if is_reply:
                    await bot.forward_messages(chat_id, reply_msg.id, event.chat_id)
                else:
                    await bot.send_message(chat_id, msg_text)
                success_count += 1
            except Exception:
                fail_count += 1
        except Exception as e:
            log.warning(f"Failed to broadcast to {chat_id_str}: {e}")
            fail_count += 1
            
    await status.edit(f"Broadcast complete!\n✅ Success: {success_count}\n❌ Failed: {fail_count}")
    raise events.StopPropagation
