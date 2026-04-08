# TeraBox Video Downloader

Downloads full-length videos from TeraBox by reconstructing them from HLS streaming segments.

---

## Features

- **Auto-detect links**: Paste a TeraBox URL anywhere in a message; it will auto-download according to your selected mode.
- **Dual Download Engines**:
  - **Traditional (`/get`)**: Budget-capped TS chunk collector relying on rotating cookies.
  - **Experimental (`/exp` / `/exphd`)**: A fast, concurrent headless browser pool (Chromium) to extract and download fast CDN links, bypassing API rate limits.
- **`/random`**: Re-sends a random previously cached video.
- **`/settings`**: Switch between default auto-download modes (`get`, `exp`, `exphd`).
- **`/info`**: Displays current chat and user details.
- **Admin Commands**:
  - **`/recent`**: Show recent users interacting with the bot.
  - **`/broadcast`**: Broadcast a message to all known users and groups.
- **Cancel button**: Inline button to abort an in-progress download.
- **Telegram-side caching**: Uploads each video once to a storage group and re-forwards on repeat requests (`cache.json` & Gist database).
- **Persistent DB via GitHub Gist**: Tracks users, chat IDs, and user settings seamlessly.
- **Flood Control Queue**: Custom semaphore and async queue handling to prevent `FloodWaitError` during viral moments.
- **Quality fallback**: Tries 1080p -> 720p -> 480p -> 360p automatically (on traditional pipeline).

---

## Flow Architecture

```text
       [ User ] --(TeraBox URL)--> [ Telegram Bot (main.py) ]
                                            |
                            (Route based on User Selected Mode)
                          /                                     \
               [ Traditional Mode ]                     [ Experimental Mode ]
              (telegram_logic/terabox_trad.py)         (telegram_logic/terabox_exp.py)
                /                  \                     /                   \
         [ Cache Hit ]        [ TeraBox API ]     [ Cache Hit ]     [ Headless Chrome Pool ]
        (caching.py)         (terabox/public_api)                    (teraboxDL/terabox_dl.py)
              |                     |                                          |
              |            [ TS Chunk Downloader ]                [ Video Extraction Pipeline ]
              |                     |                                          |
              \            [ FFMPEG TS Remuxer ]                               |
               \                    |                                         /
                \                   |                                        /
                 \---------- [ Telegram Upload ] ---------------------------/
                         (progress_callbacks.py)
                                    |
                       [ Storage Group & Cache DB ]
                      (telegram_logic/database.py)
```

---

## Project Structure

```text
main.py                        # Entry point, FastAPI wrapper, and bot command registration
.env                           # Secrets (not committed)
Dockerfile                     # Docker container configuration
requirements.txt               # Python package dependencies
apt.txt                        # OS-level dependencies (ffmpeg, chromium, etc.)

telegram_logic/
  bot.py                       # Helper functions for bot components and core bot setup
  caching.py                   # Thread-safe local cache (surl -> message ID)
  database.py                  # User activity and mode tracking mapped via GitHub Gist
  helpers.py                   # URL extraction, size/duration formatting
  progress_callbacks.py        # Live progress messages editing during download & upload
  queue.py                     # Custom task queue logic handling API flood blocks gracefully
  terabox_trad.py              # Traditional download pipeline integration
  terabox_exp.py               # Experimental concurrent extractor pipeline integration
  commands/                    # Individual Telegram command handlers
    start.py                   # /start handler
    get.py                     # /get <url> handler
    random.py                  # /random handler
    recent.py                  # /recent handler (Admin)
    broadcast.py               # /broadcast handler (Admin)
    settings.py                # /settings handler (User Download Modes)
    experimental.py            # /exp and /exphd handlers
    cancel_download.py         # Inline "Cancel" callback handler
    info.py                    # /info handler

terabox/                       # Traditional API approach
  public_api.py                # Public interface for traditional pipeline
  core_pipeline.py             # Internal extraction, chunk discovery, ts download
  internal_helpers.py          # Shared utilities and custom exceptions

teraboxDL/                     # Next-Gen Extractor approach
  terabox_dl.py                # Headless chromium pool for concurrent metadata extracting
  public_api.py                # Interface for headless requests
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- `ffmpeg` available on `PATH` (used for `.ts` -> `.mp4` conversion)
- `chromium` or Google Chrome installed on the host (for headless pool processing)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

Create a `.env` file in the project root:

```env
BOT_TOKEN=your_telegram_bot_token
APP_ID=your_telegram_app_id
API_HASH=your_telegram_api_hash
STORAGE_GROUP_ID=-1001234567890         # the numeric ID of a private group/-channel
ADMIN_ID=12345678                       # Your user ID to access /broadcast and /recent

# GitHub Gist DB
GIST_ID=your_github_gist_hash
GITHUB_TOKEN=your_github_personal_access_token

# Experimental Headless Browser Settings
CHROME_POOL_SIZE=3

# Traditional Cookies (Netscape string format)
COOKIES1=browserid=...; TSID=...
COOKIES2=...
```

- `BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `APP_ID` / `API_HASH` — from [my.telegram.org](https://my.telegram.org)
- `STORAGE_GROUP_ID` — must be a supergroup ID (starts with `-100`). The bot must be admin.
- `GIST_ID` & `GITHUB_TOKEN` — to persist application configurations and users automatically on GitHub.
- `CHROME_POOL_SIZE` — max amount of concurrent Chromium tasks to keep memory in check.

### 4. Add cookies (For Traditional Mode)

The bot authenticates with TeraBox using browser cookies. Save your copied cookie header directly inside `.env` under `COOKIES1` and `COOKIES2`. Or you can save them in `cookies.txt` in Netscape format.

See [Extracting Cookies](#extracting-cookies) below.

### 5. Run

Locally:
```bash
python main.py
```

Using Docker:
```bash
docker build -t terabox-bot .
docker run -d --env-file .env terabox-bot
```

---

## Limitations

1. **Telegram File Size Limit**: Telegram restricts standard bot file uploads to **50 MB** and strictly restricts using local API servers to **2 GB**. Any resulting video chunk transcoded to more than maximum limits will fail.
2. **Rate Limits & API Bans**: TeraBox API rate-limits aggressively on the traditional (`/get`) approach. We use budget limits to avoid IP bans but this may leave >1 hour videos missing a sub-segment (skip ~4 minutes).
3. **RAM & CPU Overhead**: The experimental (`/exp`) module uses a Headless Chrome pool. Running several concurrent instances requires at least `1GB` of free RAM and moderate CPU power. Scale `CHROME_POOL_SIZE` down if deployed on a low-end VPS.
4. **Link Expirations & CSRF Tokens**: Token scraping occasionally breaks when TeraBox updates its CDN logic, necessitating pipeline tweaks.

---

## Extracting Cookies (For Traditional pipeline)

The TeraBox traditional download pipeline requires authenticated session cookies. To extract them:

1. Open any TeraBox share link in a **desktop browser** and log in.
2. Open the same link again so the video preview loads.
3. Open **DevTools -> Network** tab while the page loads.
4. Find the top-level request to `surl?...` that returns **200 OK** (not a redirect).
5. Copy all cookies from its **Request Headers -> Cookie** field into your `.env` (as `COOKIES1`=...).

---

## Key Concepts

### What Are Chunks / Segments?

TeraBox does **not** give you a single download link for large videos. Instead, the video is internally split into **N sequential chunks** (also called "TS segments"), each roughly covering a **~4-minute window** of the video.

Each chunk is a `.ts` (MPEG Transport Stream) file named with an index suffix like `_0_ts`, `_1_ts`, `_2_ts` … `_N_ts`.  To reconstruct the full video, you must download **every** chunk in order and remux them into a single `.mp4`.

### Which Endpoints Do We Hit?

| # | Endpoint / URL | Purpose | Returns |
|---|----------------|---------|---------|
| 1 | `GET /wap/share/filelist?surl=…` | Load the share page HTML | HTML containing `jsToken` (anti-CSRF) |
| 2 | `GET /api/shorturlinfo?shorturl=…&jsToken=…` | Fetch file metadata | JSON with `shareid`, `uk`, `sign`, `timestamp`, `fs_id`, file names, sizes |
| 3 | `GET /share/streaming?…&type=M3U8_AUTO_1080&start=0` | Request HLS playlist | M3U8 text — returns **one random chunk** (see below) |
| 4 | `GET <cdn_url>/chunk_N.ts?range=0-…&len=…` | Download a single TS chunk | Raw binary `.ts` data |

> **Important:** Each chunk URL contains a **unique cryptographic signature** in its path.  You cannot fabricate or guess URLs — every chunk URL must come from an actual API response.

---

## Current Approach: Budget-Capped Collector

The current algorithm treats the problem pragmatically: **collect as many chunks as possible within a request budget, accept occasional gaps**.

### How It Works

1. **Blind poll** the streaming endpoint repeatedly (the `start` param is ignored, so we just send `start=0`)
2. **Track** discovered chunks by their unique `_N_ts` index in the URL path
3. **Stop** when either condition fires:

| Rule | Condition | Purpose |
|------|-----------|---------|
| **Early stop** | `is_complete()` AND `no_new_max_streak >= max(10, max_idx)` | Confident we have everything |
| **Budget cap** | `req_count >= max(30, max_idx × 3)`, hard capped at **100** | Prevent rate-limiting |

### `is_complete()` Logic

Returns `True` only when:
- `min(known) ≤ 1` — chunks start at index 0 or 1
- All indices between min and max are present (no gaps)

### API Request Estimates

| Video Length | Est. Chunks | Budget Cap | Expected Found | Expected Missing |
|:-------------|:-----------|:-----------|:--------------|:----------------|
| **10 min**   | 3          | 30         | 3 (all)       | 0               |
| **30 min**   | 8          | 30         | ~8            | ~0              |
| **40 min**   | 10         | 30         | ~9-10         | 0-1             |
| **1 hour**   | 15         | 45         | ~14           | ~1              |
| **2 hours**  | 30         | 90 → 100 (cap) | ~29      | ~1              |

> [!WARNING]
> **Tradeoff:** This approach may miss 1-2 chunks on unlucky runs for longer videos. A missing chunk means ~4 minutes of video is lost. This is an acceptable tradeoff vs. getting shadow-banned by the API.

---

## Edge Cases & How They're Handled

| Edge Case | How It's Handled |
|-----------|------------------|
| **Very short video (1-2 chunks)** | Min budget of 30 requests. More than enough to find 1-2 chunks and confirm no others exist. |
| **Network error during M3U8 query** | `query_random_chunk` catches `RequestException`, sleeps 2s, returns empty (loop continues). |
| **Network error on TS chunk download** | `_download_segment` retries up to **3 times** with 2s delays. |
| **Non-M3U8 response (throttled/banned)** | Sleeps 0.5s and returns empty (loop continues, budget still ticks down). |
| **All quality levels fail** | `QUALITIES` cascades: `1080 → 720 → 480 → 360`. Each failure triggers cleanup. |
| **Gaps remain after budget** | Missing indices are printed as warnings (⚠). Video is assembled from available chunks. |

---


Here is exactly how a scenario plays out when Telegram hits you with a `FloodWaitError` and the custom message queue kicks in:

### The Scenario: A Viral Moment
Suppose your bot goes viral in a large group, and 50 users all send a TeraBox link at the exact same minute. 

1. **Working Normally (Semaphore):** 
   - The bot receives 50 links almost simultaneously.
   - The Semaphore (`asyncio.Semaphore(20)`) immediately grabs the first 20 links and starts checking their cache/downloading them. The other 30 are waiting patiently in memory.
   - The 20 active pipelines all send a message back: `🔍 Checking cache for...`. They also start updating their `status.edit(...)` texts (`0%`, `10%`, etc.).

2. **The Breaking Point (`FloodWaitError` happens):**
   - Because 20 active jobs are constantly editing their status messages ("Uploading 10%", "Uploading 20%"), Telegram says: *"Whoa, you are sending too many API requests per second!"*
   - Telegram blocks the bot's API access entirely and throws a `FloodWaitError` telling it to wait **400 seconds**.

3. **The Custom Queue Kicks In (Mid-Processing):**
   - One of the active downloads [_safe_send(status.edit, "50%...")] hits the error. 
   - [_safe_send()] catches the error, sets the global cooldown (`_flood_until = now + 400s`), and goes to sleep for 400 seconds.
   - Any other active downloads trying to edit their text will also hit the error, update the cooldown, and sleep in place. **(Downloads don't cancel, they just pause their Telegram progress updates!)**

4. **New Users Arrive (The Queue at Work):**
   - With 150 seconds still left on the cooldown block, another user (User #51) pastes a new TeraBox link.
   - Instead of trying to process it, [_process_terabox()] checks [_flood_remaining()] and sees `150s` left.
   - The bot immediately shoves User #51's link into the `_flood_queue` and manages to send *one* last message (rate limits sometimes allow single critical replies):
     > *"⏳ Bot overloaded! Your request for [link] has been queued and will be processed automatically in ~150s."*

5. **The Cooldown Expires:**
   - 400 seconds finally pass. Telegram unblocks the bot.
   - The original 20 active downloads wake up from their sleep inside [_safe_send()], successfully update their status (`status.edit("80%...")`), and finish normally, sending the videos.
   
6. **The Background Worker Drains the Queue:**
   - The background task [_queue_worker()] wakes up and checks the `_flood_queue`.
   - It sees User #51's link sitting there.
   - It pulls it out, waits another 2 seconds (just to be gentle on Telegram's API so we don't instantly get blocked again), and then pushes it through the normal pipeline (`Checking cache... → Downloading... → Delivery`).
   - The user gets their video automatically without having had to type `/retry` or paste the URL a second time.


  ---

  BEFORE:
  Phase 4: bot.send_file(filepath) → reads disk + uploads bytes to Telegram
  Phase 5 fallback: bot.send_file(filepath) → reads disk AGAIN + uploads bytes AGAIN

  AFTER:
  Phase 4: _pre_upload_file(filepath) → reads disk once → InputFile handle
           _upload_to_storage(handle) → sends handle (no disk read)
  Phase 5 fallback: bot.send_file(handle) → reuses handle (no disk read, no re-upload)


  ---

  
  