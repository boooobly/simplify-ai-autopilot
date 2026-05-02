"""Configuration helpers for the Telegram moderation bot."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load values from local .env file (if present).
load_dotenv()


@dataclass
class Settings:
    """Strongly-typed bot settings loaded from environment variables."""

    bot_token: str
    admin_id: int
    channel_id: str
    openai_api_key: str | None
    db_path: str = "data/drafts.db"
    schedule_timezone: str = "Europe/Moscow"


def load_settings() -> Settings:
    """Load and validate all required environment variables."""

    token = os.getenv("BOT_TOKEN", "").strip()
    admin_raw = os.getenv("ADMIN_ID", "").strip()
    channel_id = os.getenv("CHANNEL_ID", "").strip()
    openai_api_key_raw = os.getenv("OPENAI_API_KEY", "").strip()
    openai_api_key = openai_api_key_raw or None
    schedule_timezone = os.getenv("SCHEDULE_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"

    missing = []
    if not token:
        missing.append("BOT_TOKEN")
    if not admin_raw:
        missing.append("ADMIN_ID")
    if not channel_id:
        missing.append("CHANNEL_ID")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    try:
        admin_id = int(admin_raw)
    except ValueError as exc:
        raise ValueError("ADMIN_ID must be a valid integer Telegram user ID") from exc

    return Settings(
        bot_token=token,
        admin_id=admin_id,
        channel_id=channel_id,
        openai_api_key=openai_api_key,
        schedule_timezone=schedule_timezone,
    )
