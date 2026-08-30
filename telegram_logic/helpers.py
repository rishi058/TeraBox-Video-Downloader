import re

# Generic URL detector — matches any http(s) link
_GENERIC_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# Regex to match TeraBox share URLs and extract the SURL
TERA_URL_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?[\w.-]+\.[a-z]{2,}"
    r"(?:/s/1(?P<surl_path>[A-Za-z0-9_-]+)"
    r"|/(?:sharing/link|wap/share/filelist)\?[^#]*surl=(?P<surl_param>[A-Za-z0-9_-]+))",
    re.IGNORECASE,
)

# ── Experimental modes (/exp, /exphd) ────────────────────────────────────────────
# The experimental proxy resolves many more TeraBox mirror domains and accepts
# flexible path shapes, so /exp and /exphd use their own, broader matcher.
#
# Supported shapes:
#   {baseURL}/<something>/{SURL}          e.g. https://terabox.com/s/1abc
#   {baseURL}/{SURL}                      e.g. https://terabox.com/1abc
#   {baseURL}/sharing/link?...surl=1abc   (query-param form, kept for compat)
_TERABOX_EXP_DOMAINS = (
    "terabox.com", "1024terabox.com", "teraboxapp.com", "freeterabox.com",
    "terabox.app", "terabox.fun", "4funbox.co", "4funbox.com",
    "mirrobox.com", "nephobox.com", "1024tera.com", "momerybox.com",
    "tibibox.com", "terasharefile.com", "teraboxshare.com",
    "teraboxlink.com", "terafileshare.com", "terasharelink.com",
)
# Longest-first so e.g. "4funbox.com" is preferred over the "4funbox.co" prefix.
_TERABOX_EXP_DOMAIN_ALT = "|".join(
    d.replace(".", r"\.") for d in sorted(_TERABOX_EXP_DOMAINS, key=len, reverse=True)
)

TERA_EXP_URL_RE = re.compile(
    r"https?://(?:[a-z0-9]+\.)*(?:" + _TERABOX_EXP_DOMAIN_ALT + r")"
    r"(?:"
    # Query-param form: /sharing/link?...surl=<surl>
    r"/(?:sharing/link|wap/share/filelist|share/link)\?[^\s#]*?surl=1?(?P<surl_param>[A-Za-z0-9_-]+)"
    r"|"
    # Path form: optional single intermediate segment, then the SURL.
    # A leading "1" of the SURL is not captured, matching legacy behaviour.
    r"(?:/[A-Za-z0-9_.%-]+)?/1?(?P<surl_path>[A-Za-z0-9_-]+)"
    r")",
    re.IGNORECASE,
)

# — Helpers ————————————————————————————————————————————————————————————————————————

def extract_surl(text: str) -> str | None:
    """Extract the first SURL from a TeraBox URL in the message text."""
    m = TERA_URL_RE.search(text)
    if m:
        return m.group("surl_path") or m.group("surl_param")
    return None

def extract_all_terabox_url(text: str) -> list[str]:
    """Extract all unique TeraBox URLs from the message text."""
    seen: set[str] = set()
    urls: list[str] = []
    for m in TERA_URL_RE.finditer(text):
        url = m.group(0)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_surl_exp(text: str) -> str | None:
    """Extract the first SURL from an experimental-mode (/exp, /exphd) TeraBox URL."""
    m = TERA_EXP_URL_RE.search(text)
    if m:
        return m.group("surl_path") or m.group("surl_param")
    return None


def extract_all_terabox_url_exp(text: str) -> list[str]:
    """Extract all unique TeraBox share URLs supported by /exp and /exphd."""
    seen: set[str] = set()
    urls: list[str] = []
    for m in TERA_EXP_URL_RE.finditer(text):
        url = m.group(0)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls

def extract_all_surls(text: str) -> list[str]:
    """Extract all unique SURLs from TeraBox URLs in the message text."""
    seen: set[str] = set()
    surls: list[str] = []
    for m in TERA_URL_RE.finditer(text):
        surl = m.group("surl_path") or m.group("surl_param")
        if surl and surl not in seen:
            seen.add(surl)
            surls.append(surl)
    return surls


def contains_url(text: str) -> bool:
    """Return True if the text contains any http(s) URL."""
    return bool(_GENERIC_URL_RE.search(text))


def format_size(size_bytes: int) -> str:
    """Format bytes into a human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    else:
        return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 1:
        return f"{seconds:.1f}s"
    
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
        
    return " ".join(parts[:2]) # Return max 2 most significant parts for nice reading