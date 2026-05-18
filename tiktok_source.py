import asyncio
import logging
from dataclasses import dataclass

from yt_dlp import YoutubeDL


logger = logging.getLogger("tiktok-source")


@dataclass
class TikTokStats:
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    share_count: int | None


class TikTokStatsError(Exception):
    pass


def _to_int(value) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except Exception:
        return None


def _fetch_tiktok_stats_sync(video_url: str) -> TikTokStats:
    """
    Uses yt-dlp, same general method as the Bloom bot.

    No TikTok auth token is required.
    This depends on yt-dlp being able to read the public TikTok page.
    If TikTok blocks/changes the page, update yt-dlp first.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
    }

    logger.info("Fetching TikTok stats: %s", video_url)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as exc:
        raise TikTokStatsError(f"yt-dlp failed: {exc}") from exc

    if not info:
        raise TikTokStatsError("No info returned by yt-dlp")

    view_count = _to_int(info.get("view_count"))
    like_count = _to_int(info.get("like_count"))
    comment_count = _to_int(info.get("comment_count"))

    # TikTok / yt-dlp can expose shares under different names depending on extractor version.
    share_count = (
        _to_int(info.get("share_count"))
        or _to_int(info.get("repost_count"))
        or _to_int(info.get("forward_count"))
    )

    if view_count is None:
        # This is the one metric the tracker needs for 1M alerts.
        raise TikTokStatsError("view_count missing from yt-dlp result")

    return TikTokStats(
        view_count=view_count,
        like_count=like_count,
        comment_count=comment_count,
        share_count=share_count,
    )


async def fetch_tiktok_stats(video_url: str) -> TikTokStats:
    return await asyncio.to_thread(_fetch_tiktok_stats_sync, video_url)
