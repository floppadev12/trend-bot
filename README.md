# TikTok 1M Tracker Bot — No TikTok Auth

This bot works like your Bloom bot style: it uses `yt-dlp` to read public TikTok video pages.

It does **not** require TikTok API auth.

## What it does

- Reads Bloom's Discord hit messages from a configured source channel.
- Extracts the TikTok video link, creator, description, and posted date.
- Saves only active videos up to a capacity limit, default `100`.
- Checks active videos every hour.
- If a video reaches `1,000,000` views:
  - Sends: `🎯 1M Hit | @creator | "description..." | VIEW HERE`
  - Moves the video out of active usage by setting status to `hit_1m`.
- Sends a daily report at `20:00 Europe/Berlin`.
- `/usage` shows a green/black emoji capacity bar.

## Railway setup

1. Upload these files to GitHub.
2. Create a Railway project from the GitHub repo.
3. Add Railway PostgreSQL.
4. Add environment variables:
   - `DISCORD_TOKEN`
   - `DATABASE_URL`
   - optional `DISCORD_GUILD_ID`
5. In the Discord Developer Portal, enable **Message Content Intent**.
6. Invite the bot with:
   - `bot`
   - `applications.commands`
7. Give it permissions:
   - View Channel
   - Read Message History
   - Send Messages
   - Add Reactions
   - Use Slash Commands

## Setup commands

Run these in Discord:

```text
/setup_source_channel #channel-where-bloom-posts
/setup_hit_channel #1m-alerts
/setup_daily_report_channel #daily-reports
/setup_bloom_bot 123456789012345678
```

Then use:

```text
/usage
/check_now
/tracked
/hit_videos
/archive_old_hits
/set_capacity
/debug_tracker
```

## Important notes

This uses yt-dlp, so it can break if TikTok changes their public page or blocks requests.

If stats stop working, update yt-dlp:

```bash
pip install -U yt-dlp
```

On Railway, redeploy after changing requirements or environment variables.
