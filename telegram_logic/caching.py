import requests
import json
import logging
import os
import time
from dotenv import load_dotenv
load_dotenv()

GIST_ID = os.getenv("GIST_ID", "0")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "0")

log = logging.getLogger(__name__)

# In-memory snapshot used by /random — avoids a GitHub API call on every request.
# Structure: {"data": <cache dict>, "timestamp": <unix time float or 0>}
CACHE_STORAGE: dict = {"data": {}, "timestamp": 0.0}

CACHE_TTL_SECONDS = 15 * 60  # 15 minutes

"""
Example cache structure:
{"itcyfhOhGMocHJv2vgaAqA": 23}
SURL : message_id 

At start cache.json will be only with {}.
We cannot optimize here(lazy loading etc) bcz for cache system we need latest data always.
"""

#------------------------------------------------------------------------------------------------------------------------------

# github doesn't support partial update of gist file, so we need to read the whole file, update it and then write it back.
def get_cache() -> dict:
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers={"Authorization": f"token {GITHUB_TOKEN}"})
    if r.status_code == 200:
        content = r.json()["files"]["cache.json"]["content"]
        return json.loads(content)
    else:
        return {}
    
def update_cache(data: dict):
    requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"token {GITHUB_TOKEN}"},
        json={"files": {"cache.json": {"content": json.dumps(data)}}}
    )

#------------------------------------------------------------------------------------------------------------------------------

def add_to_cache(key: str, value: int):
    cache_data = get_cache()
    cache_data[key] = value
    update_cache(cache_data)
 
def search_in_cache(key: str) -> int:
    cache_data = get_cache()
    value = cache_data.get(key, -1)
    if value != -1:
        log.info(f"Cache hit for key: {key}")
    return value

#------------------------------------------------------------------------------------------------------------------------------

def get_cache_for_random() -> dict:
    """Return cache data for /random without hitting the API on every call.

    Uses the module-level CACHE_STORAGE snapshot.
    Refreshes from GitHub only when the snapshot is older than CACHE_TTL_SECONDS.
    """
    global CACHE_STORAGE
    age = time.time() - CACHE_STORAGE["timestamp"]
    if age < CACHE_TTL_SECONDS and CACHE_STORAGE["data"]:
        log.debug(f"Serving /random from in-memory snapshot (age={age:.0f}s)")
        return CACHE_STORAGE["data"]

    log.info("CACHE_STORAGE expired or empty — refreshing from GitHub Gist")
    fresh = get_cache()
    CACHE_STORAGE["data"] = fresh
    CACHE_STORAGE["timestamp"] = time.time()
    return fresh

#------------------------------------------------------------------------------------------------------------------------------

# if __name__ == "__main__":
#     add_to_cache("itcyfhOhGMocHJv2vgaAqA", 23)
#     print(search_in_cache("itcyfhOhGMocHJv2vgaAqA")) # should print 23
#     print(search_in_cache("non_existent_key")) # should print -1