import os
import re
import json
import http.cookiejar
import time
import random
import logging
import urllib.request
from urllib.parse import unquote
from pathlib import Path

import requests

# ─── Configuration ────────────────────────────────────────────────────────────
COOKIES_FILE = Path(__file__).parent / "cookies.txt"
BASE_DOMAIN = "dm.1024tera.com"
BASE_URL = f"https://{BASE_DOMAIN}"
VIDEOS_DIR = Path(__file__).parent / "videos"
VIDEOS_DIR.mkdir(exist_ok=True)

UA = (
    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Mobile Safari/537.36"
)

log = logging.getLogger(__name__)


# ─── Custom Exceptions ────────────────────────────────────────────────────────

class TeraBoxError(Exception):
    """Base exception for all TeraBox-related errors."""

class CookieError(TeraBoxError):
    """Raised when cookies are missing, unreadable, or invalid."""

class TokenError(TeraBoxError):
    """Raised when jsToken extraction from the share page fails."""

class ShareInfoError(TeraBoxError):
    """Raised when share metadata cannot be retrieved or parsed."""

class DownloadLinkError(TeraBoxError):
    """Raised when the download link (dlink) cannot be obtained."""

class DownloadError(TeraBoxError):
    """Raised when the actual file download fails after all retries."""


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    if not COOKIES_FILE.exists():
        raise CookieError(
            f"Cookies file not found: {COOKIES_FILE}. "
            "Export your browser cookies for 1024tera.com as cookies.txt."
        )
    session = requests.Session()
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(str(COOKIES_FILE), ignore_discard=True, ignore_expires=True)
    except http.cookiejar.LoadError as e:
        raise CookieError(f"Malformed cookies file: {e}") from e
    except OSError as e:
        raise CookieError(f"Could not read cookies file: {e}") from e
    for c in jar:
        session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    return session


def _cookie_header(session: requests.Session) -> str:
    return "; ".join(
        f"{c.name}={c.value}"
        for c in session.cookies
        if "1024tera" in (c.domain or "")
    )


def _logid() -> str:
    return str(random.randint(400_000_000_000_000_000, 999_999_999_999_999_999))


def _api_headers(session: requests.Session, surl: str) -> dict:
    return {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BASE_URL}/wap/share/filelist?surl={surl}",
        "Origin": BASE_URL,
        "Cookie": _cookie_header(session),
        "dp-logid": _logid(),
    }


def _fetch_js_token(session: requests.Session, surl: str) -> str:
    url = f"{BASE_URL}/wap/share/filelist?surl={surl}&clearCache=1"
    try:
        resp = session.get(
            url,
            headers={"User-Agent": UA, "Cookie": _cookie_header(session)},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.Timeout as e:
        raise TokenError("Timed out fetching the share page.") from e
    except requests.HTTPError as e:
        raise TokenError(f"HTTP {e.response.status_code} fetching the share page.") from e
    except requests.RequestException as e:
        raise TokenError(f"Network error fetching the share page: {e}") from e

    m = re.search(r'fn%28%22([A-Fa-f0-9]+)%22%29', resp.text)
    if m:
        return m.group(1)
    m = re.search(r'eval\(decodeURIComponent\(`([^`]+)`\)\)', resp.text)
    if m:
        decoded = unquote(m.group(1))
        m2 = re.search(r'fn\("([A-Fa-f0-9]+)"\)', decoded)
        if m2:
            return m2.group(1)
    return ""


def _get_share_info(session: requests.Session, surl: str, js_token: str) -> dict | None:
    params = {
        "app_id": "250528",
        "shorturl": f"1{surl}",
        "root": "1",
        "web": "1",
        "channel": "dubox",
        "clienttype": "0",
        "jsToken": js_token,
        "t": str(int(time.time())),
        "dp-logid": _logid(),
    }
    try:
        resp = session.get(
            f"{BASE_URL}/api/shorturlinfo",
            params=params,
            headers=_api_headers(session, surl),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.Timeout as e:
        raise ShareInfoError("Timed out fetching share info.") from e
    except requests.HTTPError as e:
        raise ShareInfoError(f"HTTP {e.response.status_code} fetching share info.") from e
    except requests.RequestException as e:
        raise ShareInfoError(f"Network error fetching share info: {e}") from e
    except ValueError as e:
        raise ShareInfoError(f"Invalid JSON in share info response: {e}") from e

    if data.get("errno") == 0:
        return data
    errno = data.get("errno")
    log.warning(f"Share info returned errno={errno}")
    return None


def _get_dlink(
    session: requests.Session,
    surl: str,
    js_token: str,
    shareid,
    uk,
    sign,
    timestamp,
    fs_id,
    randsk: str = "",
) -> str:
    params = {
        "app_id": "250528",
        "channel": "dubox",
        "clienttype": "0",
        "web": "1",
        "dp-logid": _logid(),
        "jsToken": js_token,
    }
    form = {
        "shareid": str(shareid),
        "uk": str(uk),
        "sign": sign,
        "timestamp": str(timestamp),
        "fid_list": json.dumps([int(fs_id)]),
        "primaryid": str(shareid),
        "extra": json.dumps({"sekey": unquote(randsk) if randsk else ""}),
    }
    hdrs = _api_headers(session, surl)
    hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        resp = session.post(
            f"{BASE_URL}/share/download",
            params=params,
            data=form,
            headers=hdrs,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.Timeout as e:
        raise DownloadLinkError("Timed out fetching download link.") from e
    except requests.HTTPError as e:
        raise DownloadLinkError(f"HTTP {e.response.status_code} fetching download link.") from e
    except requests.RequestException as e:
        raise DownloadLinkError(f"Network error fetching download link: {e}") from e
    except ValueError as e:
        raise DownloadLinkError(f"Invalid JSON in download link response: {e}") from e

    if data.get("errno") == 0:
        return data.get("dlink", "")
    errno = data.get("errno")
    log.warning(f"Download link API returned errno={errno}")
    return ""


def _resolve_dlink(session: requests.Session, dlink: str) -> str:
    """Follow the dlink redirect (with cookies) and return the final CDN URL."""
    try:
        r = session.get(
            dlink,
            headers={
                "User-Agent": UA,
                "Cookie": _cookie_header(session),
                "Accept-Encoding": "identity",
            },
            allow_redirects=True,
            stream=True,
            timeout=15,
        )
        r.close()
        return r.url
    except Exception as e:
        log.warning(f"Failed to resolve dlink redirect, using original URL: {e}")
        return dlink


def _download_video(url: str, dest: str) -> None:
    """Download from a CDN URL (no cookies needed) using urllib, with 3 retries."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                },
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            if os.path.getsize(dest) == 0:
                raise DownloadError("Downloaded file is empty.")
            return
        except DownloadError:
            raise
        except Exception as e:
            log.warning(f"Download attempt {attempt + 1}/3 failed: {e}")
            if os.path.exists(dest):
                os.remove(dest)
            if attempt == 2:
                raise DownloadError(
                    f"All 3 download attempts failed. Last error: {e}"
                ) from e
            time.sleep(3)


# ─── Public API ───────────────────────────────────────────────────────────────

def process_terabox_link(surl: str) -> dict:
    """
    Full pipeline: surl → dict{filename, filepath, size, thumb}.

    Raises a TeraBoxError subclass on any failure so callers can
    surface a meaningful message to the user.
    """
    session = _make_session()

    # 1. JS token
    log.info(f"[{surl}] Fetching jsToken...")
    js_token = _fetch_js_token(session, surl)
    if not js_token:
        raise TokenError("Could not extract jsToken from the share page.")

    # 2. Share metadata
    log.info(f"[{surl}] Getting share info...")
    info = _get_share_info(session, surl, js_token)
    if not info:
        raise ShareInfoError(
            "Failed to get share info (errno != 0). "
            "Your cookies may be expired — please refresh cookies.txt."
        )

    files     = info.get("list", [])
    shareid   = info.get("shareid", "")
    uk        = info.get("uk", "")
    sign      = info.get("sign", "")
    timestamp = info.get("timestamp", "")
    randsk    = info.get("randsk", "")

    if not files:
        raise ShareInfoError("The share contains no files.")

    # Use the first file in the share
    f        = files[0]
    filename = f.get("server_filename", "video.mp4")
    fs_id    = f.get("fs_id", "")
    size     = int(f.get("size", 0))
    thumb    = f.get("thumbs", {}).get("url3", "")

    # 3. Obtain download link
    log.info(f"[{surl}] Getting download link for '{filename}'...")
    dlink = _get_dlink(
        session, surl, js_token, shareid, uk, sign, timestamp, fs_id, randsk
    )
    if not dlink:
        raise DownloadLinkError("Could not obtain a download link from the API.")

    log.info(f"[{surl}] Download link obtained — size={size / 1024 / 1024:.1f} MB")

    # 4. Download to videos/; skip if already cached
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", filename)
    dest = str(VIDEOS_DIR / safe_name)

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log.info(f"[{surl}] File already cached at '{dest}', skipping download.")
    else:
        cdn_url = _resolve_dlink(session, dlink)
        log.info(f"[{surl}] Downloading to '{dest}'...")
        _download_video(cdn_url, dest)
        log.info(f"[{surl}] Download complete.")

    return {
        "filename": filename,
        "filepath": dest,
        "size": size,
        "thumb": thumb,
    }
