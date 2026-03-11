# TeraBox Telegram Bot

A Telegram bot that accepts TeraBox share links and delivers the video file directly inside Telegram  no browser, no third-party site required.

---

## Overview

Users send (or paste) any TeraBox share URL to the bot. The bot resolves the link, downloads the video via the TeraBox streaming API (using browser-extracted cookies for authentication), converts the `.ts` stream to `.mp4`, and uploads it to the chat. Downloaded files are cached in a private Telegram storage group so the same link is never fetched twice.

---

## Features

- **Auto-detect links**  paste a TeraBox URL anywhere in a message; no command needed
- **`/get <url>`**  explicit download command; supports multiple URLs at once
- **`/random`**  re-sends a random previously cached video
- **`/info`**  displays current chat and user details
- **Cancel button**  inline button to abort an in-progress download
- **Telegram-side caching**  uploads each video once to a storage group and re-forwards on repeat requests (`cache.json` maps surl  message ID)
- **Quality fallback**  tries 1080p  720p  480p  360p automatically

---

## Project Structure

```
main.py                        # Entry point  starts the bot and registers commands
.env                           # Secrets (not committed)

telegram_logic/
  bot.py                       # TelegramClient setup, core download-and-upload pipeline
  caching.py                   # Thread-safe local cache (surl  Telegram message ID)
  helpers.py                   # URL extraction, size/duration formatting
  progress_callbacks.py        # Live progress messages during download & upload
  commands/
    start.py                   # /start handler
    get.py                     # /get <url> handler
    random.py                  # /random handler
    info.py                    # /info handler
    cancel_download.py         # Inline "Cancel" callback handler

terabox/
  public_api.py                # Public interface: prepare_terabox_link(), download_terabox_file()
  core_pipeline.py             # Low-level pipeline: session, JS token, share info, TS download, ffmpeg conversion
  internal_helpers.py          # Shared utilities and custom exceptions (TeraBoxError, CancelledError)
  terabox.py                   # Standalone CLI entry point for the terabox module

sample_terabox_downloader/     # Standalone script for testing the download pipeline outside Telegram
storage/                       # Temporary directory for downloaded files before upload
cache.json                     # Persistent surl  message_id mapping
cookies.txt                    # Netscape-format cookies for TeraBox authentication
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- `ffmpeg` available on `PATH` (used for `.ts`  `.mp4` conversion)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` includes: `telethon`, `cryptg`, `requests`, `python-dotenv`

### 3. Configure environment

Create a `.env` file in the project root:

```env
BOT_TOKEN=your_telegram_bot_token
APP_ID=your_telegram_app_id
API_HASH=your_telegram_api_hash
STORAGE_GROUP_ID=your_private_group_id   # optional  disables caching if omitted
```

- `BOT_TOKEN`  from [@BotFather](https://t.me/BotFather)
- `APP_ID` / `API_HASH`  from [my.telegram.org](https://my.telegram.org)
- `STORAGE_GROUP_ID`  the numeric ID of a private group/channel the bot is admin of; used as a video store

### 4. Add cookies

The bot authenticates with TeraBox using browser cookies. See [Extracting Cookies](#extracting-cookies) below, then save them to `cookies.txt` in Netscape format.

### 5. Run

```bash
python main.py
```

---

## Extracting Cookies

The TeraBox download pipeline requires authenticated session cookies. To extract them:

1. Open any TeraBox share link in a **desktop browser** and log in.
2. Open the same link again so the video preview loads.
3. Open **DevTools  Network** tab while the page loads.
4. Find the top-level request to `surl?...` that returns **200 OK** (not a redirect).
5. Copy all cookies from its **Request Headers  Cookie** field into `cookies.txt`.

---

## Third-Party Download API (Reference)

> The bot currently uses the first-party TeraBox API with cookies. The notes below document a third-party proxy (`teraboxdl.site`) explored during development.

**Base URL:** `https://teraboxdl.site/`

**Endpoint:** `POST /api/proxy`

**Request payload:**
```json
{
    "url": "https://1024terabox.com/s/1nOvK6r4RyVtnYKOxmoqp0w"
}
```

**Response (abbreviated):**
```json
{
    "errno": 0,
    "list": [
        {
            "server_filename": "video.mp4",
            "size": 110970629,
            "formatted_size": "105.83 MB",
            "direct_link": "https://api.teraboxdl.site/download?url=...",
            "stream_url": "https://api.teraboxdl.site/get_m3u8_stream_fast?q=..."
        }
    ],
    "total_size": "105.83 MB"
}
```

The `direct_link` value is a fully-formed download URL that triggers a file download when opened in a browser.