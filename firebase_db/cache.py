"""Firestore URL cache using 256 deterministic shards per download mode.

Schema::

    cache/{mode}_{000..0ff} = {
        "h_<sha256(source_url)>": {
            "source_url": "...",
            "download_url": "...",
        }
    }

    cache/_meta = {
        "modes": {
            "dw": {"000": 123, ...},
            "exp": {...},
            "exphd": {...},
            "get": {...},
        }
    }

The metadata counts let /random choose a shard proportionally to its number of
items. Metadata is cached in memory, so a normal /random call costs one read.
"""

import hashlib
import logging
import random
import time
from typing import Literal

from .db import db

log = logging.getLogger(__name__)

MODE = Literal["get", "exp", "exphd", "dw"]
MODES: tuple[MODE, ...] = ("get", "exp", "exphd", "dw")
CACHE_COLLECTION = "cache"
SHARD_COUNT = 256
META_DOCUMENT = "_meta"
_META_TTL_SECONDS = 15 * 60
_META_SNAPSHOT: dict = {"modes": {}, "timestamp": 0.0}


def make_hash_id(source_url: str) -> str:
    """Return the deterministic internal ID for a source URL."""
    return "h_" + hashlib.sha256(source_url.strip().encode("utf-8")).hexdigest()


def make_shard_id(hash_id: str) -> str:
    """Map a hash ID to a three-digit hexadecimal shard ID."""
    return f"{int(hash_id[2:10], 16) % SHARD_COUNT:03x}"


def make_document_id(mode: MODE, shard_id: str) -> str:
    return f"{mode}_{shard_id}"


def build_cache_record(source_url: str, download_url: str) -> dict:
    return {"source_url": source_url, "download_url": download_url}


def add_to_cache(source_url: str, download_url: str, user_mode: MODE) -> bool:
    """Add one URL record; existing shards normally cost exactly one write."""
    if not source_url or not download_url:
        return False
    hash_id = make_hash_id(source_url)
    shard_id = make_shard_id(hash_id)
    try:
        db.collection(CACHE_COLLECTION).document(
            make_document_id(user_mode, shard_id)
        ).set({hash_id: build_cache_record(source_url, download_url)}, merge=True)

        # Only a previously empty shard needs a metadata write. The first add
        # after process startup may read the small metadata document; all
        # subsequent adds reuse the 15-minute in-memory snapshot.
        modes = _get_mode_shard_counts()
        mode_shards = modes.get(user_mode, {}) if isinstance(modes, dict) else {}
        if shard_id not in mode_shards:
            db.collection(CACHE_COLLECTION).document(META_DOCUMENT).update(
                {f"modes.{user_mode}.{shard_id}": 1}
            )
            modes.setdefault(user_mode, {})[shard_id] = 1
        return True
    except Exception as exc:
        log.error("[DB] add_to_cache failed for mode=%s: %s", user_mode, exc)
        return False


def search_in_cache(source_url: str, user_mode: MODE) -> dict | None:
    """Find a URL record using one read per quality mode attempted."""
    hash_id = make_hash_id(source_url)
    shard_id = make_shard_id(hash_id)
    if user_mode == "get":
        search_order: tuple[MODE, ...] = ("exphd", "exp", "get")
    elif user_mode == "exp":
        search_order = ("exphd", "exp")
    else:
        search_order = (user_mode,)

    for mode in search_order:
        try:
            snap = db.collection(CACHE_COLLECTION).document(
                make_document_id(mode, shard_id)
            ).get(field_paths=[hash_id])
            record = (snap.to_dict() or {}).get(hash_id) if snap.exists else None
            if isinstance(record, dict) and record.get("download_url"):
                return record
        except Exception as exc:
            log.error("[DB] cache lookup failed for mode=%s: %s", mode, exc)
    return None


def _get_mode_shard_counts() -> dict[str, dict[str, int]]:
    age = time.time() - _META_SNAPSHOT["timestamp"]
    if age < _META_TTL_SECONDS and _META_SNAPSHOT["modes"]:
        return _META_SNAPSHOT["modes"]

    try:
        snap = db.collection(CACHE_COLLECTION).document(META_DOCUMENT).get()
        modes = (snap.to_dict() or {}).get("modes", {}) if snap.exists else {}
        if isinstance(modes, dict):
            _META_SNAPSHOT["modes"] = modes
            _META_SNAPSHOT["timestamp"] = time.time()
            return modes
    except Exception as exc:
        log.error("[DB] cache metadata read failed: %s", exc)
    return {}


def get_random_cache_record() -> dict | None:
    """Select an approximately uniform random item using normally one read."""
    modes = _get_mode_shard_counts()
    weighted_shards: list[tuple[str, str]] = []
    weights: list[int] = []
    for mode, shards in modes.items():
        if mode not in MODES or not isinstance(shards, dict):
            continue
        for shard_id, count in shards.items():
            if isinstance(count, int) and count > 0:
                weighted_shards.append((mode, shard_id))
                weights.append(count)

    if not weighted_shards:
        return None

    # Metadata should be exact. Retry only protects against a stale/empty shard.
    for _ in range(3):
        mode, shard_id = random.choices(weighted_shards, weights=weights, k=1)[0]
        try:
            snap = db.collection(CACHE_COLLECTION).document(
                make_document_id(mode, shard_id)
            ).get()
            records = [
                record
                for record in (snap.to_dict() or {}).values()
                if isinstance(record, dict)
                and record.get("source_url")
                and record.get("download_url")
            ]
            if records:
                record = dict(random.choice(records))
                # Runtime-only context used to refresh metadata for /random.
                # It is not persisted or displayed to users.
                record["_mode"] = mode
                return record
        except Exception as exc:
            log.error("[DB] random shard read failed: %s", exc)
            return None
    return None


def get_cache_for_random() -> list[dict]:
    """Compatibility wrapper used by the /random handler."""
    record = get_random_cache_record()
    return [record] if record else []


# Kept for old migration imports.
def _encode_key(value: str) -> str:
    return make_hash_id(value)
