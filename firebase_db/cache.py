"""
firebase_db/cache.py
~~~~~~~~~~~~~~~~~~~~
Firestore-backed video cache, replacing the old GitHub Gist approach.

Firestore layout
----------------
Collection : cache
Documents  : "get" | "exp" | "exphd"
Fields     : { <surl>: <message_id (int)>, ... }

Why one document per bucket instead of one massive document?
  - Each bucket is updated independently — no cross-bucket contention.
  - A single-field update (dot-notation path) writes exactly one surl→msg_id
    without touching the rest of the document.
  - Firestore document size limit is 1 MiB; at ~50-100 bytes per entry that
    gives headroom for ~10k-20k cached surls per bucket — plenty.

Search priority (same as old Gist implementation):
  get   → exphd → exp → get
  exp   → exphd → exp
  exphd → exphd only
"""

import base64
import logging
import time
from typing import Literal

from .db import db
from .write_queue import write_queue

log = logging.getLogger(__name__)

# ── Types & constants ──────────────────────────────────────────────────────────

MODE = Literal["get", "exp", "exphd"]
_CACHE_COLLECTION = "cache"
_BUCKETS = ("get", "exp", "exphd")

# ── In-memory snapshot for /random ────────────────────────────────────────────
# Avoids a Firestore read on every /random call.
# { "data": {surl: msg_id, ...}, "timestamp": float }
_RANDOM_SNAPSHOT: dict = {"data": {}, "timestamp": 0.0}
_RANDOM_TTL_SECONDS = 15 * 60  # 15 minutes


# ── Helpers ────────────────────────────────────────────────────────────────────

def _bucket_ref(bucket: str):
    return db.collection(_CACHE_COLLECTION).document(bucket)


# ── Public API ─────────────────────────────────────────────────────────────────

def add_to_cache(surl: str, message_id: int, user_mode: MODE) -> bool:
    """
    Store surl → message_id in the bucket matching user_mode.

    Uses a single-field set with merge=True so we touch ONLY the new key —
    no read-modify-write cycle required.

    The write is submitted to the rate-limited queue (fire-and-forget).
    Returns True immediately (optimistic) since the video is already
    delivered; cache misses just cause a re-download, not data loss.
    """
    safe_key = _encode_key(surl)
    ref = _bucket_ref(user_mode)

    write_queue.enqueue_nowait(ref.set, {safe_key: message_id}, merge=True)
    log.debug(f"Cache add queued: bucket={user_mode}, surl={surl}, msg_id={message_id}")

    # Invalidate /random snapshot so it refreshes on next call
    _RANDOM_SNAPSHOT["timestamp"] = 0.0
    return True


def search_in_cache(surl: str, user_mode: MODE) -> int:
    """
    Search for surl across buckets in priority order.

    Returns message_id (int) on hit, -1 on miss or DB error.

    Priority:
      get   → exphd, exp, get
      exp   → exphd, exp
      exphd → exphd only

    Each bucket is fetched lazily and only if the previous ones missed,
    minimising Firestore reads on hits in high-priority buckets.
    """
    safe_key = _encode_key(surl)

    if user_mode == "get":
        search_order = ["exphd", "exp", "get"]
    elif user_mode == "exp":
        search_order = ["exphd", "exp"]
    else:  # exphd
        search_order = ["exphd"]

    for bucket in search_order:
        try:
            snap = _bucket_ref(bucket).get(field_paths=[safe_key])
            if snap.exists:
                data = snap.to_dict() or {}
                msg_id = data.get(safe_key, -1)
                if msg_id != -1:
                    log.info(f"Cache hit: bucket={bucket}, surl={surl}, msg_id={msg_id} (user_mode={user_mode})")
                    return int(msg_id)
        except Exception as e:
            log.error(f"[DB] search_in_cache failed for bucket={bucket}, surl={surl}: {e}")
            # Continue trying remaining buckets — return -1 at end on full failure

    log.debug(f"Cache miss: surl={surl} (user_mode={user_mode})")
    return -1


def get_cache_for_random() -> dict:
    """
    Return a merged flat dict of ALL 3 buckets for /random.

    Refreshes from Firestore at most every 15 minutes (TTL-based snapshot).
    Merge order: get → exp → exphd  (exphd wins on key conflicts, highest quality).

    Returns an empty dict on DB error.
    """
    global _RANDOM_SNAPSHOT

    age = time.time() - _RANDOM_SNAPSHOT["timestamp"]
    if age < _RANDOM_TTL_SECONDS and _RANDOM_SNAPSHOT["data"]:
        log.debug(f"Serving /random from in-memory snapshot (age={age:.0f}s)")
        return _RANDOM_SNAPSHOT["data"]

    log.info("Refreshing /random snapshot from Firestore")

    merged: dict = {}
    for bucket in _BUCKETS:
        try:
            snap = _bucket_ref(bucket).get()
            if snap.exists:
                bucket_data = snap.to_dict() or {}
                # Decode keys back to surls before merging
                merged.update({_decode_key(k): v for k, v in bucket_data.items()})
        except Exception as e:
            log.error(f"[DB] get_cache_for_random failed for bucket={bucket}: {e}")
            # Continue with remaining buckets

    _RANDOM_SNAPSHOT["data"]      = merged
    _RANDOM_SNAPSHOT["timestamp"] = time.time()
    return merged


# ── Key encoding ───────────────────────────────────────────────────────────────
# Firestore field names must match [a-zA-Z_][a-zA-Z_0-9]*-.
# TeraBox surls contain hyphens and may start with digits, both of which are
# rejected by Firestore's unquoted property-path parser.
#
# Strategy: URL-safe Base64-encode the UTF-8 bytes of the surl, then:
#   • replace '-' → '_'  (base64url minus sign is invalid in field names)
#   • prefix with 'k_'   (ensures the name starts with a letter, not a digit)
#   • strip trailing '=' padding (not needed; length is implicit)
# Example: "W5BZoNNJzQdm0gOa-uiU2g" → "k_VzVCWm9OTkp6UWRtMGdPYS11aVUyZw"

def _encode_key(surl: str) -> str:
    """Encode a surl so it is safe as a Firestore field name."""
    b64 = base64.urlsafe_b64encode(surl.encode()).decode().rstrip("=")
    return "k_" + b64.replace("-", "_")


def _decode_key(field: str) -> str:
    """Reverse of _encode_key."""
    # Strip the 'k_' prefix, restore '-', re-add padding
    b64 = field[2:].replace("_", "-")
    padding = (-len(b64)) % 4
    return base64.urlsafe_b64decode(b64 + "=" * padding).decode()
