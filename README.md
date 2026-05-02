# Telegram Moderation Bot (MVP)

Beginner-friendly MVP moderation bot for an AI Telegram channel.

## Features

- Admin-only `/start`
- `/draft` creates a test draft and sends it for moderation
- `/generate [source_url]` creates an OpenAI-powered Telegram draft in Russian for `@simplify_ai`
- Generated drafts use `prompts/post_style.md` style rules (human, short, non-corporate)
- Inline moderation buttons:
  - Publish now
  - Reject
  - Rewrite
- Publish approved content to a channel
- Save draft content, status, and `source_url` in SQLite
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
  writer.py
prompts/
  post_style.md
data/
  .gitkeep
requirements.txt
.env.example
README.md
```

## Requirements

- Python 3.11+
- Telegram bot token from BotFather
- OpenAI API key

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
- `OPENAI_API_KEY` — your OpenAI API key for `/generate`

## Commands

- `/start` — bot greeting (admin-only)
- `/draft` — creates test draft (kept for testing)
- `/generate` — creates AI draft
- `/generate https://example.com/article` — creates AI draft and stores source URL in DB

`source_url` is shown in moderation messages so the admin can validate context. It is not auto-appended to the channel post unless the generated text contains it.

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
  - `OPENAI_API_KEY`

## Security

- Never commit `.env` with real tokens or secrets.
- `.env.example` contains only placeholders.
