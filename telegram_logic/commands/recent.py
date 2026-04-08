import os
import logging
import time
from datetime import datetime, timezone, timedelta
from telethon import events
from ..bot import bot
from ..database import get_all_users
from ..helpers import format_duration

log = logging.getLogger(__name__)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

@bot.on(events.NewMessage(pattern=r"^/recent$"))
async def cmd_recent(event):
    log.info(f"Received /recent command from chat {event.chat_id}")
    # Only visible to admin
    if not ADMIN_ID or (event.sender_id != ADMIN_ID and event.chat_id != ADMIN_ID):
        return

    status = await event.respond("📊 Fetching recent users...")
    
    users_data = get_all_users()
    
    if not users_data:
        await status.edit("No user data found.")
        return

    # Convert the dict to a list of tuples to sort
    # item: (chat_id_str, { "username": ..., "last_active": ... })
    user_list = list(users_data.items())
    
    # Sort descending based on last_active
    user_list.sort(key=lambda item: item[1].get("last_active", 0.0), reverse=True)
    
    # Take top 7
    top_7 = user_list[:7]
    
    now = time.time()
    
    msg_lines = ["**🏆 Top 7 Recent Users**\n"]
    
    for idx, (chat_id, info) in enumerate(top_7, start=1):
        username = info.get("username", "Unknown")
        last_active = info.get("last_active", 0.0)
        
        # Calculate how long ago
        if last_active > 0:
            ago_seconds = now - last_active
            time_ago_str = format_duration(ago_seconds) + " ago"
            IST = timezone(timedelta(hours=5, minutes=30))
            date_str = datetime.fromtimestamp(last_active, tz=timezone.utc).astimezone(IST).strftime("%d-%m-%y %H:%M")
            time_ago_str = f"{date_str} ({time_ago_str})"
        else:
            time_ago_str = "Never"
            
        username_str = f"@{username}" if username and username not in ("None", "") else "No Username"
            
        msg_lines.append(f"{idx}. `{chat_id}` | {username_str}")
        msg_lines.append(f"   └ ⏱️ Last active: **{time_ago_str}**\n")
        
    await status.edit("\n".join(msg_lines))
    raise events.StopPropagation
