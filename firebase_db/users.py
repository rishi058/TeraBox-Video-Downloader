"""
firebase_db/users.py
~~~~~~~~~~~~~~~~~~~~
Firestore-backed user tracking, replacing the old GitHub Gist approach.

Firestore layout
----------------
Collection : users
Document   : <chat_id>          (string)
Fields     :
    username    : str | None
    last_active : float          (Unix timestamp)
    mode        : "get" | "exp" | "exphd"

Why per-document instead of one giant document?
  - Partial updates: update a single user's field without touching anyone else.
  - No read-modify-write cycle needed for most operations.
  - Scales naturally with user growth.
"""

import logging
import time
from typing import Literal

from google.cloud.firestore_v1 import DELETE_FIELD  # noqa: F401 — available if needed
from firebase_admin import firestore as _fs

from .db import db
from .write_queue import write_queue

log = logging.getLogger(__name__)

# ── Types ──────────────────────────────────────────────────────────────────────

MODE = Literal["get", "exp", "exphd"]

# ── In-memory cache (reduces Firestore reads) ──────────────────────────────────
# Structure: { str(chat_id): {"username": ..., "last_active": float, "mode": ...} }
# Populated on first read; kept in sync on every write.
_USERS_CACHE: dict[str, dict] = {}

_USERS_COLLECTION = "users"
_WRITE_DEBOUNCE_SECONDS = 900  # 15 minutes — same throttle as the old Gist impl


# ── Public API ─────────────────────────────────────────────────────────────────

def track_user(chat_id: int, username: str | None) -> None:
    """
    Record / refresh a user's activity in Firestore.

    Writes are debounced to AT MOST once every 15 minutes per user to stay
    within Firestore free-tier write quotas (and because last_active precision
    below 15 min is irrelevant for /recent).

    New users are always written immediately.

    Errors are caught and logged — never propagated to callers.
    """
    uid = str(chat_id)
    current_time = time.time()

    cached = _USERS_CACHE.get(uid, {})
    last_saved = cached.get("last_active", 0.0)

    is_new_user = not cached  # nothing in local cache → unknown → check Firestore
    is_stale    = (current_time - last_saved) >= _WRITE_DEBOUNCE_SECONDS

    if not is_new_user and not is_stale:
        return  # Skip — within debounce window

    try:
        ref = db.collection(_USERS_COLLECTION).document(uid)

        if is_new_user:
            # Cold-start: check Firestore to avoid overwriting an existing user
            # (read is NOT queued — reads don't cause rate-limit issues)
            snap = ref.get()
            if snap.exists:
                existing = snap.to_dict()
                _USERS_CACHE[uid] = existing
                last_saved = existing.get("last_active", 0.0)
                if (current_time - last_saved) < _WRITE_DEBOUNCE_SECONDS:
                    return  # Already updated recently — no write needed
                # Update only last_active — fire-and-forget via queue
                write_queue.enqueue_nowait(ref.update, {"last_active": current_time})
                _USERS_CACHE[uid] = {**existing, "last_active": current_time}
                log.debug(f"Updated last_active for existing user {uid} ({username})")
                return
            else:
                # Brand-new user — fire-and-forget via queue
                user_data = {
                    "username":    username,
                    "last_active": current_time,
                    "mode":        "get",
                }
                write_queue.enqueue_nowait(ref.set, user_data)
                _USERS_CACHE[uid] = user_data
                log.info(f"Registered new user {uid} ({username})")
                return

        # Returning user past debounce window — fire-and-forget via queue
        write_queue.enqueue_nowait(ref.update, {"last_active": current_time})
        _USERS_CACHE[uid]["last_active"] = current_time
        log.debug(f"Refreshed last_active for user {uid}")

    except Exception as e:
        log.error(f"[DB] track_user failed for uid={uid}: {e}")
        # Non-fatal — tracking is best-effort, do not crash the bot


def get_user_mode(chat_id: int) -> MODE:
    """
    Return the user's current download mode.
    Reads from in-memory cache first; falls back to Firestore on cold-start.
    Default: "get"

    Returns "get" on any DB error so the bot stays functional.
    """
    uid = str(chat_id)

    if uid in _USERS_CACHE:
        return _USERS_CACHE[uid].get("mode", "get")

    try:
        # Cold-start: fetch from Firestore once, then cache
        snap = db.collection(_USERS_COLLECTION).document(uid).get()
        if snap.exists:
            data = snap.to_dict()
            _USERS_CACHE[uid] = data
            return data.get("mode", "get")
    except Exception as e:
        log.error(f"[DB] get_user_mode failed for uid={uid}: {e}")

    return "get"  # Unknown user or DB error → default mode


def set_user_mode(chat_id: int, mode: MODE) -> bool:
    """
    Persist the user's chosen download mode.
    Single-field update — no read-modify-write needed.

    Returns True on success, False on DB error.
    Raises no exceptions.
    """
    uid = str(chat_id)
    ref = db.collection(_USERS_COLLECTION).document(uid)
    # Blocking write — caller (/settings callback) needs to know if it succeeded
    ok = write_queue.enqueue_wait(ref.set, {"mode": mode}, merge=True)
    if ok:
        # Keep local cache in sync
        if uid in _USERS_CACHE:
            _USERS_CACHE[uid]["mode"] = mode
        else:
            _USERS_CACHE[uid] = {"mode": mode}
        log.info(f"Set mode={mode} for user {uid}")
    else:
        log.error(f"[DB] set_user_mode write failed for uid={uid}")
    return ok


def get_all_users() -> dict[str, dict]:
    """
    Return all users as { str(chat_id): {username, last_active, mode} }.
    Used by /recent and /broadcast — these are infrequent admin commands,
    so a full collection scan is acceptable.

    Returns an empty dict on DB error.
    """
    try:
        docs = db.collection(_USERS_COLLECTION).stream()
        result: dict[str, dict] = {}
        for doc in docs:
            result[doc.id] = doc.to_dict()
        return result
    except Exception as e:
        log.error(f"[DB] get_all_users failed: {e}")
        return {}
