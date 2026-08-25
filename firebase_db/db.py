import os
import re
import json
import base64
from urllib.parse import unquote
import logging
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


# ── Robust JSON decoder for environment variables ──────────────────────────────
# Docker, Coolify, docker-compose, and various CI systems can mangle JSON values
# in env-vars in surprising ways — especially on ARM/Linux where shell quoting
# and encoding behave differently than on Windows/macOS dev machines.
#
# Common failure modes this handles:
#   1. Extra wrapping quotes  ('{"a":1}' or "{'a':1}" or even triple-quoted)
#   2. Literal \\n instead of real newlines in PEM private_key fields
#   3. Double-escaped JSON  ("{\"a\":1}")
#   4. Base64-encoded JSON  (safest way to pass JSON through Coolify)
#   5. Unicode BOM / non-ASCII whitespace
#   6. Python-style single-quoted dicts  ({'key': 'value'})

def _decode_env_json(raw: str) -> dict:
    """Try progressively more aggressive strategies to parse *raw* into a dict."""

    # ── 0. Strip BOM / invisible unicode whitespace ──────────────────────────
    cleaned = raw.strip().lstrip("\ufeff").strip()

    # ── 1. Peel off wrapping quote layers (up to 3 deep) ────────────────────
    #    Handles:  '...'  "..."  \'...\'  \"...\"
    for _ in range(3):
        if (cleaned.startswith("\\'") and cleaned.endswith("\\'")):
            cleaned = cleaned[2:-2]
        elif (cleaned.startswith('\\"') and cleaned.endswith('\\"')):
            cleaned = cleaned[2:-2]
        elif (cleaned.startswith("'") and cleaned.endswith("'")) or \
             (cleaned.startswith('"') and cleaned.endswith('"')):
            cleaned = cleaned[1:-1]
        else:
            break

    # ── 1b. Un-escape remaining backslash-quoted characters ─────────────────
    #    Coolify/Docker often escapes ALL quotes:  \" → "  and  \' → '
    if '\\"' in cleaned or "\\'" in cleaned:
        cleaned = cleaned.replace('\\"', '"').replace("\\'", "'")

    # ── 2. Try plain json.loads first (fast path) ───────────────────────────
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            log.debug("FIREBASE_SECRETS parsed on first attempt (plain JSON).")
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # ── 3. Un-double-escape  ("{\"key\": \"val\"}"  →  {"key": "val"}) ──────
    try:
        unescaped = cleaned.replace('\\"', '"')
        result = json.loads(unescaped)
        if isinstance(result, dict):
            log.debug("FIREBASE_SECRETS parsed after un-double-escaping.")
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # ── 4. Fix literal \\n inside the string  (common PEM breakage) ─────────
    #    Also handle \\\\n  (double-double escaped)
    try:
        fixed_newlines = cleaned.replace("\\\\n", "\n").replace("\\n", "\n")
        result = json.loads(fixed_newlines)
        if isinstance(result, dict):
            log.debug("FIREBASE_SECRETS parsed after fixing escaped newlines.")
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # ── 5. Combined: un-double-escape + fix newlines ────────────────────────
    try:
        combined = cleaned.replace('\\"', '"').replace("\\\\n", "\n").replace("\\n", "\n")
        result = json.loads(combined)
        if isinstance(result, dict):
            log.debug("FIREBASE_SECRETS parsed after combined unescape + newline fix.")
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # ── 6. Try base64 decoding ──────────────────────────────────────────────
    #    Coolify tip: base64-encode the entire JSON and set that as the env var.
    #    This sidesteps ALL quoting/escaping issues.
    try:
        # Accept both standard and URL-safe base64
        decoded_bytes = base64.b64decode(cleaned, validate=True)
        result = json.loads(decoded_bytes.decode("utf-8"))
        if isinstance(result, dict):
            log.debug("FIREBASE_SECRETS parsed from base64-encoded value.")
            return result
    except Exception:
        pass

    # ── 7. Python-style dict with single quotes → JSON ──────────────────────
    #    e.g.  {'type': 'service_account', ...}
    try:
        import ast
        result = ast.literal_eval(cleaned)
        if isinstance(result, dict):
            log.debug("FIREBASE_SECRETS parsed via ast.literal_eval (Python dict).")
            return result
    except Exception:
        pass

    # ── 8. Last resort: regex-extract the first JSON object ─────────────────
    try:
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            result = json.loads(match.group(0))
            if isinstance(result, dict):
                log.debug("FIREBASE_SECRETS parsed via regex JSON extraction.")
                return result
    except (json.JSONDecodeError, ValueError):
        pass

    # ── Nothing worked — raise with diagnostics ─────────────────────────────
    preview = cleaned[:120] + ("…" if len(cleaned) > 120 else "")
    raise ValueError(
        f"Could not decode FIREBASE_SECRETS into a JSON dict.\n"
        f"  Length : {len(raw)} chars\n"
        f"  Starts: {repr(raw[:30])}\n"
        f"  Ends  : {repr(raw[-30:])}\n"
        f"  Preview (cleaned): {preview}\n"
        f"\n"
        f"TIP: Base64-encode your JSON to avoid quoting issues:\n"
        f"     base64 -w0 < service-account.json   (Linux)\n"
        f"     Then set FIREBASE_SECRETS to that base64 string."
    )


def _validate_service_account(creds: dict) -> None:
    """Sanity-check that the parsed dict looks like a Firebase service account."""
    required_keys = {"type", "project_id", "private_key", "client_email"}
    missing = required_keys - set(creds.keys())
    if missing:
        raise ValueError(
            f"FIREBASE_SECRETS is missing required keys: {missing}. "
            f"Keys present: {sorted(creds.keys())}"
        )
    if creds.get("type") != "service_account":
        log.warning(
            f"FIREBASE_SECRETS 'type' is {creds.get('type')!r}, "
            f"expected 'service_account'."
        )
    pk = creds.get("private_key", "")
    if "BEGIN" not in pk:
        raise ValueError(
            "FIREBASE_SECRETS 'private_key' does not contain a PEM header. "
            "The key may be truncated or corrupted."
        )


def _fix_private_key(creds: dict) -> None:
    """Fix PEM private_key that has literal '\\n' instead of real newlines.

    After Coolify/Docker double-escaping + JSON parsing, the private_key often
    ends up with literal two-char sequences (backslash + 'n') instead of actual
    newline characters.  The PEM parser then chokes on the backslash (byte 92).
    """
    pk = creds.get("private_key")
    if not pk or "\n" in pk:
        # Already has real newlines, or key is missing — nothing to fix
        return

    # Replace literal \n  and  \\n  sequences with real newlines
    fixed = pk.replace("\\n", "\n").replace("\\\\n", "\n")

    # Ensure the key ends with a trailing newline (PEM spec requires it)
    if not fixed.endswith("\n"):
        fixed += "\n"

    creds["private_key"] = fixed
    log.debug("Fixed private_key: replaced literal \\n with real newlines.")


def _resolve_firestore_database_id() -> str | None:
    """Normalize optional FIRESTORE_DATABASE_ID style overrides.

    If unset, or set to default aliases (including URL-encoded), return None so
    Firebase Admin uses its built-in default database routing.
    """
    raw = (
        os.getenv("FIRESTORE_DATABASE_ID")
        or os.getenv("GOOGLE_CLOUD_FIRESTORE_DATABASE_ID")
        or ""
    ).strip()
    if not raw:
        return None

    decoded = unquote(raw)
    normalized = decoded.strip().strip('"').strip("'")
    if normalized in {"(default)", "default", "%28default%29"}:
        log.warning(
            "Ignoring FIRESTORE database override=%r; using SDK default database.",
            raw,
        )
        return None

    return normalized


# ── Initialize Firebase Admin SDK (only once) ──────────────────────────────────
# Reads FIREBASE_SECRETS env-var → writes fb_secrets.json → loads from file.
# This avoids all \n / PEM-escaping issues that plague in-memory dict loading.

def _init_firebase() -> firestore.Client:
    database_id = _resolve_firestore_database_id()

    if firebase_admin._apps:
        # Already initialized (e.g. during hot-reload / testing)
        return firestore.client(database_id=database_id)

    module_dir = os.path.dirname(__file__)
    generated_path = os.path.join(module_dir, "fb_secrets.json")

    firebase_secrets_json = os.getenv("FIREBASE_SECRETS")

    if firebase_secrets_json:
        try:
            secrets_dict = _decode_env_json(firebase_secrets_json)
            _fix_private_key(secrets_dict)
            _validate_service_account(secrets_dict)

            with open(generated_path, "w", encoding="utf-8") as f:
                json.dump(secrets_dict, f, indent=2)
            log.info("Wrote credentials to %s", generated_path)

            cred = credentials.Certificate(generated_path)
            firebase_admin.initialize_app(cred)
            log.info("Firebase initialized from FIREBASE_SECRETS env-var (via file).")
        except Exception as e:
            log.error("Failed to parse FIREBASE_SECRETS env-var: %s", e)
            raise
    elif os.path.exists(generated_path):
        # Previously generated file still around
        cred = credentials.Certificate(generated_path)
        firebase_admin.initialize_app(cred)
        log.info("Firebase initialized from previously generated: %s", generated_path)
    else:
        raise RuntimeError(
            "No Firebase credentials found. "
            "Set FIREBASE_SECRETS env-var in .env."
        )

    return firestore.client(database_id=database_id)


db: firestore.Client = _init_firebase()
