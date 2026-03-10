import re
import json
import http.cookiejar
import time
import random
from urllib.parse import unquote
import requests

SURL = "nQ_ZSYa34UX4ptCihQN3eA"
BASE_DOMAIN = "dm.1024tera.com"
BASE_URL = f"https://{BASE_DOMAIN}"
SHARE_PAGE_URL = f"{BASE_URL}/wap/share/filelist?surl={SURL}&clearCache=1"
COOKIES_FILE = "cookies.txt"

UA = (
    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Mobile Safari/537.36"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def load_cookies(session: requests.Session) -> None:
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(COOKIES_FILE, ignore_discard=True, ignore_expires=True)
    for c in jar:
        session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    names = [c.name for c in jar]
    print(f"[+] Loaded {len(names)} cookies: {', '.join(names)}")


def cookie_header(session: requests.Session) -> str:
    return "; ".join(
        f"{c.name}={c.value}"
        for c in session.cookies
        if "1024tera" in (c.domain or "")
    )


def dp_logid() -> str:
    return str(random.randint(400_000_000_000_000_000, 999_999_999_999_999_999))


def api_headers(session: requests.Session) -> dict:
    return {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
        "Referer": SHARE_PAGE_URL,
        "Origin": BASE_URL,
        "Cookie": cookie_header(session),
        "dp-logid": dp_logid(),
    }


# ── page fetch & token extraction ────────────────────────────────────────────

def fetch_page(session: requests.Session) -> str:
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": cookie_header(session),
    }
    resp = session.get(SHARE_PAGE_URL, headers=hdrs, timeout=20)
    resp.raise_for_status()
    print(f"  Page OK — {len(resp.text)} bytes")
    return resp.text


def extract_js_token(html: str) -> str:
    m = re.search(r'fn%28%22([A-Fa-f0-9]+)%22%29', html)
    if m:
        return m.group(1)
    m = re.search(r'eval\(decodeURIComponent\(`([^`]+)`\)\)', html)
    if m:
        decoded = unquote(m.group(1))
        m2 = re.search(r'fn\("([A-Fa-f0-9]+)"\)', decoded)
        if m2:
            return m2.group(1)
    return ""


# ── API: get share info (file list + metadata) ───────────────────────────────

def get_share_info(session: requests.Session, js_token: str) -> dict | None:
    """
    Call /api/shorturlinfo with '1' prefix on shorturl (as the WAP JS does).
    Returns the full response dict on errno==0, else None.
    """
    params = {
        "app_id": "250528",
        "shorturl": f"1{SURL}",   # JS does: "1".concat(surl)
        "root": "1",
        "web": "1",
        "channel": "dubox",
        "clienttype": "0",
        "jsToken": js_token,
        "t": str(int(time.time())),
        "dp-logid": dp_logid(),
    }
    resp = session.get(
        f"{BASE_URL}/api/shorturlinfo",
        params=params,
        headers=api_headers(session),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errno") == 0:
        return data
    print(f"  [!] shorturlinfo errno={data.get('errno')}")
    return None


# ── API: get download link ────────────────────────────────────────────────────

def get_download_link(session: requests.Session, js_token: str,
                      shareid, uk, sign, timestamp, fs_id, randsk="") -> str:
    """
    POST /share/download to get a signed dlink for a specific file.
    """
    params = {
        "app_id": "250528",
        "channel": "dubox",
        "clienttype": "0",
        "web": "1",
        "dp-logid": dp_logid(),
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
    hdrs = api_headers(session)
    hdrs["Content-Type"] = "application/x-www-form-urlencoded"

    resp = session.post(
        f"{BASE_URL}/share/download",
        params=params, data=form, headers=hdrs, timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errno") == 0:
        return data.get("dlink", "")
    print(f"  [!] /share/download errno={data.get('errno')}")
    return ""


# ── resolve redirect ─────────────────────────────────────────────────────────

def resolve_dlink(session: requests.Session, dlink: str) -> str:
    try:
        r = session.get(
            dlink,
            headers={"User-Agent": UA, "Cookie": cookie_header(session)},
            allow_redirects=False, timeout=10,
        )
        return r.headers.get("Location", dlink)
    except Exception as e:
        print(f"  [!] Redirect resolve error: {e}")
        return dlink


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    session = requests.Session()
    load_cookies(session)

    # Step 1: Fetch share page & extract jsToken
    print("\n[1] Fetching share page...")
    html = fetch_page(session)
    js_token = extract_js_token(html)
    print(f"    jsToken: {js_token[:30]}..." if js_token else "    [!] jsToken NOT found")

    # Step 2: Get share info (file list, shareid, sign, etc.)
    print("\n[2] Getting share info...")
    info = get_share_info(session, js_token)
    if not info:
        print("    [!] Failed to get share info. Check cookies / link validity.")
        return

    files     = info.get("list", [])
    shareid   = info.get("shareid", "")
    uk        = info.get("uk", "")
    sign      = info.get("sign", "")
    timestamp = info.get("timestamp", "")
    randsk    = info.get("randsk", "")

    print(f"    shareid   : {shareid}")
    print(f"    uk        : {uk}")
    print(f"    sign      : {sign}")
    print(f"    timestamp : {timestamp}")
    print(f"    files     : {len(files)}")

    if not files:
        print("    [!] No files in share.")
        return

    # Step 3: For each file, get the download link
    print(f"\n{'='*60}")
    for i, f in enumerate(files):
        name    = f.get("server_filename", "?")
        fs_id   = f.get("fs_id", "")
        size_mb = int(f.get("size", 0)) / 1024 / 1024

        print(f"\n  [{i+1}] {name}")
        print(f"      Size  : {size_mb:.2f} MB")
        print(f"      fs_id : {fs_id}")

        print("      Requesting download link...")
        dlink = get_download_link(
            session, js_token, shareid, uk, sign, timestamp, fs_id, randsk
        )

        if dlink:
            print(f"      dlink : {dlink[:100]}...")
            direct = resolve_dlink(session, dlink)
            print(f"\n      *** DIRECT DOWNLOAD URL ***")
            print(f"      {direct}")
        else:
            print("      [!] Could not get download link.")

        thumbs = f.get("thumbs", {})
        if thumbs:
            print(f"      Thumb : {thumbs.get('url3', '')[:100]}")

    print(f"\n{'='*60}")
    print("Done! Use the direct URL with curl/wget/yt-dlp (include cookies).")


if __name__ == "__main__":
    main()
