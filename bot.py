import asyncio
import logging
import math
import os
import re
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo

import asyncpg
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from tiktok_source import fetch_tiktok_stats, TikTokStatsError


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", "1"))
DEFAULT_CAPACITY = int(os.getenv("DEFAULT_CAPACITY", "100"))
HIT_THRESHOLD = int(os.getenv("HIT_THRESHOLD", "1000000"))

FIXED_KEYWORD = "challenge"
BERLIN_TZ = ZoneInfo("Europe/Berlin")

TIKTOK_RE = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@[^/\s]+/video/\d+|https?://(?:vm|vt)\.tiktok\.com/[^\s)]+",
    re.IGNORECASE,
)
VIDEO_ID_RE = re.compile(r"/video/(\d+)")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("tiktok-1m-tracker")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def truncate_text(value: str | None, limit: int = 80) -> str:
    value = (value or "No description").replace("\n", " ").replace("\r", " ").strip()
    value = " ".join(value.split())

    if not value:
        value = "No description"

    if len(value) <= limit:
        return value

    return value[: max(0, limit - 3)].rstrip() + "..."


def format_int(value: int | None) -> str:
    if value is None:
        return "Unknown"
    return f"{int(value):,}"


def usage_bar(active: int, capacity: int) -> str:
    if capacity <= 0:
        capacity = 100

    ratio = max(0, min(active / capacity, 1))
    green_blocks = math.ceil(ratio * 10) if active > 0 else 0
    green_blocks = max(0, min(green_blocks, 10))
    black_blocks = 10 - green_blocks

    return "🟩" * green_blocks + "⬛" * black_blocks


def extract_video_id(video_url: str) -> str:
    match = VIDEO_ID_RE.search(video_url)
    if match:
        return match.group(1)

    # Short TikTok links do not expose the real video ID without resolving.
    # We still store a stable ID from the URL. yt-dlp can resolve it during stat checks.
    return video_url.rstrip("/").split("/")[-1].split("?")[0]


def parse_bloom_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    value = value.strip()

    # Bloom format from your current bot:
    # 2026-05-18 19:37:58 UTC
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


class ArchiveOldHitsView(discord.ui.View):
    def __init__(self, bot: "TikTokTrackerBot", guild_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.button(label="Archive eligible videos", style=discord.ButtonStyle.danger)
    async def archive_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("Wrong server.", ephemeral=True)
            return

        archived_count = await self.bot.archive_eligible_hits(str(self.guild_id))

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content=f"✅ Archived `{archived_count}` eligible 1M-hit videos.",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content="Cancelled. No videos were archived.",
            view=self,
        )


class TikTokTrackerBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # Embed parsing usually works without reading message text, but enabling this
        # makes the bot more reliable if Bloom ever sends plain-text links.
        intents.message_content = True

        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.db_pool: asyncpg.Pool | None = None

    async def setup_hook(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is missing.")

        self.db_pool = await asyncpg.create_pool(DATABASE_URL)
        await self.create_tables()
        self.register_commands()

        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %s", DISCORD_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally")

        self.hourly_view_checker.start()
        self.daily_reporter.start()

    async def close(self):
        if self.db_pool:
            await self.db_pool.close()
        await super().close()

    async def create_tables(self):
        assert self.db_pool is not None

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS tracker_settings (
                guild_id TEXT PRIMARY KEY,
                source_channel_id TEXT,
                hit_channel_id TEXT,
                daily_report_channel_id TEXT,
                bloom_bot_id TEXT,
                capacity INTEGER NOT NULL DEFAULT 100,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_videos (
                id SERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                video_url TEXT NOT NULL,
                creator_username TEXT,
                description TEXT,
                posted_at TIMESTAMPTZ,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                status TEXT NOT NULL DEFAULT 'active',
                matched_keyword TEXT DEFAULT 'challenge',

                first_hit_1m_at TIMESTAMPTZ,
                view_count_at_hit BIGINT,

                last_checked_at TIMESTAMPTZ,
                last_view_count BIGINT,
                last_like_count BIGINT,
                last_comment_count BIGINT,
                last_share_count BIGINT,

                archived_at TIMESTAMPTZ,

                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                UNIQUE (guild_id, video_id)
            );
            """
        )

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS video_snapshots (
                id SERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                view_count BIGINT,
                like_count BIGINT,
                comment_count BIGINT,
                share_count BIGINT
            );
            """
        )

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_report_sends (
                id SERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                report_date DATE NOT NULL,
                sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (guild_id, report_date)
            );
            """
        )

    async def ensure_settings(self, guild_id: str):
        assert self.db_pool is not None
        await self.db_pool.execute(
            """
            INSERT INTO tracker_settings (guild_id, capacity)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO NOTHING;
            """,
            guild_id,
            DEFAULT_CAPACITY,
        )

    async def get_settings(self, guild_id: str):
        assert self.db_pool is not None
        await self.ensure_settings(guild_id)
        return await self.db_pool.fetchrow(
            """
            SELECT *
            FROM tracker_settings
            WHERE guild_id = $1;
            """,
            guild_id,
        )

    def register_commands(self):
        bot = self

        @self.tree.command(
            name="setup_source_channel",
            description="Set the channel where Bloom posts challenge hits",
        )
        @app_commands.describe(channel="The channel Bloom posts TikTok challenge hits in")
        async def setup_source_channel(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
            await bot.set_channel_setting(interaction, "source_channel_id", channel, "source")

        @self.tree.command(
            name="setup_hit_channel",
            description="Set the channel where 🎯 1M hit alerts are sent",
        )
        @app_commands.describe(channel="The channel for 1M hit alerts")
        async def setup_hit_channel(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
            await bot.set_channel_setting(interaction, "hit_channel_id", channel, "1M hit alert")

        @self.tree.command(
            name="setup_daily_report_channel",
            description="Set the channel where 📅 daily reports are sent",
        )
        @app_commands.describe(channel="The channel for daily reports")
        async def setup_daily_report_channel(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
            await bot.set_channel_setting(interaction, "daily_report_channel_id", channel, "daily report")

        @self.tree.command(
            name="setup_bloom_bot",
            description="Set Bloom's Discord bot user ID",
        )
        @app_commands.describe(bot_user_id="Bloom bot user ID")
        async def setup_bloom_bot(
            interaction: discord.Interaction,
            bot_user_id: str,
        ):
            if not interaction.guild:
                await interaction.response.send_message("Use this inside a server.", ephemeral=True)
                return

            bot_user_id = bot_user_id.strip()

            if not bot_user_id.isdigit():
                await interaction.response.send_message(
                    "Bloom bot user ID must be numbers only.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None
            guild_id = str(interaction.guild.id)
            await bot.ensure_settings(guild_id)

            await bot.db_pool.execute(
                """
                UPDATE tracker_settings
                SET bloom_bot_id = $2,
                    updated_at = NOW()
                WHERE guild_id = $1;
                """,
                guild_id,
                bot_user_id,
            )

            await interaction.response.send_message(
                f"✅ Bloom bot ID set to `{bot_user_id}`.",
                ephemeral=True,
            )

        @self.tree.command(
            name="usage",
            description="Show active tracked video capacity",
        )
        async def usage(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message("Use this inside a server.", ephemeral=True)
                return

            guild_id = str(interaction.guild.id)
            settings = await bot.get_settings(guild_id)
            active_count = await bot.count_active_videos(guild_id)
            capacity = int(settings["capacity"])

            status = "Full — new videos are paused." if active_count >= capacity else "Active"

            content = (
                "📦 **Active video usage**\n\n"
                f"**{active_count}/{capacity}**\n"
                f"{usage_bar(active_count, capacity)}\n\n"
                f"Status: **{status}**\n"
                f"Capacity: **{capacity}**"
            )

            view = None
            if active_count >= capacity:
                content += "\n\nCan I archive videos that hit 1M more than 1 month ago?"
                view = ArchiveOldHitsView(bot, interaction.guild.id)

            await interaction.response.send_message(content, view=view, ephemeral=True)

        @self.tree.command(
            name="set_capacity",
            description="Set max active videos, default is 100",
        )
        @app_commands.describe(capacity="Max active videos")
        async def set_capacity(interaction: discord.Interaction, capacity: int):
            if not interaction.guild:
                await interaction.response.send_message("Use this inside a server.", ephemeral=True)
                return

            if capacity < 1 or capacity > 10000:
                await interaction.response.send_message(
                    "Capacity must be between 1 and 10000.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None
            guild_id = str(interaction.guild.id)
            await bot.ensure_settings(guild_id)

            await bot.db_pool.execute(
                """
                UPDATE tracker_settings
                SET capacity = $2,
                    updated_at = NOW()
                WHERE guild_id = $1;
                """,
                guild_id,
                capacity,
            )

            await interaction.response.send_message(
                f"✅ Capacity set to `{capacity}` active videos.",
                ephemeral=True,
            )

        @self.tree.command(
            name="check_now",
            description="Manually check all active videos for 1M views now",
        )
        async def check_now(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message("Use this inside a server.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            result = await bot.check_guild_active_videos(str(interaction.guild.id))

            await interaction.followup.send(
                (
                    "✅ Manual check finished.\n"
                    f"Checked: `{result['checked']}`\n"
                    f"Hit 1M: `{result['hit_1m']}`\n"
                    f"Errors: `{result['errors']}`"
                ),
                ephemeral=True,
            )

        @self.tree.command(
            name="tracked",
            description="Show active tracked videos",
        )
        async def tracked(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message("Use this inside a server.", ephemeral=True)
                return

            assert bot.db_pool is not None
            rows = await bot.db_pool.fetch(
                """
                SELECT creator_username, description, video_url, last_view_count, first_seen_at
                FROM tracked_videos
                WHERE guild_id = $1
                  AND status = 'active'
                ORDER BY first_seen_at DESC
                LIMIT 10;
                """,
                str(interaction.guild.id),
            )

            if not rows:
                await interaction.response.send_message("No active videos are being tracked.", ephemeral=True)
                return

            lines = ["**Latest active tracked videos:**"]
            for row in rows:
                creator = row["creator_username"] or "unknown"
                desc = truncate_text(row["description"], 50)
                views = format_int(row["last_view_count"])
                url = row["video_url"]
                lines.append(f"- **@{creator}** | `{views}` views | \"{desc}\" | [VIEW HERE]({url})")

            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(
            name="hit_videos",
            description="Show videos that already hit 1M",
        )
        async def hit_videos(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message("Use this inside a server.", ephemeral=True)
                return

            assert bot.db_pool is not None
            rows = await bot.db_pool.fetch(
                """
                SELECT creator_username, description, video_url, view_count_at_hit, first_hit_1m_at
                FROM tracked_videos
                WHERE guild_id = $1
                  AND status = 'hit_1m'
                ORDER BY first_hit_1m_at DESC
                LIMIT 10;
                """,
                str(interaction.guild.id),
            )

            if not rows:
                await interaction.response.send_message("No videos have hit 1M yet.", ephemeral=True)
                return

            lines = ["**Latest 1M hits:**"]
            for row in rows:
                creator = row["creator_username"] or "unknown"
                desc = truncate_text(row["description"], 50)
                views = format_int(row["view_count_at_hit"])
                url = row["video_url"]
                lines.append(f"- 🎯 **@{creator}** | `{views}` views | \"{desc}\" | [VIEW HERE]({url})")

            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(
            name="archive_old_hits",
            description="Ask to archive videos that hit 1M more than 1 month ago",
        )
        async def archive_old_hits(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message("Use this inside a server.", ephemeral=True)
                return

            eligible_count = await bot.count_archive_eligible_hits(str(interaction.guild.id))

            await interaction.response.send_message(
                (
                    f"Eligible 1M-hit videos older than 1 month: `{eligible_count}`\n\n"
                    "Archive them now?"
                ),
                view=ArchiveOldHitsView(bot, interaction.guild.id),
                ephemeral=True,
            )

        @self.tree.command(
            name="debug_tracker",
            description="Show tracker setup status",
        )
        async def debug_tracker(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message("Use this inside a server.", ephemeral=True)
                return

            settings = await bot.get_settings(str(interaction.guild.id))
            active_count = await bot.count_active_videos(str(interaction.guild.id))

            await interaction.response.send_message(
                (
                    f"**Source channel:** `{settings['source_channel_id']}`\n"
                    f"**Hit channel:** `{settings['hit_channel_id']}`\n"
                    f"**Daily report channel:** `{settings['daily_report_channel_id']}`\n"
                    f"**Bloom bot ID:** `{settings['bloom_bot_id']}`\n"
                    f"**Capacity:** `{settings['capacity']}`\n"
                    f"**Active videos:** `{active_count}`"
                ),
                ephemeral=True,
            )

    async def set_channel_setting(
        self,
        interaction: discord.Interaction,
        column: str,
        channel: discord.TextChannel,
        label: str,
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use this inside a server.", ephemeral=True)
            return

        allowed = {"source_channel_id", "hit_channel_id", "daily_report_channel_id"}
        if column not in allowed:
            await interaction.response.send_message("Invalid setting.", ephemeral=True)
            return

        assert self.db_pool is not None
        guild_id = str(interaction.guild.id)
        await self.ensure_settings(guild_id)

        await self.db_pool.execute(
            f"""
            UPDATE tracker_settings
            SET {column} = $2,
                updated_at = NOW()
            WHERE guild_id = $1;
            """,
            guild_id,
            str(channel.id),
        )

        await interaction.response.send_message(
            f"✅ {label.capitalize()} channel set to {channel.mention}.",
            ephemeral=True,
        )

    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        if message.author.id == self.user.id:
            return

        guild_id = str(message.guild.id)
        settings = await self.get_settings(guild_id)

        source_channel_id = settings["source_channel_id"]
        bloom_bot_id = settings["bloom_bot_id"]

        if not source_channel_id or str(message.channel.id) != str(source_channel_id):
            return

        if bloom_bot_id and str(message.author.id) != str(bloom_bot_id):
            return

        hit = self.extract_bloom_hit(message)
        if not hit:
            return

        # Bloom already filters to challenge, but keep this protection.
        if hit["matched_keyword"] and FIXED_KEYWORD not in hit["matched_keyword"].lower():
            return

        await self.handle_new_bloom_hit(message, guild_id, settings, hit)

    def extract_bloom_hit(self, message: discord.Message) -> dict | None:
        data = {
            "creator_username": None,
            "matched_keyword": None,
            "posted_at": None,
            "description": None,
            "video_url": None,
        }

        # Parse embeds from your current Bloom format:
        # Creator / Matched keyword / Date posted / Exact description / Video link
        for embed in message.embeds:
            if embed.url and "tiktok.com" in embed.url:
                data["video_url"] = embed.url

            for field in embed.fields:
                name = (field.name or "").lower().strip()
                value = (field.value or "").strip()

                if "creator" in name:
                    data["creator_username"] = value.replace("@", "").replace("`", "").strip()
                elif "matched keyword" in name:
                    data["matched_keyword"] = value.replace("`", "").strip()
                elif "date posted" in name:
                    data["posted_at"] = parse_bloom_datetime(value)
                elif "description" in name:
                    data["description"] = value.strip()
                elif "video link" in name:
                    match = TIKTOK_RE.search(value)
                    if match:
                        data["video_url"] = match.group(0)

        # Fallback if Bloom ever sends plain text.
        if not data["video_url"]:
            content = message.content or ""
            match = TIKTOK_RE.search(content)
            if match:
                data["video_url"] = match.group(0)

        if not data["video_url"]:
            return None

        if not data["creator_username"]:
            # Try to infer from full TikTok URL.
            parts = data["video_url"].split("/")
            for part in parts:
                if part.startswith("@"):
                    data["creator_username"] = part[1:]

        data["creator_username"] = (data["creator_username"] or "unknown").lower()
        data["matched_keyword"] = data["matched_keyword"] or FIXED_KEYWORD
        data["description"] = data["description"] or "No description"
        data["video_id"] = extract_video_id(data["video_url"])

        return data

    async def handle_new_bloom_hit(
        self,
        message: discord.Message,
        guild_id: str,
        settings,
        hit: dict,
    ):
        capacity = int(settings["capacity"])
        active_count = await self.count_active_videos(guild_id)

        if active_count >= capacity:
            await message.add_reaction("🛑")
            await self.send_capacity_full_warning(message.guild, settings)
            return

        inserted = await self.insert_active_video(guild_id, hit)

        if inserted:
            await message.add_reaction("✅")
            logger.info("Saved new active TikTok video %s", hit["video_id"])
        else:
            await message.add_reaction("🔁")
            logger.info("Duplicate TikTok video ignored %s", hit["video_id"])

    async def send_capacity_full_warning(self, guild: discord.Guild, settings):
        channel_id = settings["hit_channel_id"] or settings["daily_report_channel_id"] or settings["source_channel_id"]
        if not channel_id:
            return

        channel = self.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return

        content = (
            "⚠️ **Active video capacity is full**\n\n"
            f"**{settings['capacity']}/{settings['capacity']}**\n"
            f"{usage_bar(int(settings['capacity']), int(settings['capacity']))}\n\n"
            "New videos are paused.\n\n"
            "Can I archive videos that hit 1M more than 1 month ago?"
        )

        await channel.send(content, view=ArchiveOldHitsView(self, guild.id))

    async def insert_active_video(self, guild_id: str, hit: dict) -> bool:
        assert self.db_pool is not None

        result = await self.db_pool.execute(
            """
            INSERT INTO tracked_videos (
                guild_id,
                video_id,
                video_url,
                creator_username,
                description,
                posted_at,
                status,
                matched_keyword
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'active', $7)
            ON CONFLICT (guild_id, video_id)
            DO NOTHING;
            """,
            guild_id,
            hit["video_id"],
            hit["video_url"],
            hit["creator_username"],
            hit["description"],
            hit["posted_at"],
            FIXED_KEYWORD,
        )

        return int(result.split(" ")[-1]) == 1

    async def count_active_videos(self, guild_id: str) -> int:
        assert self.db_pool is not None
        value = await self.db_pool.fetchval(
            """
            SELECT COUNT(*)
            FROM tracked_videos
            WHERE guild_id = $1
              AND status = 'active';
            """,
            guild_id,
        )
        return int(value or 0)

    async def count_archive_eligible_hits(self, guild_id: str) -> int:
        assert self.db_pool is not None
        value = await self.db_pool.fetchval(
            """
            SELECT COUNT(*)
            FROM tracked_videos
            WHERE guild_id = $1
              AND status = 'hit_1m'
              AND first_hit_1m_at <= NOW() - INTERVAL '1 month';
            """,
            guild_id,
        )
        return int(value or 0)

    async def archive_eligible_hits(self, guild_id: str) -> int:
        assert self.db_pool is not None

        result = await self.db_pool.execute(
            """
            UPDATE tracked_videos
            SET status = 'archived',
                archived_at = NOW(),
                updated_at = NOW()
            WHERE guild_id = $1
              AND status = 'hit_1m'
              AND first_hit_1m_at <= NOW() - INTERVAL '1 month';
            """,
            guild_id,
        )

        return int(result.split(" ")[-1])

    @tasks.loop(hours=CHECK_INTERVAL_HOURS)
    async def hourly_view_checker(self):
        assert self.db_pool is not None

        guild_rows = await self.db_pool.fetch(
            """
            SELECT DISTINCT guild_id
            FROM tracked_videos
            WHERE status = 'active';
            """
        )

        for row in guild_rows:
            guild_id = row["guild_id"]
            try:
                result = await self.check_guild_active_videos(guild_id)
                logger.info("Hourly checked guild %s: %s", guild_id, result)
            except Exception:
                logger.exception("Failed hourly check for guild %s", guild_id)

            await asyncio.sleep(2)

    @hourly_view_checker.before_loop
    async def before_hourly_view_checker(self):
        await self.wait_until_ready()
        logger.info("Hourly 1M checker started")

    async def check_guild_active_videos(self, guild_id: str) -> dict:
        assert self.db_pool is not None

        settings = await self.get_settings(guild_id)

        rows = await self.db_pool.fetch(
            """
            SELECT video_id, video_url, creator_username, description
            FROM tracked_videos
            WHERE guild_id = $1
              AND status = 'active'
            ORDER BY first_seen_at ASC;
            """,
            guild_id,
        )

        checked = 0
        hit_1m = 0
        errors = 0

        for row in rows:
            checked += 1

            try:
                stats = await fetch_tiktok_stats(row["video_url"])
            except TikTokStatsError as exc:
                errors += 1
                logger.warning("TikTok stats unavailable for %s: %s", row["video_url"], exc)
                await asyncio.sleep(1)
                continue
            except Exception:
                errors += 1
                logger.exception("Unexpected TikTok stats error for %s", row["video_url"])
                await asyncio.sleep(1)
                continue

            await self.save_snapshot_and_update_video(
                guild_id=guild_id,
                video_id=row["video_id"],
                stats=stats,
            )

            if stats.view_count is not None and stats.view_count >= HIT_THRESHOLD:
                await self.mark_hit_1m_and_alert(
                    guild_id=guild_id,
                    video_id=row["video_id"],
                    video_url=row["video_url"],
                    creator_username=row["creator_username"],
                    description=row["description"],
                    view_count=stats.view_count,
                    settings=settings,
                )
                hit_1m += 1

            await asyncio.sleep(1)

        return {"checked": checked, "hit_1m": hit_1m, "errors": errors}

    async def save_snapshot_and_update_video(self, guild_id: str, video_id: str, stats):
        assert self.db_pool is not None

        await self.db_pool.execute(
            """
            INSERT INTO video_snapshots (
                guild_id,
                video_id,
                view_count,
                like_count,
                comment_count,
                share_count
            )
            VALUES ($1, $2, $3, $4, $5, $6);
            """,
            guild_id,
            video_id,
            stats.view_count,
            stats.like_count,
            stats.comment_count,
            stats.share_count,
        )

        await self.db_pool.execute(
            """
            UPDATE tracked_videos
            SET last_checked_at = NOW(),
                last_view_count = $3,
                last_like_count = $4,
                last_comment_count = $5,
                last_share_count = $6,
                updated_at = NOW()
            WHERE guild_id = $1
              AND video_id = $2;
            """,
            guild_id,
            video_id,
            stats.view_count,
            stats.like_count,
            stats.comment_count,
            stats.share_count,
        )

    async def mark_hit_1m_and_alert(
        self,
        guild_id: str,
        video_id: str,
        video_url: str,
        creator_username: str,
        description: str,
        view_count: int,
        settings,
    ):
        assert self.db_pool is not None

        # Only update active videos. If another loop already marked it, this returns 0.
        result = await self.db_pool.execute(
            """
            UPDATE tracked_videos
            SET status = 'hit_1m',
                first_hit_1m_at = NOW(),
                view_count_at_hit = $3,
                last_view_count = $3,
                updated_at = NOW()
            WHERE guild_id = $1
              AND video_id = $2
              AND status = 'active';
            """,
            guild_id,
            video_id,
            view_count,
        )

        if int(result.split(" ")[-1]) != 1:
            return

        channel_id = settings["hit_channel_id"]
        if not channel_id:
            logger.warning("No hit channel set for guild %s", guild_id)
            return

        channel = self.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Hit channel not found for guild %s", guild_id)
            return

        await channel.send(format_hit_message(creator_username, description, video_url))

    @tasks.loop(time=time(hour=20, minute=0, tzinfo=BERLIN_TZ))
    async def daily_reporter(self):
        assert self.db_pool is not None

        guild_rows = await self.db_pool.fetch(
            """
            SELECT guild_id
            FROM tracker_settings
            WHERE daily_report_channel_id IS NOT NULL;
            """
        )

        today_berlin = datetime.now(BERLIN_TZ).date()

        for row in guild_rows:
            guild_id = row["guild_id"]

            already_sent = await self.db_pool.fetchval(
                """
                SELECT 1
                FROM daily_report_sends
                WHERE guild_id = $1
                  AND report_date = $2;
                """,
                guild_id,
                today_berlin,
            )

            if already_sent:
                continue

            try:
                await self.send_daily_report(guild_id, today_berlin)
                await self.db_pool.execute(
                    """
                    INSERT INTO daily_report_sends (guild_id, report_date)
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id, report_date) DO NOTHING;
                    """,
                    guild_id,
                    today_berlin,
                )
            except Exception:
                logger.exception("Failed sending daily report for guild %s", guild_id)

            await asyncio.sleep(2)

    @daily_reporter.before_loop
    async def before_daily_reporter(self):
        await self.wait_until_ready()
        logger.info("Daily reporter started at 20:00 Europe/Berlin")

    async def send_daily_report(self, guild_id: str, report_date):
        assert self.db_pool is not None

        settings = await self.get_settings(guild_id)
        channel_id = settings["daily_report_channel_id"]

        if not channel_id:
            return

        channel = self.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Daily report channel not found for guild %s", guild_id)
            return

        start_berlin = datetime.combine(report_date, time.min, tzinfo=BERLIN_TZ)
        end_berlin = datetime.combine(report_date, time.max, tzinfo=BERLIN_TZ)

        rows = await self.db_pool.fetch(
            """
            SELECT creator_username, description, video_url, view_count_at_hit, first_hit_1m_at
            FROM tracked_videos
            WHERE guild_id = $1
              AND status IN ('hit_1m', 'archived')
              AND first_hit_1m_at >= $2
              AND first_hit_1m_at <= $3
            ORDER BY first_hit_1m_at ASC;
            """,
            guild_id,
            start_berlin.astimezone(timezone.utc),
            end_berlin.astimezone(timezone.utc),
        )

        date_label = report_date.strftime("%d %B %Y")

        if not rows:
            await channel.send(
                f"📅 **Daily 1M View Report — {date_label}**\n\nNo videos hit 1M today."
            )
            return

        lines = [
            f"📅 **Daily 1M View Report — {date_label}**",
            "",
            f"Videos that hit 1M today: **{len(rows)}**",
            "",
        ]

        for index, row in enumerate(rows, start=1):
            creator = row["creator_username"] or "unknown"
            desc = truncate_text(row["description"], 60)
            url = row["video_url"]
            lines.append(f"{index}. **@{creator}** | \"{desc}\" | [VIEW HERE]({url})")

        await channel.send("\n".join(lines))


def format_hit_message(creator_username: str | None, description: str | None, video_url: str) -> str:
    creator = (creator_username or "unknown").replace("@", "").strip()
    desc = truncate_text(description, 80)
    return f'🎯 **1M Hit** | **@{creator}** | "{desc}" | [VIEW HERE]({video_url})'


def validate_env():
    missing = []

    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")

    if not DATABASE_URL:
        missing.append("DATABASE_URL")

    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


if __name__ == "__main__":
    validate_env()
    client = TikTokTrackerBot()
    client.run(DISCORD_TOKEN)
