from telethon import events
from ..bot import bot

@bot.on(events.NewMessage(pattern="/info"))
async def cmd_info(event):
    sender = await event.get_sender()
    chat = await event.get_chat()

    user_id = sender.id if sender else "N/A"
    username = f"@{sender.username}" if (sender and sender.username) else "none"
    first_name = getattr(sender, "first_name", "") or ""
    last_name = getattr(sender, "last_name", "") or ""
    full_name = (first_name + (" " + last_name if last_name else "")).strip() or "N/A"

    chat_id = chat.id if chat else "N/A"
    chat_title = getattr(chat, "title", None)
    chat_username = getattr(chat, "username", None)

    if chat_title:
        chat_type = "Channel" if getattr(chat, "broadcast", False) else "Group/Supergroup"
        chat_info = (
            f"🏠 **Chat:** {chat_title}\n"
            f"🆔 **Chat ID:** `{chat_id}`\n"
            f"🔗 **Chat username:** {'@' + chat_username if chat_username else 'none'}\n"
            f"📂 **Type:** {chat_type}"
        )
    else:
        chat_info = (
            f"🏠 **Chat:** Private\n"
            f"🆔 **Chat ID:** `{chat_id}`"
        )

    msg_id = event.message.id

    text = (
        "ℹ️ **Info**\n\n"
        "👤 **User**\n"
        f"• ID: `{user_id}`\n"
        f"• Name: {full_name}\n"
        f"• Username: {username}\n\n"
        "💬 **Chat**\n"
        + chat_info + "\n\n"
        f"✉️ **Message ID:** `{msg_id}`"
    )

    await event.respond(text)
    raise events.StopPropagation