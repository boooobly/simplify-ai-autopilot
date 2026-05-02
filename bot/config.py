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
    db_path: str = "data/drafts.db"


def load_settings() -> Settings:
    """Load and validate all required environment variables."""

    token = os.getenv("BOT_TOKEN", "").strip()
    admin_raw = os.getenv("ADMIN_ID", "").strip()
    channel_id = os.getenv("CHANNEL_ID", "").strip()

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

    return Settings(bot_token=token, admin_id=admin_id, channel_id=channel_id)
