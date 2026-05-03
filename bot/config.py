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
    openrouter_api_key: str | None
    model_draft: str = "moonshotai/kimi-k2.6"
    model_polish: str = "anthropic/claude-sonnet-4.5"
    openrouter_site_url: str | None = None
    openrouter_app_name: str = "Simplify AI Autopilot"
    db_path: str = "data/drafts.db"
    schedule_timezone: str = "Europe/Moscow"

    @property
    def has_ai_provider(self) -> bool:
        return bool(self.openrouter_api_key or self.openai_api_key)


def load_settings() -> Settings:
    """Load and validate all required environment variables."""

    token = os.getenv("BOT_TOKEN", "").strip()
    admin_raw = os.getenv("ADMIN_ID", "").strip()
    channel_id = os.getenv("CHANNEL_ID", "").strip()
    openai_api_key_raw = os.getenv("OPENAI_API_KEY", "").strip()
    openai_api_key = openai_api_key_raw or None
    openrouter_api_key_raw = os.getenv("OPENROUTER_API_KEY", "").strip()
    openrouter_api_key = openrouter_api_key_raw or None
    model_draft = os.getenv("MODEL_DRAFT", "moonshotai/kimi-k2.6").strip() or "moonshotai/kimi-k2.6"
    model_polish = os.getenv("MODEL_POLISH", "anthropic/claude-sonnet-4.5").strip() or "anthropic/claude-sonnet-4.5"
    openrouter_site_url_raw = os.getenv("OPENROUTER_SITE_URL", "").strip()
    openrouter_site_url = openrouter_site_url_raw or None
    openrouter_app_name = os.getenv("OPENROUTER_APP_NAME", "Simplify AI Autopilot").strip() or "Simplify AI Autopilot"
    schedule_timezone = os.getenv("SCHEDULE_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
    db_path = os.getenv("DB_PATH", "data/drafts.db").strip() or "data/drafts.db"

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
        openrouter_api_key=openrouter_api_key,
        model_draft=model_draft,
        model_polish=model_polish,
        openrouter_site_url=openrouter_site_url,
        openrouter_app_name=openrouter_app_name,
        db_path=db_path,
        schedule_timezone=schedule_timezone,
    )
