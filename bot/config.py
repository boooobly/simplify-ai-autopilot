"""Configuration helpers for the Telegram moderation bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
import re

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
    post_max_chars: int = 1400
    post_soft_chars: int = 1100
    openrouter_input_cost_per_1m: float = 0.0
    openrouter_output_cost_per_1m: float = 0.0
    openai_input_cost_per_1m: float = 0.0
    openai_output_cost_per_1m: float = 0.0
    daily_post_slots: list[str] = field(default_factory=lambda: ["10:00", "14:00", "18:00", "21:00"])

    @property
    def has_ai_provider(self) -> bool:
        return bool(self.openrouter_api_key or self.openai_api_key)


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default



def _parse_daily_post_slots(raw: str) -> list[str]:
    default_slots = ["10:00", "14:00", "18:00", "21:00"]
    slot_re = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
    slots: list[str] = []
    for part in raw.split(","):
        value = part.strip()
        if value and slot_re.match(value):
            slots.append(value)
    return slots or default_slots

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
    openrouter_input_cost_per_1m = _parse_float_env("OPENROUTER_INPUT_COST_PER_1M", 0.0)
    openrouter_output_cost_per_1m = _parse_float_env("OPENROUTER_OUTPUT_COST_PER_1M", 0.0)
    openai_input_cost_per_1m = _parse_float_env("OPENAI_INPUT_COST_PER_1M", 0.0)
    openai_output_cost_per_1m = _parse_float_env("OPENAI_OUTPUT_COST_PER_1M", 0.0)
    daily_post_slots_raw = os.getenv("DAILY_POST_SLOTS", "10:00,14:00,18:00,21:00")
    daily_post_slots = _parse_daily_post_slots(daily_post_slots_raw)

    post_max_chars = _parse_int_env("POST_MAX_CHARS", 1400)
    post_soft_chars = _parse_int_env("POST_SOFT_CHARS", 1100)
    if post_max_chars < 500:
        post_max_chars = 1400
    if post_soft_chars < 400:
        post_soft_chars = 1100
    if post_soft_chars > post_max_chars:
        post_soft_chars = post_max_chars

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
        post_max_chars=post_max_chars,
        post_soft_chars=post_soft_chars,
        openrouter_input_cost_per_1m=openrouter_input_cost_per_1m,
        openrouter_output_cost_per_1m=openrouter_output_cost_per_1m,
        openai_input_cost_per_1m=openai_input_cost_per_1m,
        openai_output_cost_per_1m=openai_output_cost_per_1m,
        daily_post_slots=daily_post_slots,
    )
