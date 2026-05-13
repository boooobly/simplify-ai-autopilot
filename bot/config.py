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
    max_topic_age_days: int = 14
    topic_ai_enrich_limit: int = 8
    topic_ai_translate_limit: int = 8
    enable_reddit_sources: bool = False
    enable_x_sources: bool = False
    x_api_bearer_token: str = ""
    x_accounts: list[str] = field(default_factory=list)
    x_max_posts_per_account: int = 5
    openrouter_input_cost_per_1m: float = 0.0
    openrouter_output_cost_per_1m: float = 0.0
    openai_input_cost_per_1m: float = 0.0
    openai_output_cost_per_1m: float = 0.0
    daily_post_slots: list[str] = field(default_factory=lambda: ["10:00", "14:00", "18:00", "21:00"])
    custom_emoji_map: dict[str, str] = field(default_factory=dict)
    custom_emoji_aliases: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def has_ai_provider(self) -> bool:
        return bool(self.openrouter_api_key or self.openai_api_key)


TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().casefold()
    if not raw:
        return default
    return raw in TRUE_ENV_VALUES


def _parse_csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    values: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        value = part.strip().lstrip("@").strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default



def _parse_int_range_env(name: str, default: int, min_value: int, max_value: int) -> int:
    value = _parse_int_env(name, default)
    if value < min_value or value > max_value:
        return default
    return value

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


ALIAS_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
CHANNEL_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,}$")
CHANNEL_NUMERIC_ID_RE = re.compile(r"^-?\d+$")


def _parse_custom_emoji_map(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in raw.split(";"):
        item = part.strip()
        if not item or "|" not in item:
            continue
        fallback_emoji, custom_emoji_id = item.split("|", 1)
        fallback_emoji = fallback_emoji.strip()
        custom_emoji_id = custom_emoji_id.strip()
        if not fallback_emoji or not custom_emoji_id.isdigit():
            continue
        result[fallback_emoji] = custom_emoji_id
    return result



def _parse_custom_emoji_aliases(raw: str) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for part in raw.split(";"):
        item = part.strip()
        if not item:
            continue
        pieces = item.split("|", 2)
        if len(pieces) != 3:
            continue
        alias, fallback_emoji, custom_emoji_id = (piece.strip() for piece in pieces)
        if not ALIAS_RE.match(alias):
            continue
        if not fallback_emoji or not custom_emoji_id.isdigit():
            continue
        result[alias] = (fallback_emoji, custom_emoji_id)
    return result


def _validate_channel_id(channel_id: str) -> str:
    value = channel_id.strip()
    lowered = value.lower()
    if "t.me/" in lowered or "telegram.me/" in lowered:
        raise ValueError("CHANNEL_ID должен быть @username канала или числовым id вида -100..., а не invite-ссылкой.")
    if CHANNEL_USERNAME_RE.match(value):
        return value
    if CHANNEL_NUMERIC_ID_RE.match(value):
        return value
    raise ValueError("CHANNEL_ID должен быть @username канала или числовым id вида -100..., а не invite-ссылкой.")


def _detect_railway_with_local_db_path(db_path: str) -> bool:
    is_railway = any(os.getenv(name, "").strip() for name in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"))
    if not is_railway:
        return False
    normalized = db_path.strip().replace("\\", "/").lower()
    return normalized in {"data/drafts.db", "./data/drafts.db"}


def _ai_provider_label(openrouter_api_key: str | None, openai_api_key: str | None) -> str:
    if openrouter_api_key:
        return "OpenRouter"
    if openai_api_key:
        return "OpenAI"
    return "not configured"


def startup_diagnostics(settings: Settings) -> list[str]:
    channel_type = "username" if settings.channel_id.startswith("@") else "numeric id"
    lines = [
        f"AI provider: {_ai_provider_label(settings.openrouter_api_key, settings.openai_api_key)}",
        f"model_draft: {settings.model_draft}",
        f"model_polish: {settings.model_polish}",
        f"schedule_timezone: {settings.schedule_timezone}",
        f"daily_post_slots: {', '.join(settings.daily_post_slots)}",
        f"post_soft_chars/post_max_chars: {settings.post_soft_chars}/{settings.post_max_chars}",
        f"max_topic_age_days: {settings.max_topic_age_days}",
        f"topic_ai_enrich_limit: {settings.topic_ai_enrich_limit}",
        f"topic_ai_translate_limit: {settings.topic_ai_translate_limit}",
        f"enable_reddit_sources: {settings.enable_reddit_sources}",
        f"enable_x_sources: {settings.enable_x_sources}",
        f"x_accounts count: {len(settings.x_accounts)}",
        f"x_max_posts_per_account: {settings.x_max_posts_per_account}",
        f"DB_PATH: {settings.db_path}",
        f"CHANNEL_ID type: {channel_type}",
        f"custom emoji aliases count: {len(settings.custom_emoji_aliases)}",
        f"custom emoji map count: {len(settings.custom_emoji_map)}",
    ]
    if _detect_railway_with_local_db_path(settings.db_path):
        lines.append(
            "Внимание: DB_PATH указывает на локальный data/drafts.db. На Railway без persistent volume база может потеряться после redeploy. Лучше использовать путь volume, например /data/drafts.db."
        )
    return lines

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
    custom_emoji_map = _parse_custom_emoji_map(os.getenv("CUSTOM_EMOJI_MAP", ""))
    custom_emoji_aliases = _parse_custom_emoji_aliases(os.getenv("CUSTOM_EMOJI_ALIASES", ""))
    max_topic_age_days = _parse_int_range_env("MAX_TOPIC_AGE_DAYS", 14, 1, 60)
    topic_ai_enrich_limit = _parse_int_range_env("TOPIC_AI_ENRICH_LIMIT", 8, 0, 30)
    topic_ai_translate_limit = _parse_int_range_env("TOPIC_AI_TRANSLATE_LIMIT", 8, 0, 30)
    enable_reddit_sources = _parse_bool_env("ENABLE_REDDIT_SOURCES", False)
    enable_x_sources = _parse_bool_env("ENABLE_X_SOURCES", False)
    x_api_bearer_token = os.getenv("X_API_BEARER_TOKEN", "").strip()
    x_accounts = _parse_csv_env("X_ACCOUNTS")
    x_max_posts_per_account = _parse_int_range_env("X_MAX_POSTS_PER_ACCOUNT", 5, 1, 20)

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
        raise ValueError(f"Не заданы обязательные переменные окружения: {', '.join(missing)}. Заполни их в .env/Railway Variables.")

    try:
        admin_id = int(admin_raw)
    except ValueError as exc:
        raise ValueError("ADMIN_ID должен быть целым числом (Telegram user id).") from exc

    channel_id = _validate_channel_id(channel_id)

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
        max_topic_age_days=max_topic_age_days,
        topic_ai_enrich_limit=topic_ai_enrich_limit,
        topic_ai_translate_limit=topic_ai_translate_limit,
        enable_reddit_sources=enable_reddit_sources,
        enable_x_sources=enable_x_sources,
        x_api_bearer_token=x_api_bearer_token,
        x_accounts=x_accounts,
        x_max_posts_per_account=x_max_posts_per_account,
        openrouter_input_cost_per_1m=openrouter_input_cost_per_1m,
        openrouter_output_cost_per_1m=openrouter_output_cost_per_1m,
        openai_input_cost_per_1m=openai_input_cost_per_1m,
        openai_output_cost_per_1m=openai_output_cost_per_1m,
        daily_post_slots=daily_post_slots,
        custom_emoji_map=custom_emoji_map,
        custom_emoji_aliases=custom_emoji_aliases,
    )
