"""Firestore-backed source/download URL catalogue.

Layout::

    cache/{mode}/{sha256(source_url)} = {
        "source_url": "...",
        "download_url": "...",
    }

Telegram message IDs are intentionally not stored.  The opaque hash is only a
Firestore-safe field name and is never shown to users.
"""

import hashlib
import logging
import time
from typing import Literal

from .db import db

log = logging.getLogger(__name__)

MODE = Literal["get", "exp", "exphd", "dw"]
_CACHE_COLLECTION = "cache"
_RANDOM_BUCKETS = ("exp", "exphd", "dw")
_RANDOM_SNAPSHOT: dict = {"data": [], "timestamp": 0.0}
_RANDOM_TTL_SECONDS = 15 * 60


def _bucket_ref(bucket: str):
    return db.collection(_CACHE_COLLECTION).document(bucket)


def make_hash_id(source_url: str) -> str:
    """Return the internal deterministic ID for a source URL."""
    # Prefix keeps the value valid as an unquoted Firestore field path.
    return "h_" + hashlib.sha256(source_url.strip().encode("utf-8")).hexdigest()


def add_to_cache(source_url: str, download_url: str, user_mode: MODE) -> bool:
    """Store a resolved URL record. Returns False rather than raising on DB errors."""
    if not source_url or not download_url:
        return False
    hash_id = make_hash_id(source_url)
    record = {"source_url": source_url, "download_url": download_url}
    try:
        _bucket_ref(user_mode).set({hash_id: record}, merge=True)
        _RANDOM_SNAPSHOT["timestamp"] = 0.0
        log.debug("Cache add: bucket=%s hash_id=%s", user_mode, hash_id)
        return True
    except Exception as exc:
        log.error("[DB] add_to_cache failed for mode=%s: %s", user_mode, exc)
        return False


def search_in_cache(source_url: str, user_mode: MODE) -> dict | None:
    """Return a URL record for a source URL, or None when unavailable."""
    hash_id = make_hash_id(source_url)
    if user_mode == "get":
        search_order = ("exphd", "exp", "get")
    elif user_mode == "exp":
        search_order = ("exphd", "exp")
    else:
        search_order = (user_mode,)

    for bucket in search_order:
        try:
            snap = _bucket_ref(bucket).get(field_paths=[hash_id])
            record = (snap.to_dict() or {}).get(hash_id) if snap.exists else None
            if isinstance(record, dict) and record.get("download_url"):
                return record
        except Exception as exc:
            log.error("[DB] cache lookup failed for bucket=%s: %s", bucket, exc)
    return None


def get_cache_for_random() -> list[dict]:
    """Return all valid URL records used by /random (cached for 15 minutes)."""
    age = time.time() - _RANDOM_SNAPSHOT["timestamp"]
    if age < _RANDOM_TTL_SECONDS and _RANDOM_SNAPSHOT["data"]:
        return list(_RANDOM_SNAPSHOT["data"])

    # Key by source URL so the highest-quality/later bucket wins duplicates.
    merged: dict[str, dict] = {}
    for bucket in _RANDOM_BUCKETS:
        try:
            snap = _bucket_ref(bucket).get()
            for record in (snap.to_dict() or {}).values() if snap.exists else ():
                if not isinstance(record, dict):
                    continue  # ignore legacy surl -> Telegram message-id entries
                source_url = record.get("source_url")
                download_url = record.get("download_url")
                if source_url and download_url:
                    merged[source_url] = {
                        "source_url": source_url,
                        "download_url": download_url,
                    }
        except Exception as exc:
            log.error("[DB] random cache read failed for bucket=%s: %s", bucket, exc)

    records = list(merged.values())
    _RANDOM_SNAPSHOT["data"] = records
    _RANDOM_SNAPSHOT["timestamp"] = time.time()
    return list(records)


# Kept for old migration imports; new records use SHA-256 IDs.
def _encode_key(value: str) -> str:
    return make_hash_id(value)
