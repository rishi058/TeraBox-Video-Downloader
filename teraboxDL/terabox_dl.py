import os
import logging
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

    log.info("Retrieving video metadata from proxy URL")
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

def _get_file_size_bytes(stream_download_url: str) -> int:
    try:
        response = requests.head(stream_download_url, allow_redirects=True)
        content_length = response.headers.get('Content-Length')
        if content_length is None:
            raise ValueError("Server did not provide Content-Length header.")
        
        return int(content_length)
    
    except Exception as e:
        print(f"Error: {e}")
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

    if is_hd:
        return {
            "filename": file_info.get("server_filename", "unknown"),
            "size": int(file_info.get("size", 0)),
            "download_url": file_info.get("direct_link", ""),
        }
    else:
        download_url = file_info.get("stream_download_url", "")
        new_file_size = _get_file_size_bytes(download_url)

        return {
            "filename": file_info.get("server_filename", "unknown"),
            "size": new_file_size,
            "download_url": download_url,
        }
    
# if __name__ == "__main__":
#     data = get_video_metadata("https://1024terabox.com/s/1gvhn4oF65BbRvrA_fSsuWA")

#     with open("example_response.json", "w") as f:
#         json.dump(data, f, indent=2)