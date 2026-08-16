"""Generate Telegram-compatible video thumbnails with bounded FFmpeg usage."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

import requests
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

log = logging.getLogger(__name__)

_FFMPEG_SEMAPHORE = asyncio.Semaphore(5)
_THUMB_MAX_BYTES = 20_000
_DOWNLOAD_MAX_BYTES = 5 * 1024 * 1024


@dataclass
class VideoPreview:
    thumbnail_path: str | None
    work_dir: str
    duration: int
    width: int
    height: int

    def attributes(self, filename: str) -> list:
        return [
            DocumentAttributeFilename(filename),
            DocumentAttributeVideo(
                duration=max(0, self.duration),
                w=max(1, self.width),
                h=max(1, self.height),
                supports_streaming=True,
            ),
        ]

    def cleanup(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)


def _probe_video(video_path: str) -> tuple[float, int, int]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        data = json.loads(result.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        duration = float((data.get("format") or {}).get("duration") or 0)
        return duration, int(stream.get("width") or 1), int(stream.get("height") or 1)
    except Exception as exc:
        log.warning("Could not probe video metadata for %s: %s", video_path, exc)
        return 0.0, 1, 1


def _download_image(url: str, destination: str) -> bool:
    try:
        with requests.get(url, stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            size = 0
            with open(destination, "wb") as output:
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > _DOWNLOAD_MAX_BYTES:
                        raise ValueError("provider thumbnail exceeded 5 MiB")
                    output.write(chunk)
        return size > 0
    except Exception as exc:
        log.warning("Could not download provider thumbnail: %s", exc)
        return False


def _encode_jpeg(input_path: str, output_path: str) -> bool:
    for quality in (8, 12, 18, 24):
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", input_path,
                    "-frames:v", "1",
                    "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                    "-q:v", str(quality),
                    output_path,
                ],
                capture_output=True,
                timeout=30,
                check=True,
            )
            if 0 < os.path.getsize(output_path) <= _THUMB_MAX_BYTES:
                return True
        except Exception:
            continue
    return False


def _extract_random_frame(
    video_path: str,
    output_path: str,
    duration: float,
) -> bool:
    if duration > 2:
        timestamp = random.uniform(duration * 0.1, duration * 0.9)
    else:
        timestamp = 0

    for seek in (timestamp, 0):
        for quality in (8, 12, 18, 24):
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-ss", f"{seek:.3f}",
                        "-i", video_path,
                        "-map", "0:v:0",
                        "-frames:v", "1",
                        "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                        "-q:v", str(quality),
                        output_path,
                    ],
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                if 0 < os.path.getsize(output_path) <= _THUMB_MAX_BYTES:
                    return True
            except Exception:
                continue
    return False


def _prepare_preview(video_path: str, thumbnail_url: str | None) -> VideoPreview:
    os.makedirs("storage", exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="preview_", dir="storage")
    thumbnail_path = os.path.join(work_dir, "thumbnail.jpg")
    downloaded_path = os.path.join(work_dir, "provider_image")
    duration, width, height = _probe_video(video_path)

    generated = False
    if thumbnail_url and _download_image(thumbnail_url, downloaded_path):
        generated = _encode_jpeg(downloaded_path, thumbnail_path)
    if not generated:
        generated = _extract_random_frame(video_path, thumbnail_path, duration)

    if not generated:
        thumbnail_path = None
    return VideoPreview(
        thumbnail_path=thumbnail_path,
        work_dir=work_dir,
        duration=int(duration),
        width=width,
        height=height,
    )


async def prepare_video_preview(
    video_path: str,
    thumbnail_url: str | None = None,
) -> VideoPreview:
    """Queue FFmpeg preview work; at most five previews run concurrently."""
    async with _FFMPEG_SEMAPHORE:
        return await asyncio.to_thread(_prepare_preview, video_path, thumbnail_url)
