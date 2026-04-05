import json
import os
import time
import logging
import platform
import threading
import queue
import requests
from DrissionPage import ChromiumPage, ChromiumOptions

log = logging.getLogger(__name__)

BASE_URL = "https://teraboxdl.site"
END_POINT = "/api/proxy"

# — Chrome Port Pool ——————————————————————————————————————————————————————————
# Each Chrome instance runs on its own port with its own user-data-dir.
# This gives true concurrency (up to POOL_SIZE simultaneous Chrome sessions).
# Increase POOL_SIZE for more parallelism, but each Chrome uses ~200-400MB RAM.

CHROME_POOL_SIZE = int(os.environ.get("CHROME_POOL_SIZE", "3"))
_CHROME_BASE_PORT = 9222

_port_pool = queue.Queue()
for _i in range(CHROME_POOL_SIZE):
    _port_pool.put(_CHROME_BASE_PORT + _i)

# log.info(f"[Chrome Pool] Initialized with {CHROME_POOL_SIZE} slots (ports {_CHROME_BASE_PORT}-{_CHROME_BASE_PORT + CHROME_POOL_SIZE - 1})")


def _get_video_metadata(terabox_url: str) -> dict:
    # Block until a port is available (acts as a bounded semaphore)
    port = _port_pool.get()
    log.info(f"[Chrome:{port}] Acquired slot for: {terabox_url}")

    co = ChromiumOptions()
    co.headless(False)  # Must be False to solve Cloudflare reliably in most setups
    co.auto_port(False)
    co.set_local_port(port)
    co.set_user_data_path(os.path.join(os.getcwd(), "storage", f"chrome_profile_{port}"))

    if platform.system() == 'Linux':
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')
        co.set_argument('--window-size=1280,720')
        co.set_browser_path('/usr/bin/google-chrome')
    else:
        co.set_argument('--window-size=800,600')

    page = None
    try:
        page = ChromiumPage(co)
        # 1. Navigate to the main site to trigger and solve Cloudflare Turnstile
        page.get(BASE_URL + '/')

        # Give CF Turnstile 5-6 seconds to verify the bot
        time.sleep(6)

        # 2. Execute the API request inside the verified browser context
        js_code = f"""
        return fetch('{END_POINT}', {{
            method: 'POST',
            headers: {{
                'Content-Type': 'application/json'
            }},
            body: JSON.stringify({{ url: '{terabox_url}' }})
        }}).then(res => res.json()).catch(err => {{ return {{error: true, message: err.toString()}} }});
        """

        result = page.run_js(js_code)

        if not result:
            return {"error": True, "message": "Failed to get a response from browser JS fetch"}

        return result

    except MemoryError:
        log.critical(f"[Chrome:{port}] OUT OF MEMORY while processing: {terabox_url}")
        return {"error": True, "message": "Server out of memory — try again later"}
    except OSError as e:
        # Catches "Cannot allocate memory" and similar OS-level resource errors
        log.critical(f"[Chrome:{port}] OS error (likely OOM): {e}")
        return {"error": True, "message": f"Server resource error: {e}"}
    except Exception as e:
        log.error(f"[Chrome:{port}] Error: {e}")
        return {"error": True, "message": str(e)}
    finally:
        if page:
            try:
                page.quit()
            except Exception as quit_err:
                log.warning(f"[Chrome:{port}] Failed to quit cleanly: {quit_err}")
                # Force-kill any leftover Chrome process on this port
                _force_kill_chrome(port)
        _port_pool.put(port)
        log.info(f"[Chrome:{port}] Released slot for: {terabox_url}")


def _force_kill_chrome(port: int):
    """Last-resort cleanup: kill any Chrome process bound to this debugging port."""
    try:
        if platform.system() == 'Windows':
            os.system(f'netstat -ano | findstr :{port} | findstr LISTENING > nul && '
                       f'for /f "tokens=5" %p in (\'netstat -ano ^| findstr :{port} ^| findstr LISTENING\') do taskkill /F /PID %p 2>nul')
        else:
            os.system(f"fuser -k {port}/tcp 2>/dev/null")
    except Exception:
        pass


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

    if is_hd:
        return {
            "filename": data["list"][0]["server_filename"],
            "size": int(data["list"][0]["size"]),
            "download_url": data["list"][0]["direct_link"],
        }
    else:
        download_url = data["list"][0]["stream_download_url"]
        new_file_size = _get_file_size_bytes(download_url)

        return {
            "filename": data["list"][0]["server_filename"],
            "size": new_file_size,
            "download_url": download_url,
        }
    
# if __name__ == "__main__":
#     data = get_video_metadata("https://1024terabox.com/s/1gvhn4oF65BbRvrA_fSsuWA")

#     with open("example_response.json", "w") as f:
#         json.dump(data, f, indent=2)