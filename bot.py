import os
import asyncio
import logging
from datetime import datetime, timezone

import asyncpg
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from tiktok_source import get_latest_videos, TikTokVideo


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

CHECK_INTERVAL_HOURS = 1
FIXED_KEYWORD = "challenge"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("tiktok-challenge-bot")


class ChallengeBot(discord.Client):
    def __init__(self):
        # Important:
        # Do not use discord.Intents.all().
        # This bot only needs default intents for slash commands.
        intents = discord.Intents.default()

        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.db_pool: asyncpg.Pool | None = None

        self.creator_group = app_commands.Group(
            name="creator",
            description="Manage TikTok creators to monitor",
        )

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

        self.auto_checker.start()

    async def close(self):
        if self.db_pool:
            await self.db_pool.close()

        await super().close()

    async def create_tables(self):
        assert self.db_pool is not None

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id TEXT PRIMARY KEY,
                alert_channel_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS creators (
                id SERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                username TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (guild_id, username)
            );
            """
        )

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_videos (
                id SERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                creator_username TEXT NOT NULL,
                video_id TEXT NOT NULL,
                video_url TEXT NOT NULL,
                description TEXT,
                posted_at TIMESTAMPTZ,
                detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                matched_keyword TEXT,
                alerted BOOLEAN NOT NULL DEFAULT FALSE,
                UNIQUE (guild_id, video_id)
            );
            """
        )

    def register_commands(self):
        bot = self

        @self.tree.command(
            name="setchannel",
            description="Set the channel where TikTok challenge alerts will be sent",
        )
        @app_commands.describe(channel="The Discord channel for alerts")
        async def setchannel(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            await bot.db_pool.execute(
                """
                INSERT INTO guild_settings (guild_id, alert_channel_id, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (guild_id)
                DO UPDATE SET alert_channel_id = $2, updated_at = NOW();
                """,
                str(interaction.guild.id),
                str(channel.id),
            )

            await interaction.response.send_message(
                f"✅ TikTok challenge alerts will be sent to {channel.mention}.",
                ephemeral=True,
            )

        @self.creator_group.command(
            name="add",
            description="Add a TikTok creator to monitor",
        )
        @app_commands.describe(username="TikTok username, with or without @")
        async def creator_add(
            interaction: discord.Interaction,
            username: str,
        ):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            clean_username = normalize_username(username)

            try:
                await bot.db_pool.execute(
                    """
                    INSERT INTO creators (guild_id, username)
                    VALUES ($1, $2);
                    """,
                    str(interaction.guild.id),
                    clean_username,
                )
            except asyncpg.UniqueViolationError:
                await interaction.response.send_message(
                    f"⚠️ `@{clean_username}` is already being monitored.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"✅ Added `@{clean_username}` to the monitoring list.",
                ephemeral=True,
            )

        @self.creator_group.command(
            name="remove",
            description="Remove a TikTok creator from monitoring",
        )
        @app_commands.describe(username="TikTok username, with or without @")
        async def creator_remove(
            interaction: discord.Interaction,
            username: str,
        ):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            clean_username = normalize_username(username)

            result = await bot.db_pool.execute(
                """
                DELETE FROM creators
                WHERE guild_id = $1 AND username = $2;
                """,
                str(interaction.guild.id),
                clean_username,
            )

            deleted_count = int(result.split(" ")[-1])

            if deleted_count == 0:
                await interaction.response.send_message(
                    f"⚠️ `@{clean_username}` was not in the monitoring list.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"✅ Removed `@{clean_username}`.",
                ephemeral=True,
            )

        @self.creator_group.command(
            name="list",
            description="List all TikTok creators being monitored",
        )
        async def creator_list(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            rows = await bot.db_pool.fetch(
                """
                SELECT username
                FROM creators
                WHERE guild_id = $1
                ORDER BY username ASC;
                """,
                str(interaction.guild.id),
            )

            if not rows:
                await interaction.response.send_message(
                    "No creators are being monitored yet. Add one with `/creator add`.",
                    ephemeral=True,
                )
                return

            creator_text = "\n".join(f"- `@{row['username']}`" for row in rows)

            await interaction.response.send_message(
                f"Monitoring these TikTok creators for `{FIXED_KEYWORD}`:\n{creator_text}",
                ephemeral=True,
            )

        @self.tree.command(
            name="checknow",
            description="Manually check all creators now",
        )
        async def checknow(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            result = await bot.check_guild(interaction.guild.id)

            note = ""

            if not result["alert_channel_set"]:
                note = "\n⚠️ No alert channel is set. Use `/setchannel` first."

            await interaction.followup.send(
                (
                    "✅ Manual check finished.\n"
                    f"Creators checked: `{result['creators_checked']}`\n"
                    f"New videos found: `{result['new_videos']}`\n"
                    f"Challenge hits sent: `{result['alerts_sent']}`"
                    f"{note}"
                ),
                ephemeral=True,
            )

        @self.tree.command(
            name="debug",
            description="Debug bot database state",
        )
        async def debug(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            guild_id_str = str(interaction.guild.id)

            creators = await bot.db_pool.fetch(
                """
                SELECT username
                FROM creators
                WHERE guild_id = $1
                ORDER BY username ASC;
                """,
                guild_id_str,
            )

            settings = await bot.db_pool.fetchrow(
                """
                SELECT alert_channel_id
                FROM guild_settings
                WHERE guild_id = $1;
                """,
                guild_id_str,
            )

            creator_text = "\n".join(f"- @{row['username']}" for row in creators)

            if not creator_text:
                creator_text = "No creators found."

            alert_channel_id = settings["alert_channel_id"] if settings else "Not set"

            await interaction.response.send_message(
                (
                    f"**Guild ID:** `{guild_id_str}`\n"
                    f"**Alert channel ID:** `{alert_channel_id}`\n"
                    f"**Creators:**\n{creator_text}"
                ),
                ephemeral=True,
            )

        self.tree.add_command(self.creator_group)

    @tasks.loop(hours=CHECK_INTERVAL_HOURS)
    async def auto_checker(self):
        assert self.db_pool is not None

        guild_rows = await self.db_pool.fetch(
            """
            SELECT DISTINCT guild_id
            FROM creators;
            """
        )

        for row in guild_rows:
            guild_id = int(row["guild_id"])

            try:
                result = await self.check_guild(guild_id)
                logger.info("Auto-checked guild %s: %s", guild_id, result)
            except Exception:
                logger.exception("Failed auto-checking guild %s", guild_id)

            await asyncio.sleep(2)

    @auto_checker.before_loop
    async def before_auto_checker(self):
        await self.wait_until_ready()
        logger.info("Auto checker started")

    async def check_guild(self, guild_id: int) -> dict:
        assert self.db_pool is not None

        guild_id_str = str(guild_id)

        creators = await self.db_pool.fetch(
            """
            SELECT username
            FROM creators
            WHERE guild_id = $1
            ORDER BY username ASC;
            """,
            guild_id_str,
        )

        settings = await self.db_pool.fetchrow(
            """
            SELECT alert_channel_id
            FROM guild_settings
            WHERE guild_id = $1;
            """,
            guild_id_str,
        )

        alert_channel_set = bool(settings and settings["alert_channel_id"])

        if not alert_channel_set:
            logger.info("Guild %s has no alert channel set", guild_id)

            return {
                "alert_channel_set": False,
                "creators_checked": len(creators),
                "new_videos": 0,
                "alerts_sent": 0,
            }

        channel = self.get_channel(int(settings["alert_channel_id"]))

        if not isinstance(channel, discord.TextChannel):
            logger.warning("Alert channel not found for guild %s", guild_id)

            return {
                "alert_channel_set": False,
                "creators_checked": len(creators),
                "new_videos": 0,
                "alerts_sent": 0,
            }

        creators_checked = 0
        new_videos = 0
        alerts_sent = 0

        for creator in creators:
            username = creator["username"]
            creators_checked += 1

            try:
                latest_videos = await get_latest_videos(username)
            except Exception:
                logger.exception("Failed fetching TikTok videos for @%s", username)
                continue

            logger.info("Fetched %s videos for @%s", len(latest_videos), username)

            for video in latest_videos:
                was_inserted = await self.save_seen_video(guild_id_str, video)

                if not was_inserted:
                    continue

                new_videos += 1

                if video_matches_challenge(video):
                    await self.mark_video_as_alerted(
                        guild_id_str=guild_id_str,
                        video_id=video.video_id,
                        matched_keyword=FIXED_KEYWORD,
                    )

                    await send_challenge_alert(channel, video)
                    alerts_sent += 1

                await asyncio.sleep(1)

            await asyncio.sleep(3)

        return {
            "alert_channel_set": True,
            "creators_checked": creators_checked,
            "new_videos": new_videos,
            "alerts_sent": alerts_sent,
        }

    async def save_seen_video(self, guild_id_str: str, video: TikTokVideo) -> bool:
        assert self.db_pool is not None

        result = await self.db_pool.execute(
            """
            INSERT INTO seen_videos (
                guild_id,
                creator_username,
                video_id,
                video_url,
                description,
                posted_at
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (guild_id, video_id)
            DO NOTHING;
            """,
            guild_id_str,
            video.creator_username,
            video.video_id,
            video.video_url,
            video.description,
            video.posted_at,
        )

        inserted_count = int(result.split(" ")[-1])

        return inserted_count == 1

    async def mark_video_as_alerted(
        self,
        guild_id_str: str,
        video_id: str,
        matched_keyword: str,
    ):
        assert self.db_pool is not None

        await self.db_pool.execute(
            """
            UPDATE seen_videos
            SET alerted = TRUE,
                matched_keyword = $3
            WHERE guild_id = $1
              AND video_id = $2;
            """,
            guild_id_str,
            video_id,
            matched_keyword,
        )


def normalize_username(username: str) -> str:
    username = username.strip()

    if username.startswith("@"):
        username = username[1:]

    return username.lower()


def video_matches_challenge(video: TikTokVideo) -> bool:
    description = video.description or ""

    return FIXED_KEYWORD.lower() in description.lower()


def format_datetime(dt: datetime | None) -> str:
    if not dt:
        return "Unknown"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def send_challenge_alert(channel: discord.TextChannel, video: TikTokVideo):
    description = video.description or ""

    if len(description) > 3500:
        description = description[:3500] + "..."

    embed = discord.Embed(
        title="🔥 TikTok challenge hit detected",
        url=video.video_url,
        color=discord.Color.orange(),
    )

    embed.add_field(
        name="Creator",
        value=f"@{video.creator_username}",
        inline=True,
    )

    embed.add_field(
        name="Matched keyword",
        value=f"`{FIXED_KEYWORD}`",
        inline=True,
    )

    embed.add_field(
        name="Date posted",
        value=format_datetime(video.posted_at),
        inline=False,
    )

    embed.add_field(
        name="Exact description",
        value=description if description else "No description found.",
        inline=False,
    )

    embed.add_field(
        name="Video link",
        value=video.video_url,
        inline=False,
    )

    await channel.send(embed=embed)


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

    client = ChallengeBot()
    client.run(DISCORD_TOKEN)
