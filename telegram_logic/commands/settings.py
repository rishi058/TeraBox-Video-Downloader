from telethon import events, Button
from ..bot import bot
from ..database import set_user_mode, get_user_mode

AVAILABLE_MODES = ["get", "exp", "exphd"]

@bot.on(events.NewMessage(pattern="/settings"))
async def cmd_settings(event):
    sender = await event.get_sender()
    chat = await event.get_chat()

    user_id = sender.id if sender else "N/A"
    username = f"@{sender.username}" if (sender and sender.username) else "none"
   
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
            f"🆔 **Pvt Chat ID:** `{chat_id}`"
        )

    current_mode = get_user_mode(chat_id)

    available_modes = AVAILABLE_MODES.copy()
    if current_mode in available_modes:
        available_modes.remove(current_mode)  # will have 2 elements, make them button

    text = (
        f"• User ID: `{user_id}`\n"
        f"• Username: {username}\n" 
        f"• DL Mode: {current_mode}\n"
        + chat_info
    )

    buttons = [[Button.inline(f"🔗 Set Mode: {mode}", data=f"setmode_{mode}")] for mode in available_modes]

    await event.respond(text, buttons=buttons)
    raise events.StopPropagation

@bot.on(events.CallbackQuery(pattern=b"setmode_(.*)"))
async def cb_set_mode(event):
    mode = event.pattern_match.group(1).decode("utf-8")
    chat_id = event.chat_id
    
    set_user_mode(chat_id, mode)
    
    await event.delete()
    await event.respond(
        f"Mode switched to [{mode}]\n"
        f"[get] : Most reliable\n"
        f"[exp] : Backup for `get` [SLOW]\n"
        f"[exphd] : for HD Videos [Very SLOW]"
    )
    raise events.StopPropagation