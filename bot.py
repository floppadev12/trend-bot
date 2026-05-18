import os
import re
import math
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tiktok_1m_tracker")

# -----------------------------
# Config
# -----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
GUILD_ID = os.getenv("GUILD_ID")  # optional, faster slash-command sync during testing

DEFAULT_CAPACITY = int(os.getenv("DEFAULT_CAPACITY", "100"))
VIEW_THRESHOLD = int(os.getenv("VIEW_THRESHOLD", "1000000"))
BERLIN_TZ = ZoneInfo("Europe/Berlin")

TIKTOK_PROVIDER = os.getenv("TIKTOK_PROVIDER", "official").lower()
TIKTOK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN", "")
CUSTOM_STATS_ENDPOINT = os.getenv("CUSTOM_STATS_ENDPOINT", "")
CUSTOM_STATS_API_KEY = os.getenv("CUSTOM_STATS_API_KEY", "")

TIKTOK_URL_RE = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@(?P<creator>[^/\s]+)/video/(?P<video_id>\d+)",
    re.IGNORECASE,
)

# -----------------------------
# Helper functions
# -----------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def berlin_today_bounds_utc() -> Tuple[datetime, datetime]:
    now_berlin = datetime.now(BERLIN_TZ)
    start_berlin = now_berlin.replace(hour=0, minute=0, second=0, microsecond=0)
    end_berlin = start_berlin + timedelta(days=1)
    return start_berlin.astimezone(timezone.utc), end_berlin.astimezone(timezone.utc)


def format_berlin_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")


def truncate_description(text: Optional[str], max_len: int = 80) -> str:
    text = (text or "No description").replace("\n", " ").strip()
    if not text:
        return "No description"
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def usage_bar(active: int, capacity: int) -> str:
    if capacity <= 0:
        return "⬛" * 10
    filled = math.ceil((active / capacity) * 10) if active > 0 else 0
    filled = max(0, min(10, filled))
    return "🟩" * filled + "⬛" * (10 - filled)


def extract_video_from_text(text: str) -> Optional[Tuple[str, str, str]]:
    match = TIKTOK_URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0)
    creator = match.group("creator")
    video_id = match.group("video_id")
    return url, creator, video_id


def extract_from_bloom_message(message: discord.Message) -> Optional[dict]:
    """Extract TikTok hit data from Bloom embed/text."""
    combined_text = message.content or ""
    description = None
    matched_keyword = None
    posted_at_raw = None

    for embed in message.embeds:
        if embed.title:
            combined_text += "\n" + embed.title
        if embed.description:
            combined_text += "\n" + embed.description
        for field in embed.fields:
            name = (field.name or "").lower()
            value = field.value or ""
            combined_text += "\n" + value

            if "description" in name:
                description = value.strip().strip("`")
            elif "keyword" in name:
                matched_keyword = value.strip().strip("`")
            elif "date" in name or "posted" in name:
                posted_at_raw = value.strip()

    extracted = extract_video_from_text(combined_text)
    if not extracted:
        return None

    video_url, creator, video_id = extracted
    return {
        "video_url": video_url,
        "creator_username": creator,
        "video_id": video_id,
        "description": description or "No description",
        "matched_keyword": matched_keyword or "challenge",
        "posted_at_raw": posted_at_raw,
    }

# -----------------------------
# TikTok stats provider
# -----------------------------
@dataclass
class TikTokStats:
    view_count: int
    like_count: int = 0
    comment_count: int = 0
    share_count: int = 0


class TikTokStatsClient:
    """
    Provider modes:
    - official: Uses TikTok /v2/video/query/. Important: official docs say this endpoint is for videos
      belonging to the authorized user, so it may not work for arbitrary public Bloom links.
    - custom_http: Sends video URLs to your own TikTok tracker API.
    - mock: For local testing only.
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def fetch_many(self, videos: List[asyncpg.Record]) -> Dict[str, TikTokStats]:
        if TIKTOK_PROVIDER == "official":
            return await self._fetch_official(videos)
        if TIKTOK_PROVIDER == "custom_http":
            return await self._fetch_custom_http(videos)
        if TIKTOK_PROVIDER == "mock":
            return await self._fetch_mock(videos)
        raise RuntimeError(f"Unknown TIKTOK_PROVIDER={TIKTOK_PROVIDER}")

    async def _fetch_official(self, videos: List[asyncpg.Record]) -> Dict[str, TikTokStats]:
        if not TIKTOK_ACCESS_TOKEN:
            raise RuntimeError("TIKTOK_ACCESS_TOKEN is missing")

        results: Dict[str, TikTokStats] = {}
        fields = "id,share_url,video_description,like_count,comment_count,share_count,view_count"
        endpoint = f"https://open.tiktokapis.com/v2/video/query/?fields={fields}"
        headers = {
            "Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }

        # TikTok official endpoint accepts up to 20 video IDs per request.
        for i in range(0, len(videos), 20):
            chunk = videos[i : i + 20]
            body = {"filters": {"video_ids": [str(v["video_id"]) for v in chunk]}}
            async with self.session.post(endpoint, headers=headers, json=body, timeout=30) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    log.warning("TikTok official API error %s: %s", resp.status, data)
                    continue
                for item in data.get("data", {}).get("videos", []):
                    vid = str(item.get("id"))
                    results[vid] = TikTokStats(
                        view_count=int(item.get("view_count") or 0),
                        like_count=int(item.get("like_count") or 0),
                        comment_count=int(item.get("comment_count") or 0),
                        share_count=int(item.get("share_count") or 0),
                    )
        return results

    async def _fetch_custom_http(self, videos: List[asyncpg.Record]) -> Dict[str, TikTokStats]:
        """
        Expected custom endpoint request:
        POST {CUSTOM_STATS_ENDPOINT}
        Headers: Authorization: Bearer {CUSTOM_STATS_API_KEY}  # optional
        Body: {"videos": [{"video_id": "...", "video_url": "..."}]}

        Expected response:
        {
          "videos": [
            {"video_id":"123", "view_count":1000000, "like_count":1, "comment_count":2, "share_count":3}
          ]
        }
        """
        if not CUSTOM_STATS_ENDPOINT:
            raise RuntimeError("CUSTOM_STATS_ENDPOINT is missing")
        headers = {"Content-Type": "application/json"}
        if CUSTOM_STATS_API_KEY:
            headers["Authorization"] = f"Bearer {CUSTOM_STATS_API_KEY}"
        body = {
            "videos": [
                {"video_id": str(v["video_id"]), "video_url": v["video_url"]}
                for v in videos
            ]
        }
        async with self.session.post(CUSTOM_STATS_ENDPOINT, headers=headers, json=body, timeout=60) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"Custom stats API error {resp.status}: {data}")
        results = {}
        for item in data.get("videos", []):
            vid = str(item.get("video_id"))
            results[vid] = TikTokStats(
                view_count=int(item.get("view_count") or 0),
                like_count=int(item.get("like_count") or 0),
                comment_count=int(item.get("comment_count") or 0),
                share_count=int(item.get("share_count") or 0),
            )
        return results

    async def _fetch_mock(self, videos: List[asyncpg.Record]) -> Dict[str, TikTokStats]:
        # For testing: each check adds 250k views until it hits 1M.
        results = {}
        for v in videos:
            current = int(v.get("last_view_count") or 0)
            results[str(v["video_id"])] = TikTokStats(view_count=current + 250_000)
        await asyncio.sleep(0.2)
        return results

# -----------------------------
# Database
# -----------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bot_settings (
    guild_id BIGINT PRIMARY KEY,
    source_channel_id BIGINT,
    hit_channel_id BIGINT,
    daily_report_channel_id BIGINT,
    bloom_bot_id BIGINT,
    capacity INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tracked_videos (
    video_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    video_url TEXT NOT NULL,
    creator_username TEXT,
    matched_keyword TEXT DEFAULT 'challenge',
    description TEXT,
    posted_at_raw TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'active',
    first_hit_1m_at TIMESTAMPTZ,
    view_count_at_hit BIGINT,
    last_checked_at TIMESTAMPTZ,
    last_view_count BIGINT DEFAULT 0,
    last_like_count BIGINT DEFAULT 0,
    last_comment_count BIGINT DEFAULT 0,
    last_share_count BIGINT DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS video_snapshots (
    id BIGSERIAL PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES tracked_videos(video_id) ON DELETE CASCADE,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    view_count BIGINT NOT NULL DEFAULT 0,
    like_count BIGINT NOT NULL DEFAULT 0,
    comment_count BIGINT NOT NULL DEFAULT 0,
    share_count BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tracked_videos_guild_status ON tracked_videos(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_tracked_videos_hit_time ON tracked_videos(guild_id, first_hit_1m_at);
"""

class TrackerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        super().__init__(command_prefix="!", intents=intents)
        self.pool: Optional[asyncpg.Pool] = None
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.stats_client: Optional[TikTokStatsClient] = None
        self.last_daily_report_date: Dict[int, str] = {}

    async def setup_hook(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is missing")
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        self.http_session = aiohttp.ClientSession()
        self.stats_client = TikTokStatsClient(self.http_session)

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced slash commands to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced global slash commands")

        self.hourly_check_loop.start()
        self.daily_report_loop.start()

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        if self.pool:
            await self.pool.close()
        await super().close()

    async def ensure_settings(self, guild_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bot_settings (guild_id, capacity)
                VALUES ($1, $2)
                ON CONFLICT (guild_id) DO NOTHING
                """,
                guild_id,
                DEFAULT_CAPACITY,
            )

    async def get_settings(self, guild_id: int) -> asyncpg.Record:
        await self.ensure_settings(guild_id)
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM bot_settings WHERE guild_id=$1", guild_id)

    async def active_count(self, guild_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM tracked_videos WHERE guild_id=$1 AND status='active'",
                guild_id,
            )

    async def eligible_archive_count(self, guild_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM tracked_videos
                WHERE guild_id=$1
                  AND status='hit_1m'
                  AND first_hit_1m_at <= NOW() - INTERVAL '30 days'
                """,
                guild_id,
            )

    async def send_capacity_full(self, guild: discord.Guild, settings: asyncpg.Record):
        channel_id = settings.get("hit_channel_id") or settings.get("daily_report_channel_id") or settings.get("source_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return
        eligible = await self.eligible_archive_count(guild.id)
        text = (
            "⚠️ **Active video usage is full**\n\n"
            f"{settings['capacity']}/{settings['capacity']}\n"
            f"{usage_bar(settings['capacity'], settings['capacity'])}\n\n"
            "Status: Full — new videos are paused.\n\n"
        )
        if eligible > 0:
            text += f"Can I archive **{eligible}** videos that hit 1M more than 1 month ago?"
            await channel.send(text, view=ArchiveConfirmView(self, guild.id))
        else:
            text += "No videos are eligible for archive yet."
            await channel.send(text)

    @tasks.loop(hours=1)
    async def hourly_check_loop(self):
        await self.wait_until_ready()
        log.info("Starting hourly active video check")
        async with self.pool.acquire() as conn:
            guild_ids = await conn.fetch("SELECT guild_id FROM bot_settings")
        for row in guild_ids:
            await self.check_guild_active_videos(int(row["guild_id"]))

    @hourly_check_loop.before_loop
    async def before_hourly(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def daily_report_loop(self):
        await self.wait_until_ready()
        now_berlin = datetime.now(BERLIN_TZ)
        if not (now_berlin.hour == 20 and now_berlin.minute == 0):
            return
        report_date = now_berlin.date().isoformat()
        async with self.pool.acquire() as conn:
            settings_rows = await conn.fetch("SELECT * FROM bot_settings WHERE daily_report_channel_id IS NOT NULL")
        for settings in settings_rows:
            guild_id = int(settings["guild_id"])
            if self.last_daily_report_date.get(guild_id) == report_date:
                continue
            await self.send_daily_report(guild_id, settings)
            self.last_daily_report_date[guild_id] = report_date

    async def check_guild_active_videos(self, guild_id: int) -> int:
        settings = await self.get_settings(guild_id)
        async with self.pool.acquire() as conn:
            videos = await conn.fetch(
                """
                SELECT * FROM tracked_videos
                WHERE guild_id=$1 AND status='active'
                ORDER BY first_seen_at ASC
                """,
                guild_id,
            )
        if not videos:
            return 0

        try:
            stats_by_id = await self.stats_client.fetch_many(videos)
        except Exception as e:
            log.exception("Stats fetch failed for guild %s: %s", guild_id, e)
            return 0

        hits = []
        async with self.pool.acquire() as conn:
            for video in videos:
                vid = str(video["video_id"])
                stats = stats_by_id.get(vid)
                if not stats:
                    continue
                await conn.execute(
                    """
                    INSERT INTO video_snapshots
                    (video_id, view_count, like_count, comment_count, share_count)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    vid,
                    stats.view_count,
                    stats.like_count,
                    stats.comment_count,
                    stats.share_count,
                )
                if stats.view_count >= VIEW_THRESHOLD:
                    updated = await conn.fetchrow(
                        """
                        UPDATE tracked_videos
                        SET status='hit_1m',
                            first_hit_1m_at=NOW(),
                            view_count_at_hit=$2,
                            last_checked_at=NOW(),
                            last_view_count=$2,
                            last_like_count=$3,
                            last_comment_count=$4,
                            last_share_count=$5,
                            updated_at=NOW()
                        WHERE video_id=$1 AND status='active'
                        RETURNING *
                        """,
                        vid,
                        stats.view_count,
                        stats.like_count,
                        stats.comment_count,
                        stats.share_count,
                    )
                    if updated:
                        hits.append(updated)
                else:
                    await conn.execute(
                        """
                        UPDATE tracked_videos
                        SET last_checked_at=NOW(),
                            last_view_count=$2,
                            last_like_count=$3,
                            last_comment_count=$4,
                            last_share_count=$5,
                            updated_at=NOW()
                        WHERE video_id=$1
                        """,
                        vid,
                        stats.view_count,
                        stats.like_count,
                        stats.comment_count,
                        stats.share_count,
                    )

        for hit in hits:
            await self.send_1m_hit_alert(guild_id, settings, hit)
        return len(hits)

    async def send_1m_hit_alert(self, guild_id: int, settings: asyncpg.Record, video: asyncpg.Record):
        channel_id = settings.get("hit_channel_id")
        if not channel_id:
            return
        guild = self.get_guild(guild_id)
        channel = guild.get_channel(int(channel_id)) if guild else self.get_channel(int(channel_id))
        if not channel:
            return
        desc = truncate_description(video.get("description"), 80)
        creator = video.get("creator_username") or "unknown"
        url = video.get("video_url")
        msg = f'🎯 **1M Hit** | **@{creator}** | "{desc}" | [VIEW HERE]({url})'
        await channel.send(msg)

    async def send_daily_report(self, guild_id: int, settings: asyncpg.Record):
        channel_id = settings.get("daily_report_channel_id")
        if not channel_id:
            return
        guild = self.get_guild(guild_id)
        channel = guild.get_channel(int(channel_id)) if guild else self.get_channel(int(channel_id))
        if not channel:
            return

        start_utc, end_utc = berlin_today_bounds_utc()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM tracked_videos
                WHERE guild_id=$1
                  AND status IN ('hit_1m', 'archived')
                  AND first_hit_1m_at >= $2
                  AND first_hit_1m_at < $3
                ORDER BY first_hit_1m_at ASC
                """,
                guild_id,
                start_utc,
                end_utc,
            )
        today_str = datetime.now(BERLIN_TZ).strftime("%d.%m.%Y")
        if not rows:
            await channel.send(f"📅 **Daily 1M Report — {today_str}**\n\nNo videos hit 1M today.")
            return
        lines = [f"📅 **Daily 1M Report — {today_str}**", "", f"Videos that hit 1M today: **{len(rows)}**", ""]
        for idx, row in enumerate(rows, start=1):
            creator = row.get("creator_username") or "unknown"
            desc = truncate_description(row.get("description"), 60)
            views = int(row.get("view_count_at_hit") or row.get("last_view_count") or 0)
            lines.append(f'{idx}. **@{creator}** | "{desc}" | **{views:,} views** | [VIEW HERE]({row["video_url"]})')
        await channel.send("\n".join(lines))

bot = TrackerBot()

# -----------------------------
# Discord UI
# -----------------------------
class ArchiveConfirmView(discord.ui.View):
    def __init__(self, bot_instance: TrackerBot, guild_id: int):
        super().__init__(timeout=120)
        self.bot_instance = bot_instance
        self.guild_id = guild_id

    @discord.ui.button(label="Archive eligible videos", style=discord.ButtonStyle.danger)
    async def archive_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only admins can archive videos.", ephemeral=True)
            return
        async with self.bot_instance.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                WITH archived AS (
                    UPDATE tracked_videos
                    SET status='archived', updated_at=NOW()
                    WHERE guild_id=$1
                      AND status='hit_1m'
                      AND first_hit_1m_at <= NOW() - INTERVAL '30 days'
                    RETURNING 1
                )
                SELECT COUNT(*) FROM archived
                """,
                self.guild_id,
            )
        await interaction.response.edit_message(content=f"✅ Archived **{count}** eligible videos.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)

# -----------------------------
# Events
# -----------------------------
@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)


@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.id == bot.user.id:
        return

    settings = await bot.get_settings(message.guild.id)
    source_channel_id = settings.get("source_channel_id")
    bloom_bot_id = settings.get("bloom_bot_id")

    if not source_channel_id or not bloom_bot_id:
        return
    if message.channel.id != int(source_channel_id):
        return
    if message.author.id != int(bloom_bot_id):
        return

    data = extract_from_bloom_message(message)
    if not data:
        return

    active = await bot.active_count(message.guild.id)
    capacity = int(settings["capacity"])
    if active >= capacity:
        try:
            await message.add_reaction("🛑")
        except discord.HTTPException:
            pass
        await bot.send_capacity_full(message.guild, settings)
        return

    async with bot.pool.acquire() as conn:
        inserted = await conn.fetchrow(
            """
            INSERT INTO tracked_videos
            (video_id, guild_id, video_url, creator_username, matched_keyword, description, posted_at_raw, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'active')
            ON CONFLICT (video_id) DO NOTHING
            RETURNING *
            """,
            data["video_id"],
            message.guild.id,
            data["video_url"],
            data["creator_username"],
            data["matched_keyword"],
            data["description"],
            data["posted_at_raw"],
        )

    try:
        await message.add_reaction("✅" if inserted else "🔁")
    except discord.HTTPException:
        pass

# -----------------------------
# Slash commands
# -----------------------------
def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.manage_guild
    return app_commands.check(predicate)


@bot.tree.command(name="setup_source_channel", description="Set the channel where Bloom posts TikTok hits.")
@admin_only()
async def setup_source_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.ensure_settings(interaction.guild_id)
    async with bot.pool.acquire() as conn:
        await conn.execute("UPDATE bot_settings SET source_channel_id=$1, updated_at=NOW() WHERE guild_id=$2", channel.id, interaction.guild_id)
    await interaction.response.send_message(f"✅ Source channel set to {channel.mention}", ephemeral=True)


@bot.tree.command(name="setup_hit_channel", description="Set the channel for 🎯 1M hit alerts.")
@admin_only()
async def setup_hit_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.ensure_settings(interaction.guild_id)
    async with bot.pool.acquire() as conn:
        await conn.execute("UPDATE bot_settings SET hit_channel_id=$1, updated_at=NOW() WHERE guild_id=$2", channel.id, interaction.guild_id)
    await interaction.response.send_message(f"✅ 1M hit alert channel set to {channel.mention}", ephemeral=True)


@bot.tree.command(name="setup_daily_report_channel", description="Set the channel for daily 20:00 Germany-time reports.")
@admin_only()
async def setup_daily_report_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.ensure_settings(interaction.guild_id)
    async with bot.pool.acquire() as conn:
        await conn.execute("UPDATE bot_settings SET daily_report_channel_id=$1, updated_at=NOW() WHERE guild_id=$2", channel.id, interaction.guild_id)
    await interaction.response.send_message(f"✅ Daily report channel set to {channel.mention}", ephemeral=True)


@bot.tree.command(name="setup_bloom_bot", description="Set Bloom's Discord bot user ID.")
@admin_only()
async def setup_bloom_bot(interaction: discord.Interaction, bloom_bot_id: str):
    try:
        bot_id_int = int(bloom_bot_id)
    except ValueError:
        await interaction.response.send_message("❌ Please provide a numeric bot user ID.", ephemeral=True)
        return
    await bot.ensure_settings(interaction.guild_id)
    async with bot.pool.acquire() as conn:
        await conn.execute("UPDATE bot_settings SET bloom_bot_id=$1, updated_at=NOW() WHERE guild_id=$2", bot_id_int, interaction.guild_id)
    await interaction.response.send_message(f"✅ Bloom bot ID set to `{bot_id_int}`", ephemeral=True)


@bot.tree.command(name="usage", description="Show active video usage capacity.")
async def usage(interaction: discord.Interaction):
    settings = await bot.get_settings(interaction.guild_id)
    active = await bot.active_count(interaction.guild_id)
    capacity = int(settings["capacity"])
    full = active >= capacity
    title = "⚠️ Active video usage" if full else "📦 Active video usage"
    status = "Full — new videos are paused." if full else "Active"
    content = f"{title}\n\n{active}/{capacity}\n{usage_bar(active, capacity)}\n\nStatus: {status}\nCapacity: {capacity}"
    if full:
        eligible = await bot.eligible_archive_count(interaction.guild_id)
        if eligible > 0:
            content += f"\n\nCan I archive **{eligible}** videos that hit 1M more than 1 month ago?"
            await interaction.response.send_message(content, view=ArchiveConfirmView(bot, interaction.guild_id))
        else:
            content += "\n\nNo videos are eligible for archive yet."
            await interaction.response.send_message(content)
    else:
        await interaction.response.send_message(content)


@bot.tree.command(name="set_capacity", description="Set active video capacity.")
@admin_only()
async def set_capacity(interaction: discord.Interaction, capacity: app_commands.Range[int, 1, 10000]):
    await bot.ensure_settings(interaction.guild_id)
    async with bot.pool.acquire() as conn:
        await conn.execute("UPDATE bot_settings SET capacity=$1, updated_at=NOW() WHERE guild_id=$2", capacity, interaction.guild_id)
    await interaction.response.send_message(f"✅ Capacity set to {capacity}", ephemeral=True)


@bot.tree.command(name="check_now", description="Manually check active videos now.")
@admin_only()
async def check_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    hits = await bot.check_guild_active_videos(interaction.guild_id)
    await interaction.followup.send(f"✅ Check complete. New 1M hits: **{hits}**", ephemeral=True)


@bot.tree.command(name="tracked", description="Show recent active tracked videos.")
async def tracked(interaction: discord.Interaction):
    async with bot.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM tracked_videos
            WHERE guild_id=$1 AND status='active'
            ORDER BY first_seen_at DESC
            LIMIT 10
            """,
            interaction.guild_id,
        )
    if not rows:
        await interaction.response.send_message("No active videos are being tracked.", ephemeral=True)
        return
    lines = ["📌 **Recent active tracked videos**", ""]
    for row in rows:
        desc = truncate_description(row.get("description"), 50)
        views = int(row.get("last_view_count") or 0)
        lines.append(f'• **@{row.get("creator_username") or "unknown"}** | "{desc}" | {views:,} views | [VIEW HERE]({row["video_url"]})')
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="hit_videos", description="Show recent videos that hit 1M.")
async def hit_videos(interaction: discord.Interaction):
    async with bot.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM tracked_videos
            WHERE guild_id=$1 AND status='hit_1m'
            ORDER BY first_hit_1m_at DESC
            LIMIT 10
            """,
            interaction.guild_id,
        )
    if not rows:
        await interaction.response.send_message("No videos have hit 1M yet.", ephemeral=True)
        return
    lines = ["🎯 **Recent 1M hits**", ""]
    for row in rows:
        desc = truncate_description(row.get("description"), 50)
        views = int(row.get("view_count_at_hit") or row.get("last_view_count") or 0)
        hit_time = format_berlin_dt(row["first_hit_1m_at"])
        lines.append(f'• **@{row.get("creator_username") or "unknown"}** | "{desc}" | {views:,} views | {hit_time} | [VIEW HERE]({row["video_url"]})')
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="archive_old_hits", description="Ask to archive videos that hit 1M more than 1 month ago.")
@admin_only()
async def archive_old_hits(interaction: discord.Interaction):
    eligible = await bot.eligible_archive_count(interaction.guild_id)
    if eligible <= 0:
        await interaction.response.send_message("No videos are eligible for archive yet.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"Can I archive **{eligible}** videos that hit 1M more than 1 month ago?",
        view=ArchiveConfirmView(bot, interaction.guild_id),
        ephemeral=True,
    )


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing")
    bot.run(DISCORD_TOKEN)
