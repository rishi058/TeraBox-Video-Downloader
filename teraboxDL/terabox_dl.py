import os
import time
import random
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
import requests

log = logging.getLogger(__name__)

THIRD_PARTY_TERABOXDL_URL = os.getenv("THIRD_PARTY_TERABOXDL_URL")
PROXY_URL = os.getenv("PROXY_URL")

def _get_video_metadata(terabox_url: str) -> dict:
    if not PROXY_URL:
        raise Exception("PROXY_URL not found in ENV")
        
    if not THIRD_PARTY_TERABOXDL_URL:
        raise Exception("THIRD_PARTY_TERABOXDL_URL not found in ENV")

    payload = {
        "cmd": "request.post2",
        "base_url": f"{THIRD_PARTY_TERABOXDL_URL}",
        "post_endpoint": "api/proxy",
        "post_json_body": f'{{"url": "{terabox_url}"}}'
    }

    # Random jitter delay to stagger concurrent requests and avoid shadow banning
    delay = random.uniform(0.1, 2.5)
    log.info(f"Retrieving video metadata from proxy URL (jitter delay: {delay:.2f}s)")
    time.sleep(delay)

    response = requests.post(PROXY_URL, json=payload, timeout=600)

    if response.status_code != 200:
        raise Exception(f"Proxy request failed with status code {response.status_code}")

    response_dict = response.json()
    log.info(f"Time taken: {response_dict['time_taken']}, for proxy URL to return data")
    
    target_url_response = response_dict.get("target_url_response")
    if not target_url_response:
        raise Exception(f"Missing 'target_url_response' in proxy response: {response_dict}")

    body = target_url_response.get("body")
    if body is None:
        raise Exception(f"Missing 'body' in target_url_response: {target_url_response}")

    return body

_STREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def _segment_size(segment_url: str) -> int:
    """Return a segment's transfer size without downloading its contents."""
    with requests.head(
        segment_url,
        headers=_STREAM_HEADERS,
        allow_redirects=True,
        timeout=15,
    ) as response:
        if response.ok:
            content_length = response.headers.get("Content-Length")
            if content_length:
                return int(content_length)

    # Some CDNs reject HEAD but report the complete size in Content-Range.
    with requests.get(
        segment_url,
        headers={**_STREAM_HEADERS, "Range": "bytes=0-0"},
        stream=True,
        timeout=15,
    ) as response:
        response.raise_for_status()
        content_range = response.headers.get("Content-Range", "")
        if "/" in content_range:
            return int(content_range.rsplit("/", 1)[1])
        return int(response.headers.get("Content-Length", 0))


def _get_file_size_bytes(stream_download_url: str) -> int:
    """Return a direct file size or the summed transfer size of an HLS stream."""
    try:
        with requests.head(
            stream_download_url,
            headers=_STREAM_HEADERS,
            allow_redirects=True,
            timeout=15,
        ) as response:
            if response.ok:
                content_type = response.headers.get("Content-Type", "").lower()
                content_length = response.headers.get("Content-Length")
                if content_length and "mpegurl" not in content_type:
                    return int(content_length)

        # The provider's stream endpoint rejects HEAD with a small JSON error.
        # Fetch the manifest instead of mistaking that error body's length for
        # the video size.
        with requests.get(
            stream_download_url,
            headers=_STREAM_HEADERS,
            stream=True,
            timeout=30,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if "mpegurl" not in content_type:
                return int(response.headers.get("Content-Length", 0))
            manifest = response.text
            manifest_url = response.url
        
        segment_urls = [
            urljoin(manifest_url, line.strip())
            for line in manifest.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not segment_urls:
            raise ValueError("HLS manifest has no media segments")

        # Segment HEAD requests provide the stream's actual transfer size while
        # avoiding the cost of downloading every segment during metadata lookup.
        with ThreadPoolExecutor(max_workers=min(8, len(segment_urls))) as executor:
            sizes = list(executor.map(_segment_size, segment_urls))
        if any(size <= 0 for size in sizes):
            raise ValueError("At least one HLS segment has no discoverable size")
        return sum(sizes)
    except Exception as exc:
        log.warning("Could not determine stream file size: %s", exc)
        return 0


#!--------PUBLIC API------------

def get_video_info(terabox_url: str, is_hd: bool) -> dict:
    data = _get_video_metadata(terabox_url)

    # with open("example_response.json", "w") as f:
    #     json.dump(data, f, indent=2)

    if data.get("error"):
        raise Exception(data.get("message", "Unknown error in getting video metadata"))
    if "list" not in data or not data["list"]:
        raise Exception("Video list not found or empty in metadata response")

    file_info = data["list"][0]
    thumbs = file_info.get("thumbs") or {}
    if not isinstance(thumbs, dict):
        thumbs = {}
    thumbnail_url = thumbs.get("url1") or thumbs.get("url2") or thumbs.get("icon")

    if is_hd:
        return {
            "filename": file_info.get("server_filename", "unknown"),
            "size": int(file_info.get("size", 0)),
            "download_url": file_info.get("direct_link", ""),
            "thumbnail_url": thumbnail_url,
            "duration": int(file_info.get("duration") or 0),
        }
    else:
        download_url = file_info.get("stream_url", "")
        new_file_size = _get_file_size_bytes(download_url)

        return {
            "filename": file_info.get("server_filename", "unknown"),
            "size": new_file_size,
            "download_url": download_url,
            "thumbnail_url": thumbnail_url,
            "duration": int(file_info.get("duration") or 0),
        }
    
# if __name__ == "__main__":
#     data = get_video_metadata("https://1024terabox.com/s/1gvhn4oF65BbRvrA_fSsuWA")

#     with open("example_response.json", "w") as f:
#         json.dump(data, f, indent=2)
