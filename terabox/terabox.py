import os
from .public_api import prepare_terabox_link, download_terabox_file
from .internal_helpers import TeraBoxError, CancelledError   # noqa: F401

# ── Standalone entry point ────────────────────────────────────────────────────

def _standalone_download(surl: str) -> None:
    print(f"[1] Preparing link (surl={surl})...")
    prepared = prepare_terabox_link(surl)
    filename = prepared["filename"]
    size_mb = prepared["size"] / 1048576
    print(f"    File: {filename} ({size_mb:.1f} MB)")
    print("[2] Downloading...")
    mp4_path = download_terabox_file(prepared)
    final_mb = os.path.getsize(mp4_path) / 1048576
    print(f"\n    Saved: {mp4_path} ({final_mb:.1f} MB)")


if __name__ == "__main__":
    _standalone_download("nOvK6r4RyVtnYKOxmoqp0w")
