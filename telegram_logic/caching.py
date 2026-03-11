import os
import threading
import json

# — Local surl->message_id cache (avoids bot SearchRequest restriction) ————————

CACHE_FILE = "cache.json"

_cache_lock = threading.Lock()

def _load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _cache_put(surl: str, message_id: int) -> None:
    with _cache_lock:
        data = _load_cache()
        data[surl] = message_id
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)


def _cache_get(surl: str) -> int | None:
    with _cache_lock:
        return _load_cache().get(surl)


