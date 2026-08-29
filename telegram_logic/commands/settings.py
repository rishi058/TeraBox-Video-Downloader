from telethon import events, Button
import logging
from ..bot import bot
from firebase_db.users import (
    set_user_mode, get_user_mode, set_user_terabox_mode, get_user_terabox_mode,
)

log = logging.getLogger(__name__)
AVAILABLE_MODES = ["get", "exp", "exphd", "dw", "fz", "all"]
TERABOX_SUBMODES = ["get", "exp", "exphd"]

@bot.on(events.NewMessage(pattern="/settings"))
async def cmd_settings(event):
    log.info(f"Received /settings command from chat {event.chat_id}")
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
            f"\n🏠 **Chat Title:** {chat_title}\n"
            f"🆔 **Chat ID:** `{chat_id}`\n"
            f"🔗 **Chat Username:** {'@' + chat_username if chat_username else 'None'}\n"
            f"📂 **Chat Type:** {chat_type}"
        )
    else:
        chat_info = (
            f"\n🆔 **Private Chat ID:** `{chat_id}`"
        )

    try:
        current_mode = get_user_mode(chat_id)
    except Exception as e:
        log.error(f"[/settings] DB error fetching mode for chat {chat_id}: {e}")
        await event.respond(
            "⚠️ **Database error** — could not load your settings.\n"
            "Please try again in a moment."
        )
        raise events.StopPropagation

    available_modes = AVAILABLE_MODES.copy()
    if current_mode in available_modes:
        available_modes.remove(current_mode)  # will have N elements, make them buttons

    try:
        current_tb_mode = get_user_terabox_mode(chat_id)
    except Exception as e:
        log.error(f"[/settings] DB error fetching terabox_mode for chat {chat_id}: {e}")
        current_tb_mode = "exp"

    text = (
        f"👤 **User ID:** `{user_id}`\n"
        f"📛 **Username:** {username}\n"
        f"⚙️ **Current DL Mode:** `{current_mode}`\n"
        f"🎬 **TeraBox Engine (used in `all`):** `{current_tb_mode}`\n"
        + chat_info
    )

    buttons = [[Button.inline(f"🔄 Switch to {mode}", data=f"setmode_{mode}")] for mode in available_modes]
    buttons += [
        [Button.inline(f"🎬 TeraBox engine → {mode}", data=f"settbmode_{mode}")]
        for mode in TERABOX_SUBMODES if mode != current_tb_mode
    ]

    await event.respond(text, buttons=buttons)
    raise events.StopPropagation

@bot.on(events.CallbackQuery(pattern=b"setmode_(.*)"))
async def cb_set_mode(event):
    mode = event.pattern_match.group(1).decode("utf-8")
    chat_id = event.chat_id
    
    log.info(f"Mode Switch to {mode}, for user {chat_id}")

    try:
        success = set_user_mode(chat_id, mode)
    except Exception as e:
        log.error(f"[cb_set_mode] Unexpected DB error for chat {chat_id}: {e}")
        success = False

    if not success:
        await event.respond(
            "⚠️ **Database error** — could not save your mode setting.\n"
            "Please try again in a moment."
        )
        raise events.StopPropagation

    await event.delete()
    await event.respond(
        f"✅ **Mode switched successfully to [{mode}]**\n\n"
        f"➡️ **get** : Unstable [Use for small files]\n"
        f"➡️ **exp** : Reliable & Fast [Recommended]\n"
        f"➡️ **exphd** : For HD Videos [Slow]\n"
        f"➡️ **dw** : Download Diskwala videos\n"
        f"➡️ **fz** : Download Flezen videos\n"
        f"➡️ **all** : Auto-detect TeraBox/Diskwala/Flezen links "
        f"[TeraBox links use your **TeraBox engine** setting below]"
    )
    raise events.StopPropagation


@bot.on(events.CallbackQuery(pattern=b"settbmode_(.*)"))
async def cb_set_terabox_mode(event):
    mode = event.pattern_match.group(1).decode("utf-8")
    chat_id = event.chat_id

    log.info(f"TeraBox engine switch to {mode}, for user {chat_id}")

    try:
        success = set_user_terabox_mode(chat_id, mode)
    except Exception as e:
        log.error(f"[cb_set_terabox_mode] Unexpected DB error for chat {chat_id}: {e}")
        success = False

    if not success:
        await event.respond(
            "⚠️ **Database error** — could not save your TeraBox engine setting.\n"
            "Please try again in a moment."
        )
        raise events.StopPropagation

    await event.delete()
    await event.respond(
        f"✅ **TeraBox engine (used in `all` mode) switched to [{mode}]**\n\n"
        f"➡️ **get** : Unstable [Use for small files]\n"
        f"➡️ **exp** : Reliable & Fast [Recommended]\n"
        f"➡️ **exphd** : For HD Videos [Slow]"
    )
    raise events.StopPropagation