"""Firestore-backed, memory-first URL cache with 256 shards per mode.

Firestore is loaded once at startup. Commands then use an in-memory snapshot,
persisted to ``runtime_cache.json`` as a fallback. Every five minutes only the
small metadata document is read; changed shards are fetched in one batch.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Literal

from .db import db

log = logging.getLogger(__name__)

MODE = Literal["get", "exp", "exphd", "dw"]
MODES: tuple[MODE, ...] = ("get", "exp", "exphd", "dw")
CACHE_COLLECTION = "cache"
SHARD_COUNT = 256
META_DOCUMENT = "_meta"
REFRESH_INTERVAL_SECONDS = 5 * 60
FIRESTORE_READ_BATCH_SIZE = 20
SNAPSHOT_PATH = Path(os.getenv("CACHE_SNAPSHOT_PATH", "runtime_cache.json"))

_LOCK = threading.RLock()
_SHARDS: dict[tuple[str, str], dict[str, dict]] = {}
_VERSIONS: dict[tuple[str, str], float] = {}
_SOURCE_INDEX: dict[tuple[str, str], tuple[str, str]] = {}
_WEIGHTED_SHARDS: list[tuple[str, str, int]] = []
_PENDING_VERSIONS: dict[tuple[str, str], float] = {}
_PENDING_NEW_SHARDS: set[tuple[str, str]] = set()
_INITIALIZED = False
_LOADING = False
_SNAPSHOT_DIRTY = False


def make_hash_id(source_url: str) -> str:
    return "h_" + hashlib.sha256(source_url.strip().encode("utf-8")).hexdigest()


def make_shard_id(hash_id: str) -> str:
    return f"{int(hash_id[2:10], 16) % SHARD_COUNT:03x}"


def make_document_id(mode: MODE, shard_id: str) -> str:
    return f"{mode}_{shard_id}"


def build_cache_record(
    source_url: str,
    download_url: str,
    filename: str | None = None,
    file_size: int | None = None,
    thumbnail_url: str | None = None,
) -> dict:
    record = {"source_url": source_url, "download_url": download_url}
    if filename:
        record["filename"] = filename
    if file_size:
        record["file_size"] = int(file_size)
    if thumbnail_url:
        record["thumbnail_url"] = thumbnail_url
    return record


def _valid_record(value) -> bool:
    return (
        isinstance(value, dict)
        and bool(value.get("source_url"))
        and bool(value.get("download_url"))
    )


def _rebuild_indexes_locked() -> None:
    _SOURCE_INDEX.clear()
    _WEIGHTED_SHARDS.clear()
    for (mode, shard_id), records in _SHARDS.items():
        valid_count = 0
        for hash_id, record in records.items():
            if not _valid_record(record):
                continue
            _SOURCE_INDEX[(mode, record["source_url"])] = (shard_id, hash_id)
            valid_count += 1
        if valid_count:
            _WEIGHTED_SHARDS.append((mode, shard_id, valid_count))


def _snapshot_payload_locked() -> dict:
    return {
        "schema": 1,
        "saved_at": time.time(),
        "versions": {
            f"{mode}:{shard_id}": version
            for (mode, shard_id), version in _VERSIONS.items()
        },
        "shards": {
            f"{mode}:{shard_id}": records
            for (mode, shard_id), records in _SHARDS.items()
        },
    }


def _save_snapshot() -> None:
    global _SNAPSHOT_DIRTY
    with _LOCK:
        payload = _snapshot_payload_locked()
    try:
        target = SNAPSHOT_PATH.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        with _LOCK:
            _SNAPSHOT_DIRTY = False
    except Exception as exc:
        log.error("Could not persist runtime cache snapshot: %s", exc)


def _load_snapshot() -> bool:
    global _INITIALIZED
    if not SNAPSHOT_PATH.exists():
        return False
    try:
        payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        shards = {}
        for key, records in (payload.get("shards") or {}).items():
            mode, shard_id = key.split(":", 1)
            if mode in MODES and isinstance(records, dict):
                shards[(mode, shard_id)] = records
        versions = {}
        for key, version in (payload.get("versions") or {}).items():
            mode, shard_id = key.split(":", 1)
            if mode in MODES:
                versions[(mode, shard_id)] = float(version or 0)
        with _LOCK:
            _SHARDS.clear()
            _SHARDS.update(shards)
            _VERSIONS.clear()
            _VERSIONS.update(versions)
            _rebuild_indexes_locked()
            _INITIALIZED = bool(_SHARDS)
        log.info("Loaded %s cache shards from %s", len(shards), SNAPSHOT_PATH)
        return bool(shards)
    except Exception as exc:
        log.error("Could not load runtime cache snapshot: %s", exc)
        return False


def _metadata_maps(data: dict) -> tuple[dict, dict]:
    modes = data.get("modes") if isinstance(data.get("modes"), dict) else {}
    versions = data.get("versions") if isinstance(data.get("versions"), dict) else {}
    return modes, versions


def _get_all_shards(refs: list, operation: str, on_batch=None) -> list:
    """Fetch large shard sets in bounded responses with progress logging."""
    snapshots = []
    total = len(refs)
    for start in range(0, total, FIRESTORE_READ_BATCH_SIZE):
        chunk = refs[start : start + FIRESTORE_READ_BATCH_SIZE]
        batch_snapshots = list(db.get_all(chunk, timeout=30))
        snapshots.extend(batch_snapshots)
        if on_batch is not None:
            on_batch(batch_snapshots)
        log.info(
            "%s: loaded %s/%s cache shards",
            operation,
            min(start + len(chunk), total),
            total,
        )
    return snapshots


async def _run_in_daemon_thread(function):
    """Run blocking Firebase work without creating shutdown-blocking threads."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def worker():
        try:
            result = function()
        except BaseException as exc:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_finish_future, future, None, exc)
        else:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_finish_future, future, result, None)

    threading.Thread(target=worker, name="firebase-cache", daemon=True).start()
    return await future


def _finish_future(future, result, error) -> None:
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)


def initialize_runtime_cache() -> bool:
    """Use JSON + differential refresh, or do one full read on first startup."""
    global _INITIALIZED, _LOADING, _SNAPSHOT_DIRTY
    _LOADING = True
    loaded_local = _load_snapshot()
    if loaded_local:
        # Normal restarts cost one metadata read plus only shards changed since
        # the JSON snapshot. A complete read is reserved for first startup.
        refresh_runtime_cache()
        _LOADING = False
        return True
    try:
        log.info("Loading runtime media cache…")
        meta = db.collection(CACHE_COLLECTION).document(META_DOCUMENT).get(timeout=15)
        meta_data = meta.to_dict() or {}
        modes, versions = _metadata_maps(meta_data)
        refs = []
        for mode, shard_counts in modes.items():
            if mode not in MODES or not isinstance(shard_counts, dict):
                continue
            for shard_id, count in shard_counts.items():
                if int(count or 0) <= 0:
                    continue
                refs.append(
                    db.collection(CACHE_COLLECTION).document(
                        make_document_id(mode, shard_id)
                    )
                )

        fresh_shards = {}

        def apply_startup_batch(batch_snapshots) -> None:
            """Publish each batch immediately, preserving concurrent writes."""
            nonlocal fresh_shards
            parsed = {}
            for snap in batch_snapshots:
                if not snap.exists:
                    continue
                mode, shard_id = snap.id.rsplit("_", 1)
                key = (mode, shard_id)
                parsed[key] = {
                    hash_id: record
                    for hash_id, record in (snap.to_dict() or {}).items()
                    if _valid_record(record)
                }
            fresh_shards.update(parsed)
            with _LOCK:
                for key, remote_records in parsed.items():
                    # Commands can add records while startup loading is in
                    # progress. Merge instead of replacing; local writes win.
                    merged = dict(remote_records)
                    merged.update(_SHARDS.get(key, {}))
                    _SHARDS[key] = merged
                _rebuild_indexes_locked()

        _get_all_shards(refs, "Startup cache", on_batch=apply_startup_batch)

        fresh_versions = {}
        for mode, mode_versions in versions.items():
            if mode not in MODES or not isinstance(mode_versions, dict):
                continue
            for shard_id, version in mode_versions.items():
                fresh_versions[(mode, shard_id)] = float(version or 0)

        with _LOCK:
            # Do not clear live state: command writes may have arrived after a
            # shard batch was read. Keep the newest local version in that race.
            for key, remote_version in fresh_versions.items():
                _VERSIONS[key] = max(_VERSIONS.get(key, 0), remote_version)
            _rebuild_indexes_locked()
            _INITIALIZED = True
            _SNAPSHOT_DIRTY = True
        _save_snapshot()
        log.info(
            "Initialized runtime cache from Firestore: %s shards, %s records",
            len(fresh_shards),
            sum(len(records) for records in fresh_shards.values()),
        )
        _LOADING = False
        return True
    except Exception as exc:
        log.error("Firestore startup cache load failed; using JSON snapshot: %s", exc)
        _LOADING = False
        with _LOCK:
            partial_ready = bool(_SHARDS)
            if partial_ready:
                _INITIALIZED = True
        return loaded_local or partial_ready


async def initialize_runtime_cache_async() -> bool:
    return await _run_in_daemon_thread(initialize_runtime_cache)


def refresh_runtime_cache() -> int:
    """Read metadata once and batch-fetch only shards changed by other workers."""
    global _SNAPSHOT_DIRTY
    try:
        flush_runtime_cache_updates()
        meta = db.collection(CACHE_COLLECTION).document(META_DOCUMENT).get(timeout=15)
        meta_data = meta.to_dict() or {}
        _, remote_versions = _metadata_maps(meta_data)
        changed = []
        polled_versions = {}
        with _LOCK:
            for mode, mode_versions in remote_versions.items():
                if mode not in MODES or not isinstance(mode_versions, dict):
                    continue
                for shard_id, version in mode_versions.items():
                    key = (mode, shard_id)
                    remote_version = float(version or 0)
                    if remote_version > _VERSIONS.get(key, 0):
                        changed.append(key)
                        polled_versions[key] = remote_version

        if changed:
            refs = [
                db.collection(CACHE_COLLECTION).document(make_document_id(mode, shard_id))
                for mode, shard_id in changed
            ]
            snapshots = {
                snap.id: snap
                for snap in _get_all_shards(refs, "Cache refresh")
            }
            with _LOCK:
                for key in changed:
                    mode, shard_id = key
                    snap = snapshots.get(make_document_id(mode, shard_id))
                    # A local write after metadata polling is newer; never let
                    # this refresh replace it with a stale shard snapshot.
                    if _VERSIONS.get(key, 0) > polled_versions[key]:
                        continue
                    if snap and snap.exists:
                        _SHARDS[key] = {
                            hash_id: record
                            for hash_id, record in (snap.to_dict() or {}).items()
                            if _valid_record(record)
                        }
                        _VERSIONS[key] = polled_versions[key]
                _rebuild_indexes_locked()
                _SNAPSHOT_DIRTY = True

        if changed or _SNAPSHOT_DIRTY:
            _save_snapshot()
        return len(changed)
    except Exception as exc:
        log.error("Runtime cache refresh failed; keeping current snapshot: %s", exc)
        return 0


async def runtime_cache_refresh_loop() -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        changed = await _run_in_daemon_thread(refresh_runtime_cache)
        if changed:
            log.info("Runtime cache refreshed %s changed shards", changed)


def shutdown_runtime_cache() -> None:
    """Persist memory locally without any shutdown-blocking network call."""
    if _SNAPSHOT_DIRTY:
        _save_snapshot()


def flush_runtime_cache_updates() -> bool:
    """Publish all locally changed shard versions with at most one DB write."""
    with _LOCK:
        pending_versions = dict(_PENDING_VERSIONS)
        pending_new_shards = set(_PENDING_NEW_SHARDS)
    if not pending_versions and not pending_new_shards:
        return True

    updates = {
        f"versions.{mode}.{shard_id}": version
        for (mode, shard_id), version in pending_versions.items()
    }
    for mode, shard_id in pending_new_shards:
        updates[f"modes.{mode}.{shard_id}"] = 1

    try:
        db.collection(CACHE_COLLECTION).document(META_DOCUMENT).update(updates)
        with _LOCK:
            for key, published_version in pending_versions.items():
                if _PENDING_VERSIONS.get(key) == published_version:
                    _PENDING_VERSIONS.pop(key, None)
            _PENDING_NEW_SHARDS.difference_update(pending_new_shards)
        return True
    except Exception as exc:
        log.error("[DB] batched cache metadata update failed: %s", exc)
        return False


def _resolve_record_key_locked(mode: MODE, source_url: str) -> tuple[str, str]:
    indexed = _SOURCE_INDEX.get((mode, source_url))
    if indexed:
        return indexed

    # A true SHA-256 collision is extraordinarily unlikely, but never overwrite
    # a different source URL if one occurs. Deterministic suffixes keep both.
    for collision_index in range(100):
        material = source_url if collision_index == 0 else f"{source_url}\0{collision_index}"
        hash_id = make_hash_id(material)
        shard_id = make_shard_id(hash_id)
        existing = _SHARDS.get((mode, shard_id), {}).get(hash_id)
        if not existing or existing.get("source_url") == source_url:
            return shard_id, hash_id
    raise RuntimeError("Could not allocate collision-safe cache key")


def add_to_cache(
    source_url: str,
    download_url: str,
    user_mode: MODE,
    filename: str | None = None,
    file_size: int | None = None,
    thumbnail_url: str | None = None,
) -> bool:
    """Write Firestore and immediately update the local runtime cache."""
    global _SNAPSHOT_DIRTY
    if not source_url or not download_url:
        return False
    record = build_cache_record(
        source_url, download_url, filename, file_size, thumbnail_url
    )
    with _LOCK:
        shard_id, hash_id = _resolve_record_key_locked(user_mode, source_url)
        shard_was_empty = not _SHARDS.get((user_mode, shard_id))
    version = time.time()
    try:
        cache = db.collection(CACHE_COLLECTION)
        cache.document(make_document_id(user_mode, shard_id)).set(
            {hash_id: record}, merge=True
        )
        with _LOCK:
            _SHARDS.setdefault((user_mode, shard_id), {})[hash_id] = record
            _VERSIONS[(user_mode, shard_id)] = version
            _rebuild_indexes_locked()
            _SNAPSHOT_DIRTY = True

            _PENDING_VERSIONS[(user_mode, shard_id)] = version
            if shard_was_empty:
                _PENDING_NEW_SHARDS.add((user_mode, shard_id))
        return True
    except Exception as exc:
        log.error("[DB] add_to_cache failed for mode=%s: %s", user_mode, exc)
        return False


def search_in_cache(source_url: str, user_mode: MODE) -> dict | None:
    """Look up a source entirely in memory; performs no Firestore operation."""
    if user_mode == "get":
        search_order: tuple[MODE, ...] = ("exphd", "exp", "get")
    elif user_mode == "exp":
        search_order = ("exphd", "exp")
    else:
        search_order = (user_mode,)

    with _LOCK:
        for mode in search_order:
            indexed = _SOURCE_INDEX.get((mode, source_url))
            if not indexed:
                continue
            shard_id, hash_id = indexed
            record = _SHARDS.get((mode, shard_id), {}).get(hash_id)
            if _valid_record(record):
                result = dict(record)
                result["_mode"] = mode
                return result
    return None


def get_random_cache_record() -> dict | None:
    """Select a random item entirely in memory; performs no Firestore read."""
    with _LOCK:
        if not _WEIGHTED_SHARDS:
            return None
        mode, shard_id, _ = random.choices(
            _WEIGHTED_SHARDS,
            weights=[count for _, _, count in _WEIGHTED_SHARDS],
            k=1,
        )[0]
        records = [record for record in _SHARDS[(mode, shard_id)].values() if _valid_record(record)]
        if not records:
            return None
        result = dict(random.choice(records))
        result["_mode"] = mode
        return result


def get_cache_for_random() -> list[dict]:
    record = get_random_cache_record()
    return [record] if record else []


def get_runtime_cache_stats() -> dict:
    with _LOCK:
        return {
            "initialized": _INITIALIZED,
            "loading": _LOADING,
            "shards": len(_SHARDS),
            "records": sum(len(records) for records in _SHARDS.values()),
        }


def _encode_key(value: str) -> str:
    return make_hash_id(value)
