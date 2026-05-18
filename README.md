# TikTok 1M Tracker Discord Bot

Discord bot for Railway + PostgreSQL.

It reads Bloom's TikTok challenge-hit messages, saves TikTok video links, checks active videos hourly, sends a 🎯 1M alert, removes hit videos from active usage, and sends a daily 📅 20:00 Europe/Berlin report.

## Important TikTok API note

`TIKTOK_PROVIDER=official` uses TikTok's official `/v2/video/query/` endpoint. TikTok's docs describe this endpoint as querying videos for an authorized user, so it may not work for arbitrary public TikTok links from Bloom. If that happens, use `TIKTOK_PROVIDER=custom_http` with a TikTok tracker/data provider that accepts video URLs and returns view counts.

## Railway setup

1. Create a Railway project.
2. Add a PostgreSQL database.
3. Add these files to a GitHub repo and deploy it on Railway.
4. Add environment variables from `.env.example`.
5. In Discord Developer Portal, enable:
   - Server Members Intent is not required.
   - Message Content Intent should be enabled.
6. Invite the bot with:
   - `bot`
   - `applications.commands`
   - permissions: View Channels, Read Message History, Send Messages, Add Reactions, Use Slash Commands.

## Setup commands in Discord

Run these as an admin:

```text
/setup_source_channel #tiktok-hits
/setup_hit_channel #1m-alerts
/setup_daily_report_channel #daily-reports
/setup_bloom_bot 123456789012345678
/usage
```

## Main commands

```text
/usage
/check_now
/tracked
/hit_videos
/archive_old_hits
/set_capacity
```

## 1M alert format

```text
🎯 1M Hit | @creator | "description max 80 chars..." | VIEW HERE
```

## Usage format

```text
30/100
🟩🟩🟩⬛⬛⬛⬛⬛⬛⬛
```
