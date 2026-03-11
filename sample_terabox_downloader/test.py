"""
Test script: reads TeraBox share links from test-input.txt, extracts each
SURL, and fully downloads + converts every file to MP4 to confirm end-to-end
functionality.  Downloads are saved to the 'test_downloads/' folder.
"""
import os
import re
import sys
import time
import sample_terabox_downloader.sample_terabox_downloader as td

INPUT_FILE = "test-input.txt"
OUT_DIR = "test_downloads"
QUALITIES = ["M3U8_AUTO_1080", "M3U8_AUTO_720", "M3U8_AUTO_480", "M3U8_AUTO_360"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_surl(url: str) -> str | None:
    """Extract SURL from a TeraBox share URL.

    URL format: https://1024terabox.com/s/1<SURL>
    The leading '1' is part of the path slug but NOT part of the SURL used
    by the API (e.g. shorturl param uses '1'+SURL, surl param uses SURL).
    """
    m = re.search(r'/s/1([A-Za-z0-9_\-]+)', url)
    return m.group(1) if m else None


def test_link(session, surl: str, out_dir: str) -> tuple[bool, str]:
    """Fully download all files in a share (TS → MP4) and return status.

    Patches the module-level td.SURL so all helper functions use the correct
    share URL. Downloads are written to *out_dir*.

    Returns (success, message).
    """
    original = td.SURL
    td.SURL = surl
    try:
        js_token = td.get_js_token(session)

        info = td.get_share_info(session, js_token)
        files = info.get("list", [])
        if not files:
            return False, "No files found in share"

        shareid, uk = info["shareid"], info["uk"]
        sign, timestamp = info["sign"], info["timestamp"]
        os.makedirs(out_dir, exist_ok=True)

        downloaded: list[str] = []

        for f in files:
            name = f["server_filename"]
            fs_id = f["fs_id"]
            size_mb = int(f.get("size", 0)) / 1_048_576
            safe = td._safe_filename(name)
            base = safe.rsplit(".", 1)[0] if "." in safe else safe
            ts_path = os.path.join(out_dir, base + ".ts")
            mp4_path = os.path.join(out_dir, base + ".mp4")

            print(f"      File: {name} ({size_mb:.1f} MB)")

            succeeded = False
            for quality in QUALITIES:
                try:
                    print(f"        [{quality}] resolving stream...", flush=True)
                    stream_url = td.build_streaming_url(
                        shareid, uk, sign, timestamp, fs_id, quality
                    )
                    full_url, ts_size = td.fetch_full_ts_url(session, stream_url)
                    print(f"        TS size: {ts_size / 1_048_576:.1f} MB — downloading...")
                    td.download_ts(session, full_url, ts_path, ts_size)
                    td.convert_ts_to_mp4(ts_path, mp4_path)
                    final_mb = os.path.getsize(mp4_path) / 1_048_576
                    print(f"        Saved: {mp4_path} ({final_mb:.1f} MB)")
                    downloaded.append(f"{name} → {final_mb:.1f} MB")
                    succeeded = True
                    break
                except Exception as exc:
                    print(f"        [{quality}] failed: {exc}")
                    for p in (ts_path, mp4_path):
                        if os.path.exists(p) and os.path.getsize(p) < 1024:
                            os.remove(p)

            if not succeeded:
                return False, f"All qualities failed for '{name}'"

        summary = f"{len(files)} file(s) downloaded: " + " | ".join(downloaded)
        return True, summary

    except Exception as exc:
        return False, str(exc)
    finally:
        td.SURL = original


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Read links
    try:
        with open(INPUT_FILE, encoding="utf-8") as fh:
            raw_urls = [line.strip() for line in fh if line.strip()]
    except FileNotFoundError:
        print(f"[!] Input file not found: {INPUT_FILE}")
        sys.exit(1)

    print(f"[*] Loaded {len(raw_urls)} URL(s) from {INPUT_FILE}")
    print(f"[*] Downloads will be saved to: {os.path.abspath(OUT_DIR)}\n")

    # Load session once (shared across all tests)
    try:
        session = td.load_session()
    except Exception as exc:
        print(f"[!] Failed to load session: {exc}")
        sys.exit(1)

    results: list[tuple[str, str | None, bool, str]] = []

    for idx, url in enumerate(raw_urls, start=1):
        surl = extract_surl(url)

        if surl is None:
            print(f"[{idx:2d}] SKIP  {url}")
            print(f"         Reason: could not extract SURL from URL\n")
            results.append((url, None, False, "Could not extract SURL"))
            continue

        link_dir = os.path.join(OUT_DIR, surl)
        print(f"[{idx:2d}] {url}")
        print(f"      SURL={surl}")

        ok, msg = test_link(session, surl, link_dir)
        label = "OK  " if ok else "FAIL"
        print(f"      {label} — {msg}\n")
        results.append((url, surl, ok, msg))

        time.sleep(0.5)  # polite delay between shares

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(1 for *_, ok, _ in results if ok)
    print("=" * 64)
    print(f"RESULTS: {passed} / {len(results)} links are downloadable")

    failed = [(url, msg) for url, _, ok, msg in results if not ok]
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for url, msg in failed:
            print(f"  {url}")
            print(f"    → {msg}")

    print("=" * 64)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
