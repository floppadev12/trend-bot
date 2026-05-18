import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from yt_dlp import YoutubeDL


logger = logging.getLogger("tiktok-source")


@dataclass
class TikTokVideo:
    video_id: str
    creator_username: str
    description: str
    video_url: str
    posted_at: datetime | None


def clean_username(username: str) -> str:
    username = username.strip()

    if username.startswith("@"):
        username = username[1:]

    return username.lower()


def parse_timestamp(value) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except Exception:
        return None


def extract_video_id(entry: dict) -> str | None:
    for key in ["id", "display_id"]:
        value = entry.get(key)
        if value:
            return str(value)

    webpage_url = entry.get("webpage_url") or entry.get("url")
    if webpage_url and "/video/" in webpage_url:
        return webpage_url.rstrip("/").split("/video/")[-1].split("?")[0]

    return None


def extract_description(entry: dict) -> str:
    candidates = [
        entry.get("description"),
        entry.get("title"),
        entry.get("fulltitle"),
    ]

    for value in candidates:
        if value:
            return str(value).strip()

    return ""


def extract_video_url(username: str, entry: dict, video_id: str) -> str:
    webpage_url = entry.get("webpage_url")

    if webpage_url:
        return str(webpage_url)

    return f"https://www.tiktok.com/@{username}/video/{video_id}"


def fetch_latest_videos_sync(username: str, max_videos: int = 5) -> list[TikTokVideo]:
    username = clean_username(username)
    profile_url = f"https://www.tiktok.com/@{username}"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "playlistend": max_videos,
        "ignoreerrors": True,
        "noplaylist": False,
        "socket_timeout": 25,
        "retries": 2,
    }

    videos: list[TikTokVideo] = []

    logger.info("Fetching TikTok profile: %s", profile_url)

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(profile_url, download=False)

    if not info:
        logger.warning("No TikTok info returned for @%s", username)
        return []

    entries = info.get("entries") or []

    for entry in entries:
        if not entry:
            continue

        video_id = extract_video_id(entry)
        if not video_id:
            continue

        description = extract_description(entry)
        video_url = extract_video_url(username, entry, video_id)
        posted_at = parse_timestamp(entry.get("timestamp"))

        videos.append(
            TikTokVideo(
                video_id=video_id,
                creator_username=username,
                description=description,
                video_url=video_url,
                posted_at=posted_at,
            )
        )

    logger.info("Fetched %s videos for @%s", len(videos), username)

    return videos


async def get_latest_videos(username: str) -> list[TikTokVideo]:
    return await asyncio.to_thread(fetch_latest_videos_sync, username, 5)
