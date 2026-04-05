import logging
import os
import requests
import json
import time
from typing import Literal
from dotenv import load_dotenv
load_dotenv()

GIST_ID = os.getenv("GIST_ID", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

log = logging.getLogger(__name__)

"""
Example users.json structure:
{"user_id": {"username": "username", "last_active": "timestamp", "mode": "get"}}
"""

#------------------------------------------------------------------------------------------------------------------------------

MODE = Literal["get", "exp", "exphd"]

# Stores: {"user_id": {"username": "username", "last_active": "timestamp", "mode": "get"}}
# Prevents spamming the GitHub Gist on every message.
# This cache is only used for checking last_active time. 
USERS_DATA_CACHE = {}

#------------------------------------------------------------------------------------------------------------------------------

# used for /settings command
def set_user_mode(chat_id: int, mode: MODE):
    user_data = _get_users_gist() 
    user_data[str(chat_id)]["mode"] = mode
    _update_users_gist(user_data)
    USERS_DATA_CACHE[chat_id] = user_data

# used for handling terabox messages
def get_user_mode(chat_id: int) -> MODE:   
    return USERS_DATA_CACHE.get(chat_id, {}).get("mode", "get")

# used for /broadcast command
def get_all_users() -> dict:
    return _get_users_gist()

# used in maintaining /recent command data
def track_user(chat_id: int, username: str):
    """
    Tracks a user by their chat_id and username to GitHub Gist, plus last activity.
    Saves to the Gist AT MOST every 15 minutes per user to protect rate limits.
    """
    if not GIST_ID or not GITHUB_TOKEN:
        return

    current_time = time.time()
    user_data = USERS_DATA_CACHE.get(chat_id, {})
    last_saved_time = user_data.get("last_active", 0.0)

    #! If we saved them less than 15 minutes (900s) ago, skip writing to Gist.
    if current_time - last_saved_time < 900:
        return
        
    chat_id = str(chat_id)
   
    # Re-fetch from Gist to merge changes
    users_data = _get_users_gist()
    user_info = users_data.get(chat_id, {})

    if user_info == {}:
        # New User detected
        user_info["username"] = username               
        user_info["last_active"] = current_time            
        user_info["mode"] = "get"

        log.info(f"Registered new user/group {chat_id} ({username})")
    else:
        # Update data for existing user
        user_info["last_active"] = current_time

    # Update the Whole data
    users_data[chat_id] = user_info
    
    _update_users_gist(users_data)

    # Update local cache to reset the 15-minute timer
    USERS_DATA_CACHE[chat_id] = users_data

#!-----------------------------PRIVATE HELPERS----------------------------------

def _get_users_gist() -> dict:
    """
    Fetches the users.json file from the GitHub Gist.
    Returns a dictionary of {"user_id": {"username": "foo", "last_active": 123.4, "mode": ____}}
    It automatically migrates older schemas (strings) to this new format.
    """
    if not GIST_ID or not GITHUB_TOKEN:
        log.warning("GIST_ID or GITHUB_TOKEN not set; user tracking is disabled.")
        return {}

    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"token {GITHUB_TOKEN}"}
        )
        if r.status_code == 200:
            files = r.json().get("files", {})
            if "users.json" in files:
                content = files["users.json"]["content"]
                data = json.loads(content)            
                return data
            else:
                return {} 
        else:
            log.error(f"Failed to load users from gist: HTTP {r.status_code}")
            return {}
    except Exception as e:
        log.error(f"Error reading users from Gist: {e}")
        return {}

def _update_users_gist(data: dict):
    """
    Patches the GitHub Gist to update users.json.
    """
    if not GIST_ID or not GITHUB_TOKEN:
        return

    try:
        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            json={"files": {"users.json": {"content": json.dumps(data, indent=2)}}}
        )
        if r.status_code != 200:
            log.error(f"Failed to update users in gist: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Error updating users in Gist: {e}")
