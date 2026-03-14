import requests
import json
import logging
import os
from dotenv import load_dotenv
load_dotenv()

GIST_ID = os.getenv("GIST_ID", "0")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "0")

log = logging.getLogger(__name__)

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

# if __name__ == "__main__":
#     add_to_cache("itcyfhOhGMocHJv2vgaAqA", 23)
#     print(search_in_cache("itcyfhOhGMocHJv2vgaAqA")) # should print 23
#     print(search_in_cache("non_existent_key")) # should print -1