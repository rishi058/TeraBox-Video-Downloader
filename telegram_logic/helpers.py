import re

# Regex to match TeraBox share URLs and extract the SURL
TERA_URL_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?[\w.-]+\.[a-z]{2,}"
    r"(?:/s/1(?P<surl_path>[A-Za-z0-9_-]+)"
    r"|/(?:sharing/link|wap/share/filelist)\?[^#]*surl=(?P<surl_param>[A-Za-z0-9_-]+))",
    re.IGNORECASE,
)

# — Helpers ————————————————————————————————————————————————————————————————————————

def extract_surl(text: str) -> str | None:
    """Extract the first SURL from a TeraBox URL in the message text."""
    m = TERA_URL_RE.search(text)
    if m:
        return m.group("surl_path") or m.group("surl_param")
    return None


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
    minutes = int(seconds) // 60
    secs = seconds - (minutes * 60)
    if minutes > 0:
        return f"{minutes}m {secs:.1f}s"
    return f"{secs:.1f}s"