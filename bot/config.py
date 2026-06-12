"""Configuration helpers for the Telegram moderation bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    model_topic_enrich: str = "tencent/hy3-preview"
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
    enable_telegram_channel_sources: bool = False
    x_api_bearer_token: str = ""
    x_accounts: list[str] = field(default_factory=list)
    x_max_posts_per_account: int = 5
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_session_string: str = ""
    telegram_source_channels: list[str] = field(default_factory=list)
    telegram_source_lookback_hours: int = 24
    telegram_source_max_posts_per_channel: int = 20
    openrouter_input_cost_per_1m: float = 0.0
    openrouter_output_cost_per_1m: float = 0.0
    openai_input_cost_per_1m: float = 0.0
    openai_output_cost_per_1m: float = 0.0
    daily_post_slots: list[str] = field(default_factory=lambda: ["10:00", "14:00", "18:00", "21:00"])
    custom_emoji_map: dict[str, str] = field(default_factory=dict)
    custom_emoji_aliases: dict[str, tuple[str, str]] = field(default_factory=dict)
    strict_config: bool = False
    config_warnings: list[str] = field(default_factory=list)

    @property
    def has_ai_provider(self) -> bool:
        return bool(self.openrouter_api_key or self.openai_api_key)


TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
RAILWAY_ENV_MARKERS = ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")
DEFAULT_DB_PATH = "data/drafts.db"
RAILWAY_DEFAULT_DB_WARNING = (
    "DB_PATH указывает на локальный data/drafts.db. На Railway без persistent volume "
    "SQLite-база с черновиками, отложенными публикациями, источниками и историей может "
    "потеряться после redeploy. Подключи Railway Volume и задай DB_PATH=/data/drafts.db "
    "или другой путь внутри mounted volume."
)


class ConfigWarningCollector:
    """Collect non-fatal configuration warnings, or fail fast in strict mode."""

    def __init__(self, *, strict: bool = False) -> None:
        self.strict = strict
        self.warnings: list[str] = []

    def add(self, message: str) -> None:
        if self.strict:
            raise ValueError(message)
        self.warnings.append(message)


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


def _parse_int_env(name: str, default: int, warnings: ConfigWarningCollector | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        if warnings is not None:
            warnings.add(f"{name} has invalid integer value; using default {default}.")
        return default


def _parse_int_range_env(
    name: str,
    default: int,
    min_value: int,
    max_value: int,
    warnings: ConfigWarningCollector | None = None,
) -> int:
    raw = os.getenv(name, "").strip()
    value = _parse_int_env(name, default, warnings)
    if not raw:
        return value
    if value < min_value or value > max_value:
        if warnings is not None:
            warnings.add(f"{name} must be between {min_value} and {max_value}; using default {default}.")
        return default
    return value


def _parse_float_env(name: str, default: float, warnings: ConfigWarningCollector | None = None) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        if warnings is not None:
            warnings.add(f"{name} has invalid numeric value; using default {default}.")
        return default


def _parse_daily_post_slots(raw: str, warnings: ConfigWarningCollector | None = None) -> list[str]:
    default_slots = ["10:00", "14:00", "18:00", "21:00"]
    slot_re = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
    slots: list[str] = []
    seen: set[str] = set()
    invalid_count = 0
    duplicate_count = 0
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        if slot_re.match(value):
            if value in seen:
                duplicate_count += 1
                continue
            seen.add(value)
            slots.append(value)
        else:
            invalid_count += 1
    if invalid_count and warnings is not None:
        fallback = default_slots if not slots else slots
        warnings.add(f"DAILY_POST_SLOTS contains {invalid_count} invalid time slots; using {', '.join(fallback)}.")
    if duplicate_count and warnings is not None:
        warnings.add(f"DAILY_POST_SLOTS contains {duplicate_count} duplicate time slots; duplicates were removed.")
    return slots or default_slots


def _validate_timezone(
    timezone_name: str,
    warnings: ConfigWarningCollector,
    default: str = "Europe/Moscow",
) -> str:
    try:
        ZoneInfo(timezone_name)
        return timezone_name
    except (ZoneInfoNotFoundError, ValueError):
        warnings.add(f"SCHEDULE_TIMEZONE '{timezone_name}' is invalid; using {default}.")
        return default


ALIAS_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
CUSTOM_EMOJI_FALLBACK_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\u2600-\u27BF"
    "]\ufe0f?",
)
CHANNEL_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,}$")
CHANNEL_NUMERIC_ID_RE = re.compile(r"^-?\d+$")


def _is_custom_emoji_fallback(value: str) -> bool:
    stripped = value.strip()
    if not stripped or not CUSTOM_EMOJI_FALLBACK_RE.search(stripped):
        return False
    remainder = CUSTOM_EMOJI_FALLBACK_RE.sub("", stripped)
    for joiner in ("\u200d", "\ufe0e", "\ufe0f", "\u20e3"):
        remainder = remainder.replace(joiner, "")
    return not remainder


def _parse_custom_emoji_map(raw: str, warnings: ConfigWarningCollector | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    skipped = 0
    for part in raw.split(";"):
        item = part.strip()
        if not item:
            continue
        if "|" not in item:
            skipped += 1
            continue
        fallback_emoji, custom_emoji_id = item.split("|", 1)
        fallback_emoji = fallback_emoji.strip()
        custom_emoji_id = custom_emoji_id.strip()
        if not _is_custom_emoji_fallback(fallback_emoji) or not custom_emoji_id.isdigit():
            skipped += 1
            continue
        result[fallback_emoji] = custom_emoji_id
    if skipped and warnings is not None:
        warnings.add(f"CUSTOM_EMOJI_MAP has {skipped} malformed entries; skipped invalid entries.")
    return result


def _parse_custom_emoji_aliases(raw: str, warnings: ConfigWarningCollector | None = None) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    skipped = 0
    for part in raw.split(";"):
        item = part.strip()
        if not item:
            continue
        pieces = item.split("|", 2)
        if len(pieces) != 3:
            skipped += 1
            continue
        alias, fallback_emoji, custom_emoji_id = (piece.strip() for piece in pieces)
        if not ALIAS_RE.match(alias):
            skipped += 1
            continue
        if not _is_custom_emoji_fallback(fallback_emoji) or not custom_emoji_id.isdigit():
            skipped += 1
            continue
        result[alias] = (fallback_emoji, custom_emoji_id)
    if skipped and warnings is not None:
        warnings.add(f"CUSTOM_EMOJI_ALIASES has {skipped} malformed entries; skipped invalid entries.")
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


def _is_railway_environment() -> bool:
    return any(os.getenv(name, "").strip() for name in RAILWAY_ENV_MARKERS)


def _is_default_local_db_path(db_path: str) -> bool:
    normalized = db_path.strip().replace("\\", "/").lower()
    return normalized in {DEFAULT_DB_PATH, f"./{DEFAULT_DB_PATH}"}


def _detect_railway_with_local_db_path(db_path: str) -> bool:
    return _is_railway_environment() and _is_default_local_db_path(db_path)


def _validate_db_path_parent(db_path: str) -> None:
    db_file = Path(db_path).expanduser()
    parent = db_file.parent if str(db_file.parent) else Path(".")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"DB_PATH parent directory cannot be created: {parent} ({exc})") from exc
    if not parent.is_dir():
        raise ValueError(f"DB_PATH parent path exists but is not a directory: {parent}")
    try:
        with tempfile.NamedTemporaryFile(prefix=".db-path-check-", dir=parent, delete=True) as probe:
            probe.write(b"ok")
            probe.flush()
    except OSError as exc:
        raise ValueError(f"DB_PATH parent directory is not writable: {parent} ({exc})") from exc


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
        f"model_topic_enrich: {settings.model_topic_enrich}",
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
        f"enable_telegram_channel_sources: {settings.enable_telegram_channel_sources}",
        f"telegram_source_channels count: {len(settings.telegram_source_channels)}",
        f"telegram_source_lookback_hours: {settings.telegram_source_lookback_hours}",
        f"telegram_source_max_posts_per_channel: {settings.telegram_source_max_posts_per_channel}",
        f"DB_PATH: {settings.db_path}",
        f"CHANNEL_ID type: {channel_type}",
        f"custom emoji aliases count: {len(settings.custom_emoji_aliases)}",
        f"custom emoji map count: {len(settings.custom_emoji_map)}",
        f"strict_config: {settings.strict_config}",
    ]
    for warning in settings.config_warnings:
        lines.append(f"CONFIG WARNING: {warning}")
    if _detect_railway_with_local_db_path(settings.db_path) and RAILWAY_DEFAULT_DB_WARNING not in settings.config_warnings:
        lines.append(f"CONFIG WARNING: {RAILWAY_DEFAULT_DB_WARNING}")
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
    model_topic_enrich = os.getenv("MODEL_TOPIC_ENRICH", "tencent/hy3-preview").strip() or "tencent/hy3-preview"
    model_polish = os.getenv("MODEL_POLISH", "anthropic/claude-sonnet-4.5").strip() or "anthropic/claude-sonnet-4.5"
    openrouter_site_url_raw = os.getenv("OPENROUTER_SITE_URL", "").strip()
    openrouter_site_url = openrouter_site_url_raw or None
    openrouter_app_name = os.getenv("OPENROUTER_APP_NAME", "Simplify AI Autopilot").strip() or "Simplify AI Autopilot"
    schedule_timezone = os.getenv("SCHEDULE_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
    db_path = os.getenv("DB_PATH", "data/drafts.db").strip() or "data/drafts.db"
    strict_config = _parse_bool_env("STRICT_CONFIG", False)
    config_warnings = ConfigWarningCollector(strict=strict_config)
    schedule_timezone = _validate_timezone(schedule_timezone, config_warnings)
    if _detect_railway_with_local_db_path(db_path):
        config_warnings.add(RAILWAY_DEFAULT_DB_WARNING)
    _validate_db_path_parent(db_path)
    openrouter_input_cost_per_1m = _parse_float_env("OPENROUTER_INPUT_COST_PER_1M", 0.0, config_warnings)
    openrouter_output_cost_per_1m = _parse_float_env("OPENROUTER_OUTPUT_COST_PER_1M", 0.0, config_warnings)
    openai_input_cost_per_1m = _parse_float_env("OPENAI_INPUT_COST_PER_1M", 0.0, config_warnings)
    openai_output_cost_per_1m = _parse_float_env("OPENAI_OUTPUT_COST_PER_1M", 0.0, config_warnings)
    daily_post_slots_raw = os.getenv("DAILY_POST_SLOTS", "10:00,14:00,18:00,21:00")
    daily_post_slots = _parse_daily_post_slots(daily_post_slots_raw, config_warnings)
    custom_emoji_map = _parse_custom_emoji_map(os.getenv("CUSTOM_EMOJI_MAP", ""), config_warnings)
    custom_emoji_aliases = _parse_custom_emoji_aliases(os.getenv("CUSTOM_EMOJI_ALIASES", ""), config_warnings)
    max_topic_age_days = _parse_int_range_env("MAX_TOPIC_AGE_DAYS", 14, 1, 60, config_warnings)
    topic_ai_enrich_limit = _parse_int_range_env("TOPIC_AI_ENRICH_LIMIT", 8, 0, 30, config_warnings)
    topic_ai_translate_limit = _parse_int_range_env("TOPIC_AI_TRANSLATE_LIMIT", 8, 0, 30, config_warnings)
    enable_reddit_sources = _parse_bool_env("ENABLE_REDDIT_SOURCES", False)
    enable_x_sources = _parse_bool_env("ENABLE_X_SOURCES", False)
    x_api_bearer_token = os.getenv("X_API_BEARER_TOKEN", "").strip()
    x_accounts = _parse_csv_env("X_ACCOUNTS")
    x_max_posts_per_account = _parse_int_range_env("X_MAX_POSTS_PER_ACCOUNT", 5, 1, 20, config_warnings)
    if enable_x_sources and (not x_api_bearer_token or not x_accounts):
        missing_x = []
        if not x_api_bearer_token:
            missing_x.append("X_API_BEARER_TOKEN")
        if not x_accounts:
            missing_x.append("X_ACCOUNTS")
        config_warnings.add(f"ENABLE_X_SOURCES is true but {', '.join(missing_x)} is not configured; X sources will not work until configured.")
    enable_telegram_channel_sources = _parse_bool_env("ENABLE_TELEGRAM_CHANNEL_SOURCES", False)
    telegram_api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    if telegram_api_id_raw and not telegram_api_id_raw.isdigit():
        config_warnings.add("TELEGRAM_API_ID has invalid integer value; using no Telegram API id.")
    telegram_api_id = int(telegram_api_id_raw) if telegram_api_id_raw.isdigit() else None
    telegram_api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    telegram_session_string = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
    telegram_source_channels = _parse_csv_env("TELEGRAM_SOURCE_CHANNELS")
    telegram_source_lookback_hours = _parse_int_range_env("TELEGRAM_SOURCE_LOOKBACK_HOURS", 24, 1, 168, config_warnings)
    telegram_source_max_posts_per_channel = _parse_int_range_env("TELEGRAM_SOURCE_MAX_POSTS_PER_CHANNEL", 20, 1, 100, config_warnings)
    if enable_telegram_channel_sources and (not telegram_api_id or not telegram_api_hash or not telegram_session_string or not telegram_source_channels):
        missing_telegram = []
        if not telegram_api_id:
            missing_telegram.append("TELEGRAM_API_ID")
        if not telegram_api_hash:
            missing_telegram.append("TELEGRAM_API_HASH")
        if not telegram_session_string:
            missing_telegram.append("TELEGRAM_SESSION_STRING")
        if not telegram_source_channels:
            missing_telegram.append("TELEGRAM_SOURCE_CHANNELS")
        config_warnings.add(
            f"ENABLE_TELEGRAM_CHANNEL_SOURCES is true but {', '.join(missing_telegram)} is not configured; Telegram channel sources will not work until configured."
        )

    post_max_chars = _parse_int_env("POST_MAX_CHARS", 1400, config_warnings)
    post_soft_chars = _parse_int_env("POST_SOFT_CHARS", 1100, config_warnings)
    if post_max_chars < 500:
        config_warnings.add("POST_MAX_CHARS must be at least 500; using default 1400.")
        post_max_chars = 1400
    if post_soft_chars < 400:
        config_warnings.add("POST_SOFT_CHARS must be at least 400; using default 1100.")
        post_soft_chars = 1100
    if post_soft_chars > post_max_chars:
        config_warnings.add("POST_SOFT_CHARS is greater than POST_MAX_CHARS; using POST_MAX_CHARS value.")
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
        model_topic_enrich=model_topic_enrich,
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
        enable_telegram_channel_sources=enable_telegram_channel_sources,
        telegram_api_id=telegram_api_id,
        telegram_api_hash=telegram_api_hash,
        telegram_session_string=telegram_session_string,
        telegram_source_channels=telegram_source_channels,
        telegram_source_lookback_hours=telegram_source_lookback_hours,
        telegram_source_max_posts_per_channel=telegram_source_max_posts_per_channel,
        openrouter_input_cost_per_1m=openrouter_input_cost_per_1m,
        openrouter_output_cost_per_1m=openrouter_output_cost_per_1m,
        openai_input_cost_per_1m=openai_input_cost_per_1m,
        openai_output_cost_per_1m=openai_output_cost_per_1m,
        daily_post_slots=daily_post_slots,
        custom_emoji_map=custom_emoji_map,
        custom_emoji_aliases=custom_emoji_aliases,
        strict_config=strict_config,
        config_warnings=list(config_warnings.warnings),
    )
