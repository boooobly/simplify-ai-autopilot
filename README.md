# Telegram Moderation Bot (MVP)

Beginner-friendly MVP moderation bot for an AI Telegram channel.

## Features

- Admin-only `/start`
- `/draft` creates a test draft and sends it for moderation
- Inline moderation buttons:
  - Publish now
  - Reject
  - Rewrite
- Publish approved content to a channel
- Save draft content and status in SQLite
- Runs with long polling (good for Railway worker service)

## Project structure

```text
main.py
bot/
  config.py
  database.py
  handlers.py
  publisher.py
  drafts.py
data/
  .gitkeep
requirements.txt
.env.example
README.md
```

## Requirements

- Python 3.11+
- Telegram bot token from BotFather

## Setup

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Create your env file:

```bash
cp .env.example .env
```

5. Fill `.env` values:

- `BOT_TOKEN` — your bot token
- `ADMIN_ID` — your Telegram numeric user ID
- `CHANNEL_ID` — channel username (example: `@my_channel`) or channel id

## Run locally

```bash
python main.py
```

## Railway deployment notes

Use this as your start command:

```bash
python main.py
```

Important:
- Deploy this bot as a **worker/background service** (long-running process).
- Set the environment variables in Railway project settings:
  - `BOT_TOKEN`
  - `ADMIN_ID`
  - `CHANNEL_ID`

## Security

- Never commit `.env` with real tokens or secrets.
- `.env.example` contains only placeholders.
