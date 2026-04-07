import requests
import json
import logging
import os
import time
from typing import Literal
from dotenv import load_dotenv
load_dotenv()

GIST_ID = os.getenv("GIST_ID", "0")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "0")

log = logging.getLogger(__name__)

MODE = Literal["get", "exp", "exphd"]

# In-memory snapshot used by /random — avoids a GitHub API call on every request.
# Structure: {"data": <merged flat dict>, "timestamp": <unix time float or 0>}
CACHE_STORAGE: dict = {"data": {}, "timestamp": 0.0}

CACHE_TTL_SECONDS = 15 * 60  # 15 minutes

"""
New cache structure:
{
    "get":   {"surl1": message_id1, ...},
    "exp":   {"surl1": message_id1, ...},
    "exphd": {"surl1": message_id1, ...}
}

Search order by user_mode:
  get   → exphd → exp → get   (highest quality first)
  exp   → exphd → exp
  exphd → exphd only

Write: surl+msg_id is stored ONLY in the bucket matching user_mode.
"""

#------------------------------------------------------------------------------------------------------------------------------

# github doesn't support partial update of gist file, so we need to read the whole file, update it and then write it back.
def get_cache() -> dict:
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers={"Authorization": f"token {GITHUB_TOKEN}"})
    if r.status_code == 200:
        content = r.json()["files"]["cache.json"]["content"]
        return json.loads(content)
    else:
        return {"get": {}, "exp": {}, "exphd": {}}
    
def update_cache(data: dict):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"token {GITHUB_TOKEN}"},
        json={"files": {"cache.json": {"content": json.dumps(data, indent=2)}}}
    )

#------------------------------------------------------------------------------------------------------------------------------

def add_to_cache(key: str, value: int, user_mode: MODE):
    """Store surl→msg_id in the bucket that matches user_mode."""
    cache_data = get_cache()
    cache_data.setdefault(user_mode, {})[key] = value
    update_cache(cache_data)

def search_in_cache(key: str, user_mode: MODE) -> int:
    """
    Search for surl across buckets in priority order based on user_mode.

    get   → searches exphd, then exp, then get
    exp   → searches exphd, then exp
    exphd → searches exphd only

    Returns message_id (int) on hit, -1 on miss.
    """
    cache_data = get_cache()

    if user_mode == "get":
        search_order = ["exphd", "exp", "get"]
    elif user_mode == "exp":
        search_order = ["exphd", "exp"]
    else:  # exphd
        search_order = ["exphd"]

    for bucket in search_order:
        value = cache_data.get(bucket, {}).get(key, -1)
        if value != -1:
            log.info(f"Cache hit for key={key} in bucket={bucket} (user_mode={user_mode})")
            return value

    return -1

#------------------------------------------------------------------------------------------------------------------------------

def get_cache_for_random() -> dict:
    """Return a merged flat dict of all 3 buckets for /random, without hitting
    the API on every call.

    Merges get + exp + exphd into one dict (exphd wins on key conflicts).
    Refreshes from GitHub only when the snapshot is older than CACHE_TTL_SECONDS.
    """
    global CACHE_STORAGE
    age = time.time() - CACHE_STORAGE["timestamp"]
    if age < CACHE_TTL_SECONDS and CACHE_STORAGE["data"]:
        log.debug(f"Serving /random from in-memory snapshot (age={age:.0f}s)")
        return CACHE_STORAGE["data"]

    log.info("CACHE_STORAGE expired or empty — refreshing from GitHub Gist")
    fresh = get_cache()

    # Merge all buckets into one flat dict for random selection
    merged: dict = {}
    for bucket in ("get", "exp", "exphd"):
        merged.update(fresh.get(bucket, {}))

    CACHE_STORAGE["data"] = merged
    CACHE_STORAGE["timestamp"] = time.time()
    return merged

#------------------------------------------------------------------------------------------------------------------------------