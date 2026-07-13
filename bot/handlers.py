"""Telegram handlers for admin commands and moderation callbacks."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, ReplyKeyboardRemove, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot.config import _detect_railway_with_local_db_path
from bot import source_handlers
from bot.database import DraftDatabase
from bot.cleanup_handlers import (
    CLEANUP_PREVIEW_COUNTS_KEY as _CLEANUP_PREVIEW_COUNTS_KEY,
    CLEANUP_PREVIEW_GENERATED_AT_KEY as _CLEANUP_PREVIEW_GENERATED_AT_KEY,
    _cleanup_keyboard,
    _render_cleanup_preview_text,
    _store_cleanup_preview,
    handle_cleanup_callback,
)
from bot.drafts import create_test_draft, rewrite_test_draft
from bot.media_utils import decode_media_items, encode_media_group, media_count
from bot.publisher import publish_to_channel
from bot.moderation_handlers import (
    ModerationCallbackDeps,
    _rewrite_action_config as _moderation_rewrite_action_config,
    handle_draft_moderation_callback,
)
from bot.source_normalization import normalize_source_url as _normalize_source_url, normalize_telegram_channel_input
from bot.queue_helpers import (
    ACTIONABLE_DRAFT_STATUSES,
    _empty_slots_for_day,
    _get_day_range,
    _is_local_slot_free,
    _latest_actionable_drafts,
    _normalize_slot_hhmm,
    _parse_slot_hhmm,
    _queue_draft_pick_keyboard,
    _queue_keyboard,
    _render_queue_text,
    _schedule_draft_to_local_slot,
    _schedule_draft_to_nearest_slot,
)
from bot.telegram_formatting import render_post_html, strip_quote_markers
from bot.telegram_safety import (
    TELEGRAM_SAFE_TEXT_LIMIT,
    safe_edit_or_send_callback_message,
    safe_reply_text,
    safe_send_message,
    telegram_text_len,
    truncate_telegram_text,
)
from bot.topic_handlers import (
    topics_menu_command,
    _handle_topics_callback,
    handle_topic_moderation_action,
)


from bot.topic_scoring import canonical_topic_key, hybrid_topic_score
from bot.sources import (
    SourceReport,
    collect_topics,
    collect_topics_with_diagnostics,
    discover_rss_feed_url,
)
from bot.topic_display import build_deterministic_topic_metadata_ru, is_weak_topic_metadata, related_sources_summary, topic_angle_ru, topic_compact_preview_ru, topic_display_reason, topic_display_title, topic_original_title_line, topic_summary_ru
from bot.writer import (
    CompletionFallback,
    EmptyAIResponseError,
    GenerationResult,
    fetch_page_content,
    fetch_page_content_details,
    find_first_url,
    generate_post_draft,
    generate_post_draft_from_page,
    generate_post_draft_from_topic_metadata,
    normalize_url,
    translate_topic_title_to_ru,
    enrich_topic_metadata_ru,
    enrich_topic_understanding_ru,
    polish_post_draft,
    rewrite_post_draft,
    _parse_topic_metadata_fields,
)

logger = logging.getLogger(__name__)
_rewrite_action_config = _moderation_rewrite_action_config
CLEANUP_PREVIEW_COUNTS_KEY = _CLEANUP_PREVIEW_COUNTS_KEY
CLEANUP_PREVIEW_GENERATED_AT_KEY = _CLEANUP_PREVIEW_GENERATED_AT_KEY
ALLOWED_MEDIA_TYPES = {"photo", "video", "animation"}
ALLOWED_DRAFT_STATUSES = {"draft", "approved", "scheduled", "publishing", "published", "rejected", "failed"}
TELEGRAM_CAPTION_LIMIT = 1024
SHORT_MEDIA_PREVIEW_LIMIT = 850
EMPTY_AI_REPLY_TEXT = "Модель вернула пустой ответ. Черновик не создан. Попробуй ещё раз или смени MODEL_DRAFT."
REDDIT_METADATA_EMPTY_REPLY_TEXT = "Reddit-источник заблокирован, а по сохранённому описанию не удалось собрать нормальный черновик. Лучше отклонить тему или открыть источник вручную."


def normalize_source_url(value: str) -> str:
    """Backward-compatible public wrapper used by source-management callers."""
    return _normalize_source_url(value)

TOPIC_ENRICH_FALLBACK_SUMMARY_RU = "Нужен ручной просмотр: не удалось нормально обработать тему."
TOPIC_ENRICH_FALLBACK_ANGLE_RU = "Открой источник и проверь тему вручную перед генерацией поста."


def _topic_enrich_model(settings) -> str:
    return (getattr(settings, "model_topic_enrich", "") or getattr(settings, "model_draft", "")).strip() or getattr(settings, "model_draft", "")


def _apply_topic_enrichment_fallback(item, db: DraftDatabase, *, force: bool = False) -> bool:
    metadata = build_deterministic_topic_metadata_ru(item)
    before = (item.title_ru or "", item.summary_ru or "", item.angle_ru or "", item.reason_ru or "")
    existing_is_weak = is_weak_topic_metadata(item.title_ru, item.summary_ru, item.angle_ru, original_title=item.title)
    if force or existing_is_weak:
        item.title_ru = metadata["title_ru"]
        item.summary_ru = metadata["summary_ru"]
        item.angle_ru = metadata["angle_ru"]
        item.reason_ru = metadata["reason_ru"]
        setattr(item, "ai_value_score", None)
        setattr(item, "ai_value_reason_ru", None)
        setattr(item, "audience_fit_ru", None)
        setattr(item, "_deterministic_fallback_used", True)
    else:
        item.title_ru = item.title_ru or metadata["title_ru"]
        item.summary_ru = item.summary_ru or metadata["summary_ru"]
        item.angle_ru = item.angle_ru or metadata["angle_ru"]
        item.reason_ru = item.reason_ru or metadata["reason_ru"]
    changed = before != (item.title_ru or "", item.summary_ru or "", item.angle_ru or "", item.reason_ru or "")
    topic = db.find_topic_candidate_by_url(item.url)
    if topic:
        topic_is_weak = is_weak_topic_metadata(
            str(topic.get("title_ru") or ""),
            str(topic.get("summary_ru") or ""),
            str(topic.get("angle_ru") or ""),
            original_title=str(topic.get("title") or item.title or ""),
        )
        if force or topic_is_weak:
            db.force_update_topic_candidate_display_fields(
                int(topic["id"]),
                title_ru=item.title_ru,
                summary_ru=item.summary_ru,
                angle_ru=item.angle_ru,
                reason_ru=item.reason_ru or "",
                clear_ai_value=True,
                metadata_source="fallback",
            )
        else:
            db.update_topic_candidate_display_fields(
                int(topic["id"]),
                title_ru=item.title_ru,
                summary_ru=item.summary_ru,
                angle_ru=item.angle_ru,
                reason_ru=item.reason_ru,
            )
    return changed





NAV_PLAN_DAY = "🗓 План"
NAV_GENERATE_PLAN = "🧩 Черновики из плана"
NAV_QUEUE = "📅 Очередь"
NAV_DRAFTS = "📝 Черновики"
NAV_TOPICS = "🧠 Темы"
NAV_SOURCES = "📡 Источники"
NAV_USAGE = "📊 Расходы"
NAV_STYLE = "✍️ Стиль"
NAV_SETTINGS = "⚙️ Настройки"
NAV_HELP = "❓ Помощь"

CATEGORY_LABELS = {
    "news": "Новости",
    "tool": "Инструменты",
    "agent": "Агенты",
    "model": "Модели",
    "dev": "Разработка",
    "creator": "Видео/картинки",
    "mobile": "Приложения",
    "drama": "Фейлы/скандалы",
    "meme": "Мемное",
    "guide": "Гайды/курсы",
    "privacy": "Приватность",
    "research": "Исследования",
    "business": "Бизнес",
    "other": "Другое",
}

SOURCE_GROUP_LABELS = {
    "official_ai": "Официальные AI-блоги",
    "tech_media": "Техно-медиа",
    "ru_tech": "Русские медиа",
    "tools": "Инструменты",
    "community": "Сообщества",
    "github": "GitHub",
    "x": "X API",
    "custom": "Кастомные источники",
    "telegram": "Telegram-каналы",
    "other": "Другое",
}

def is_valid_rss_input_url(raw: str) -> bool:
    return source_handlers.is_valid_rss_input_url(raw)


def built_in_rss_sources() -> list[dict]:
    return source_handlers.built_in_rss_sources()


def env_configured_sources(settings) -> list[dict]:
    return source_handlers.env_configured_sources(settings)


def db_managed_sources(db: DraftDatabase) -> list[dict]:
    return source_handlers.db_managed_sources(db)


def find_duplicate_source(source_type: str, value: str, settings, db: DraftDatabase) -> dict | None:
    return source_handlers.find_duplicate_source(source_type, value, settings, db)


@dataclass
class TopicCollectStats:
    total: int = 0
    new: int = 0
    existing: int = 0
    near_duplicate: int = 0
    merged_story: int = 0
    low_score: int = 0
    low_quality: int = 0
    stale: int = 0
    missing_date: int = 0
    invalid: int = 0
    spam: int = 0
    source_seconds: float = 0.0
    store_seconds: float = 0.0
    ai_seconds: float = 0.0
    total_seconds: float = 0.0
    ai_enriched: int = 0
    ai_enrich_limit: int = 0
    deterministic_fallback_used: int = 0
    ai_enrichment_attempted: int = 0
    ai_enrichment_failed: int = 0
    ai_enrichment_skipped_no_provider: int = 0
    ai_enrichment_skipped_limit: int = 0
    ai_enrichment_skipped_no_candidates: int = 0
    ai_enrichment_skipped_existing_metadata: int = 0
    ai_enrichment_provider_error: int = 0
    ai_enrichment_invalid_model_output: int = 0
    ai_enrichment_invalid_json: int = 0
    ai_enrichment_invalid_fields: int = 0
    ai_enrichment_output_truncated: int = 0
    ai_enrichment_json_mode_unsupported: int = 0
    ai_enrichment_model: str = ""
    skipped_examples: dict[str, list[str]] | None = None
    ai_enrichment_selected: list[str] | None = None

    @property
    def enrichment_failed(self) -> int:
        return self.ai_enrichment_failed

    @enrichment_failed.setter
    def enrichment_failed(self, value: int) -> None:
        self.ai_enrichment_failed = value

    @property
    def enrichment_skipped_limit(self) -> int:
        return self.ai_enrichment_skipped_limit

    @enrichment_skipped_limit.setter
    def enrichment_skipped_limit(self, value: int) -> None:
        self.ai_enrichment_skipped_limit = value

    @property
    def enrichment_skipped_no_provider(self) -> int:
        return self.ai_enrichment_skipped_no_provider

    @enrichment_skipped_no_provider.setter
    def enrichment_skipped_no_provider(self, value: int) -> None:
        self.ai_enrichment_skipped_no_provider = value



def _topic_published_datetime(published_at: str | None) -> datetime | None:
    if not published_at:
        return None
    raw = str(published_at).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_stale_topic(item, max_topic_age_days: int) -> bool:
    published = _topic_published_datetime(getattr(item, "published_at", None))
    if published is None:
        return False
    age = datetime.now(timezone.utc) - published.astimezone(timezone.utc)
    return age > timedelta(days=max_topic_age_days)

def _category_label(value: str | None) -> str:
    return CATEGORY_LABELS.get((value or "other").strip().lower(), CATEGORY_LABELS["other"])


def _source_group_label(value: str | None) -> str:
    return SOURCE_GROUP_LABELS.get((value or "other").strip().lower(), SOURCE_GROUP_LABELS["other"])


def _score_label(score: int) -> str:
    if score >= 85:
        return "очень высокий"
    if score >= 70:
        return "высокий"
    if score >= 50:
        return "средний"
    return "низкий"


def _parse_topic_limit(context: ContextTypes.DEFAULT_TYPE, default: int, max_limit: int = 30) -> int:
    if not context.args:
        return default
    try:
        value = int(context.args[0])
    except (TypeError, ValueError):
        return default
    return max(1, min(max_limit, value))


def estimate_ai_cost(provider: str, prompt_tokens: int, completion_tokens: int, settings) -> float:
    if provider == "openrouter":
        input_cost = settings.openrouter_input_cost_per_1m
        output_cost = settings.openrouter_output_cost_per_1m
    else:
        input_cost = settings.openai_input_cost_per_1m
        output_cost = settings.openai_output_cost_per_1m
    return (prompt_tokens / 1_000_000) * input_cost + (completion_tokens / 1_000_000) * output_cost


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _render_usage_text(summary: dict[str, object], period_title: str, costs_enabled: bool) -> str:
    lines = [
        f"📊 Расходы ИИ за {period_title}",
        "",
        f"Запросов: {_fmt_int(int(summary['requests']))}",
        f"Input tokens: {_fmt_int(int(summary['prompt_tokens']))}",
        f"Output tokens: {_fmt_int(int(summary['completion_tokens']))}",
        f"Всего tokens: {_fmt_int(int(summary['total_tokens']))}",
    ]
    if costs_enabled:
        lines.append(f"Примерная стоимость: ${float(summary['estimated_cost_usd']):.4f}")
    else:
        lines.append("Стоимость не считается: укажи цены в Railway Variables.")
    lines.append("")
    lines.append("По моделям:")
    by_model = summary.get("by_model") or []
    if not by_model:
        lines.append("- пока нет данных")
    else:
        for row in by_model:
            lines.append(
                f"- {row['model']}: {int(row['requests'])} запросов, "
                f"{_fmt_int(int(row['total_tokens']))} tokens, ${float(row['estimated_cost_usd']):.4f}"
            )
    return "\n".join(lines)




def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧹 Очистка базы", callback_data="menu_cleanup_preview")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")],
        ]
    )


def _disabled_link_preview_options() -> LinkPreviewOptions:
    return LinkPreviewOptions(is_disabled=True)



def _format_failed_draft_line(draft: dict) -> str:
    draft_id = int(draft["id"])
    source_url = str(draft.get("source_url") or "—")
    media_type = str(draft.get("media_type") or "нет")
    media_total = media_count(draft.get("media_url"), draft.get("media_type"))
    updated_at = str(draft.get("updated_at") or "—")
    snippet = strip_quote_markers(str(draft.get("content") or "")).replace("\n", " ").strip()
    if len(snippet) > 120:
        snippet = snippet[:119].rstrip() + "…"
    if not snippet:
        snippet = "[пусто]"
    return (
        f"#{draft_id} | media: {media_type} ({media_total}) | updated: {updated_at}\n"
        f"URL: {source_url}\n"
        f"{snippet}"
    )


def _render_failed_drafts_text(drafts: list[dict]) -> str:
    lines = ["❗ Failed drafts (последние 10)", ""]
    for draft in drafts:
        lines.append(_format_failed_draft_line(draft))
        lines.append("")
    lines.append("Можно восстановить: /restore_draft ID")
    return "\n".join(lines).strip()


def _failed_drafts_keyboard(drafts: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for draft in drafts:
        draft_id = int(draft["id"])
        rows.append(
            [
                InlineKeyboardButton(f"Открыть #{draft_id}", callback_data=f"draft_info:{draft_id}"),
                InlineKeyboardButton(f"🔁 Восстановить #{draft_id}", callback_data=f"restore_draft:{draft_id}"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def _lane_label_ru(lane: str | None) -> str:
    mapping = {"breaking_news":"Важная новость","tool":"AI-сервис","creator":"Креаторский инструмент","short_video":"Идея для видео","meme":"AI-юмор","guide":"Гайд/промпт","business":"Бизнес/корпоративное","dev":"Для разработчиков","research":"Исследование","low_value":"Слабая тема"}
    return mapping.get((lane or "").strip().lower(), "Тема")


def _format_label_ru(fmt: str | None) -> str:
    mapping = {"post":"пост","short_video":"короткое видео","meme":"мем","tool_review":"обзор сервиса","guide":"гайд","news":"новость","tool":"инструмент","video":"видео","fail":"фейл","github":"GitHub","telegram":"Telegram"}
    return mapping.get((fmt or "").strip().lower(), "пост")


def _topic_card_text(topic: dict) -> str:
    score = int(topic.get("score") or 0)
    lane = str(topic.get("editorial_lane") or "")
    content_format = str(topic.get("content_format") or "post")
    lines = [
        f"🧠 Тема #{topic['id']} · {score}/100 · {_category_label(topic.get('category'))}",
        "",
        topic_display_title(topic),
        "",
        f"О чём: {topic_summary_ru(topic)}",
        f"Угол: {topic_angle_ru(topic)}",
    ]
    original_line = topic_original_title_line(topic)
    if original_line:
        lines.extend(["", original_line])
    related_line = related_sources_summary(topic)
    if related_line:
        lines.extend(["", related_line])
    lines.extend(
        [
            f"Источник: {topic['source']} · {_source_group_label(topic.get('source_group'))}",
            f"Формат: {_lane_label_ru(lane)} · {_format_label_ru(content_format)}",
            f"Почему: {topic_display_reason(topic)}",
            str(topic["url"]),
        ]
    )
    if topic.get("_show_metadata_diagnostics"):
        lines.extend([
            "",
            f"metadata_source: {topic.get('metadata_source') or 'fallback'}",
            f"metadata_is_weak: {str(bool(topic.get('_metadata_is_weak'))).lower()}",
            f"ai_enrichment_attempted: {str(bool(topic.get('_ai_enrichment_attempted'))).lower()}",
        ])
        if topic.get("_ai_enrichment_error"):
            lines.append(f"ai_enrichment_error: {topic['_ai_enrichment_error']}")
    return "\n".join(lines)


def _topic_actions_keyboard(topic_id: int, source_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("✍️ Создать черновик", callback_data=f"topic_generate:{topic_id}")]]
    rows.append([InlineKeyboardButton("🧠 Понять тему через AI", callback_data=f"topic_reenrich:{topic_id}")])
    if source_url:
        rows.append([InlineKeyboardButton("🔗 Открыть источник", url=source_url)])
    rows.append([InlineKeyboardButton("❌ Отклонить тему", callback_data=f"reject_topic:{topic_id}")])
    return InlineKeyboardMarkup(rows)


def _topics_hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔥 Горячие", callback_data="topics_hot:0"), InlineKeyboardButton("🆕 Новые", callback_data="topics_new:0")],
            [InlineKeyboardButton("🛠 Инструменты", callback_data="topics_tools:0"), InlineKeyboardButton("📰 Новости", callback_data="topics_news:0")],
            [InlineKeyboardButton("😄 Живые", callback_data="topics_fun:0"), InlineKeyboardButton("🎬 Идеи для видео", callback_data="topics_video:0")],
            [InlineKeyboardButton("🧩 Гайды", callback_data="topics_guides:0"), InlineKeyboardButton("⭐ Лучшее", callback_data="topics_best:0")],
            [InlineKeyboardButton("🔄 Собрать темы", callback_data="menu_collect")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")],
        ]
    )


def _collect_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧠 Открыть темы", callback_data="menu_topics")],
            [InlineKeyboardButton("🔥 Горячие", callback_data="topics_hot:0"), InlineKeyboardButton("🆕 Лучшие новые", callback_data="topics_new:0")],
            [InlineKeyboardButton("🔄 Собрать ещё", callback_data="menu_collect")],
        ]
    )


def _topics_for_kind(db: DraftDatabase, kind: str, limit: int = 10) -> list[dict]:
    if kind == "hot":
        return db.list_topic_candidates_min_score(limit=limit, status="new", min_score=75)
    if kind == "tools":
        return db.list_topic_candidates_filtered(limit=limit, status="new", categories=["tool", "creator", "guide", "dev", "mobile"])
    if kind == "news":
        return db.list_topic_candidates_filtered(limit=limit, status="new", categories=["news", "model", "agent", "research", "business", "privacy"])
    if kind == "video":
        return db.list_topic_candidates_by_editorial(limit=limit, lanes=["short_video", "creator", "tool", "meme"], formats=["short_video", "tool_review", "meme"], min_score=62)
    if kind == "guides":
        return db.list_topic_candidates_by_editorial(limit=limit, lanes=["guide"], categories=["guide"])
    if kind == "best":
        return db.get_balanced_topic_shortlist(limit=limit, hours=48, min_score=60)
    if kind == "fun":
        topics_by_category = db.list_topic_candidates_filtered(limit=20, status="new", categories=["drama", "meme"])
        topics_by_group = db.list_topic_candidates_filtered(limit=20, status="new", source_groups=["community", "github", "x", "custom"])
        merged: dict[int, dict] = {}
        for topic in topics_by_category + topics_by_group:
            merged[int(topic["id"])] = topic
        return sorted(merged.values(), key=lambda t: (int(t.get("score") or 0), str(t.get("created_at") or "")), reverse=True)[:limit]
    return db.list_topic_candidates(limit=limit, status="new", order_by_score=True)


def _topic_count(db: DraftDatabase, kind: str) -> int:
    return len(_topics_for_kind(db, kind, limit=1000))


def _render_topics_hub_text(db: DraftDatabase) -> str:
    return "\n".join(
        [
            "🧠 Темы",
            f"Горячие: {_topic_count(db, 'hot')}",
            f"Новые: {_topic_count(db, 'new')}",
            f"Инструменты: {_topic_count(db, 'tools')}",
            f"Новости: {_topic_count(db, 'news')}",
            f"Мемное/живое: {_topic_count(db, 'fun')}",
            f"Видео: {_topic_count(db, 'video')}",
            f"Гайды: {_topic_count(db, 'guides')}",
            f"Лучшее: {_topic_count(db, 'best')}",
        ]
    )


def _topic_preview_line(topic: dict) -> str:
    score = int(topic.get("score") or 0)
    first, *_rest = topic_compact_preview_ru(topic, max_len=120).splitlines()
    return f"- {score} - {_category_label(topic.get('category'))} - {first}"


def _render_topic_preview_list(title: str, topics: list[dict]) -> str:
    lines = [title, ""]
    if not topics:
        lines.append("Тем пока нет. Запусти /collect или /collect_debug.")
    else:
        lines.extend(_topic_preview_line(topic) for topic in topics[:5])
    return "\n".join(lines)


def _is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


def _ai_provider_for_status(settings) -> str:
    if settings.openrouter_api_key and settings.openai_api_key:
        return "OpenRouter → OpenAI fallback"
    if settings.openrouter_api_key:
        return "OpenRouter"
    if settings.openai_api_key:
        return "OpenAI"
    return "не настроен"


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    provider = _ai_provider_for_status(settings)
    draft_route = _resolve_ai_request(settings, "draft")
    topic_route = _resolve_ai_request(settings, "topic_enrich")
    polish_route = _resolve_ai_request(settings, "polish")
    lines = [
        "Бот запущен",
        f"Провайдер AI: {provider}",
        f"Draft model: {draft_route.model or 'не настроена'}",
        f"Topic enrich model: {topic_route.model or 'не настроена'}",
        f"Polish model: {polish_route.model or 'не настроена'}",
        f"Таймзона: {settings.schedule_timezone}",
        f"Слоты: {', '.join(settings.daily_post_slots)}",
        f"DB path: {settings.db_path}",
        f"AI настроен: {'да' if settings.has_ai_provider else 'нет'}",
        f"Emoji aliases: {len(settings.custom_emoji_aliases)}",
        f"Emoji map: {len(settings.custom_emoji_map)}",
    ]
    if _detect_railway_with_local_db_path(settings.db_path):
        lines.append("⚠️ DB_PATH сейчас local data/drafts.db. Источники, добавленные через бота, могут пропасть после redeploy. Лучше подключить Railway Volume и поставить DB_PATH=/data/drafts.db.")
    if update.message:
        await update.message.reply_text("\n".join(lines), reply_markup=_admin_reply_keyboard())


def _parse_callback_data(data: str) -> tuple[str, int, str | None]:
    if data.startswith("schedule_slot:"):
        action, draft_id_raw, slot = data.split(":", 2)
        return action, int(draft_id_raw), slot
    if data.startswith("queue_pick_slot:"):
        action, day_offset_raw, slot = data.split(":", 2)
        return action, int(day_offset_raw), slot
    if data.startswith("queue_schedule_draft:"):
        action, draft_id_raw, day_offset_raw, slot = data.split(":", 3)
        return action, int(draft_id_raw), f"{day_offset_raw}:{slot}"
    action, draft_id_raw = data.split(":", 1)
    return action, int(draft_id_raw), None


def _can_publish(status: str | None) -> bool:
    return status in {"draft", "approved", "scheduled"}


def _can_schedule(status: str | None) -> bool:
    return status in ACTIONABLE_DRAFT_STATUSES


def _can_edit(status: str | None) -> bool:
    return status in ACTIONABLE_DRAFT_STATUSES


def _status_guard_message(action: str, status: str | None) -> str:
    if status == "published":
        if action == "schedule":
            return "Опубликованный черновик уже нельзя планировать."
        if action == "publish":
            return "Этот черновик уже опубликован."
        if action == "edit":
            return "Опубликованный черновик нельзя редактировать."
    if status == "rejected":
        if action == "schedule":
            return "Отклонённый черновик нельзя планировать."
        if action == "publish":
            return "Этот черновик отклонён. Сначала создай новый или восстанови его позже."
        if action == "edit":
            return "Отклонённый черновик нельзя редактировать."
    if status == "publishing":
        return "Черновик сейчас публикуется. Подожди немного."
    if status == "failed":
        return "Черновик в статусе failed. Сначала верни его в черновики."
    if status == "scheduled" and action == "edit":
        return "Запланированный черновик уже в очереди. Сначала сними его с очереди позже."
    if status == "scheduled" and action == "schedule":
        return "Черновик уже запланирован."
    return f"Это действие недоступно для текущего статуса: {status or 'unknown'}."


def _schedule_keyboard(draft_id: int, slots: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(slot, callback_data=f"schedule_slot:{draft_id}:{slot}")] for slot in slots]
    )



def _select_daily_plan_topics(db: DraftDatabase, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    raw_candidates = db.list_topic_candidates(limit=max(80, limit * 8), status="new", order_by_score=True)
    now = datetime.now(timezone.utc)
    undated_allowed_groups = {"github", "tools", "community", "x", "telegram"}
    fast_news_categories = {"news", "model", "agent", "business", "privacy", "drama", "research"}
    candidates: list[dict] = []
    for topic in raw_candidates:
        if int(topic.get("score") or 0) < 60:
            continue
        published = _topic_published_datetime(str(topic.get("published_at") or ""))
        if published is None:
            if str(topic.get("source_group") or "") not in undated_allowed_groups:
                continue
        else:
            max_age = timedelta(days=3 if str(topic.get("category") or "") in fast_news_categories else 7)
            if now - published.astimezone(timezone.utc) > max_age:
                continue
        candidates.append(topic)
    if not candidates:
        return []
    selected: list[dict] = []
    selected_ids: set[int] = set()
    category_counts: dict[str, int] = {}
    source_group_counts: dict[str, int] = {}

    def _pick_from(predicate, respect_balance: bool = True) -> None:
        if len(selected) >= limit:
            return
        for topic in candidates:
            topic_id = int(topic["id"])
            category = str(topic.get("category") or "other").strip().lower()
            source_group = str(topic.get("source_group") or "other").strip().lower()
            if topic_id in selected_ids or not predicate(topic, category, source_group):
                continue
            if respect_balance and (category_counts.get(category, 0) >= 2 or source_group_counts.get(source_group, 0) >= 2):
                continue
            selected.append(topic)
            selected_ids.add(topic_id)
            category_counts[category] = category_counts.get(category, 0) + 1
            source_group_counts[source_group] = source_group_counts.get(source_group, 0) + 1
            return

    _pick_from(lambda _t, c, _g: c in {"tool", "guide", "creator", "dev", "mobile"})
    _pick_from(lambda _t, c, _g: c in {"news", "model", "agent", "research", "privacy"})
    _pick_from(lambda _t, c, g: c in {"drama", "meme"} or g in {"community", "github", "x", "custom"})
    while len(selected) < limit:
        previous = len(selected)
        _pick_from(lambda _t, _c, _g: True, respect_balance=True)
        if len(selected) == previous:
            break
    while len(selected) < limit:
        previous = len(selected)
        _pick_from(lambda _t, _c, _g: True, respect_balance=False)
        if len(selected) == previous:
            break
    return selected[:limit]


def _render_plan_text(day_name: str, slots: list[str], topics: list[dict]) -> str:
    title = f"🗓️ План тем на {day_name}"
    if not slots:
        queue_hint = "/queue_today" if day_name == "сегодня" else "/queue_tomorrow"
        return f"{title}\n\nНа {day_name} все слоты уже заняты. Проверь {queue_hint}"
    if not topics:
        return f"{title}\n\nПустые слоты: {len(slots)}\nНет подходящих тем. Запусти /collect_debug или /topics_hot"
    lines = [title, "", f"Пустые слоты: {len(slots)}"]
    for slot, topic in zip(slots, topics):
        score = int(topic.get("score") or 0)
        lines.extend(
            [
                f"{slot} - тема #{topic['id']} - {_category_label(topic.get('category'))} - вес {score}",
                topic_display_title(topic),
                f"О чем: {topic_summary_ru(topic)}",
                f"Идея поста: {topic_angle_ru(topic)}",
                "",
            ]
        )
    lines.extend(
        [
            "Что дальше:",
            'нажми "✍️ Создать черновик" под нужной темой, потом проверь текст и поставь в очередь.',
            "Можно создать черновики по одному под карточками или нажать кнопку создания черновиков из всего плана.",
        ]
    )
    return "\n".join(lines).rstrip()


def _plan_summary_keyboard(day_offset: int, can_generate: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if can_generate:
        callback = "menu_generate_plan_day" if day_offset == 0 else "menu_generate_plan_tomorrow"
        rows.append([InlineKeyboardButton("🧩 Создать черновики из плана", callback_data=callback)])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")])
    return InlineKeyboardMarkup(rows)


def _main_menu_text() -> str:
    return "🤖 Simplify AI Autopilot\n\nВыбери действие:"


def _clear_pending_plan_schedule(context) -> None:
    context.user_data.pop("pending_plan_schedule_day", None)
    context.user_data.pop("pending_plan_schedule_items", None)


PENDING_EDIT_DRAFT_KEY = "pending_edit_draft_id"
PENDING_MEDIA_DRAFT_KEY = "pending_media_draft_id"


def _set_pending_edit(context, draft_id: int) -> None:
    context.user_data[PENDING_EDIT_DRAFT_KEY] = draft_id


def _get_pending_edit(context) -> int | None:
    value = context.user_data.get(PENDING_EDIT_DRAFT_KEY)
    return int(value) if value is not None else None


def _clear_pending_edit(context) -> None:
    context.user_data.pop(PENDING_EDIT_DRAFT_KEY, None)


def _set_pending_media(context, draft_id: int) -> None:
    context.user_data[PENDING_MEDIA_DRAFT_KEY] = draft_id
    context.user_data["pending_media_items"] = []


def _get_pending_media(context) -> int | None:
    value = context.user_data.get(PENDING_MEDIA_DRAFT_KEY)
    return int(value) if value is not None else None


def _clear_pending_media(context) -> None:
    context.user_data.pop(PENDING_MEDIA_DRAFT_KEY, None)
    context.user_data.pop("pending_media_items", None)


def _generated_plan_keyboard(day_offset: int, has_created: bool) -> InlineKeyboardMarkup | None:
    queue_callback = "queue_today:0" if day_offset == 0 else "queue_tomorrow:0"
    schedule_callback = "menu_schedule_generated_plan_day" if day_offset == 0 else "menu_schedule_generated_plan_tomorrow"
    rows: list[list[InlineKeyboardButton]] = []
    if has_created:
        rows.append([InlineKeyboardButton("📅 Поставить созданные в очередь", callback_data=schedule_callback)])
    rows.append([InlineKeyboardButton("📅 Открыть очередь", callback_data=queue_callback)])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")])
    return InlineKeyboardMarkup(rows) if rows else None


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✍️ Создать черновик", callback_data="menu_generate")],
            [InlineKeyboardButton("🔗 Пост из ссылки", callback_data="menu_url_help")],
            [InlineKeyboardButton("📝 Черновики", callback_data="menu_drafts")],
            [InlineKeyboardButton("🧠 Темы", callback_data="menu_topics")],
            [InlineKeyboardButton("📡 Источники", callback_data="menu_sources")],
            [InlineKeyboardButton("🗓️ План на день", callback_data="menu_plan_day")],
            [InlineKeyboardButton("📅 Очередь", callback_data="menu_queue")],
            [InlineKeyboardButton("📊 Расходы ИИ", callback_data="menu_usage")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")],
            [InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
        ]
    )


def _back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")]])


def _sources_hub_keyboard() -> InlineKeyboardMarkup:
    return source_handlers.sources_hub_keyboard()


def _source_card_keyboard(source_id: int, enabled: bool) -> InlineKeyboardMarkup:
    return source_handlers.source_card_keyboard(source_id, enabled)


def _settings_text(settings) -> str:
    ai_provider = _ai_provider_for_status(settings)
    draft_route = _resolve_ai_request(settings, "draft")
    topic_route = _resolve_ai_request(settings, "topic_enrich")
    polish_route = _resolve_ai_request(settings, "polish")
    fallback_line = ""
    if draft_route.fallback and topic_route.fallback and polish_route.fallback:
        fallback_line = (
            f"OpenAI fallback: {draft_route.fallback.model} / "
            f"{topic_route.fallback.model} / {polish_route.fallback.model}\n"
        )
    return (
        "⚙️ Настройки\n\n"
        f"Провайдер ИИ: {ai_provider}\n"
        f"Модель черновика: {draft_route.model or 'не настроена'}\n"
        f"Модель тем: {topic_route.model or 'не настроена'}\n"
        f"Модель улучшения: {polish_route.model or 'не настроена'}\n"
        f"{fallback_line}"
        f"Часовой пояс: {settings.schedule_timezone}\n"
        f"Длина поста: до {settings.post_soft_chars} / максимум {settings.post_max_chars} символов\n"
        f"База данных: {settings.db_path}"
    )


@dataclass(frozen=True)
class AIRequestRoute:
    api_key: str = field(repr=False)
    provider: str
    model: str
    base_url: str | None
    extra_headers: dict[str, str] | None
    fallback: CompletionFallback | None = None


def _model_for_role(settings, role: str, *, openai: bool = False) -> str:
    role_to_attr = {
        "draft": "model_draft",
        "topic_enrich": "model_topic_enrich",
        "polish": "model_polish",
    }
    try:
        primary_attr = role_to_attr[role]
    except KeyError as exc:
        raise ValueError(f"Unsupported AI model role: {role}") from exc
    primary_model = str(getattr(settings, primary_attr, "") or "").strip()
    if not openai:
        return primary_model
    fallback_attr = f"openai_{primary_attr}"
    return str(getattr(settings, fallback_attr, "") or primary_model).strip()


def _openrouter_headers(settings) -> dict[str, str]:
    headers = {"X-Title": settings.openrouter_app_name}
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    return headers


def _resolve_ai_request(settings, role: str) -> AIRequestRoute:
    """Resolve a provider-compatible model and an independent secondary route."""

    if settings.openrouter_api_key:
        fallback = None
        if settings.openai_api_key:
            fallback = CompletionFallback(
                api_key=settings.openai_api_key,
                model=_model_for_role(settings, role, openai=True),
                provider="openai",
            )
        return AIRequestRoute(
            api_key=settings.openrouter_api_key,
            provider="openrouter",
            model=_model_for_role(settings, role),
            base_url="https://openrouter.ai/api/v1",
            extra_headers=_openrouter_headers(settings),
            fallback=fallback,
        )
    if settings.openai_api_key:
        return AIRequestRoute(
            api_key=settings.openai_api_key,
            provider="openai",
            model=_model_for_role(settings, role, openai=True),
            base_url=None,
            extra_headers=None,
        )
    return AIRequestRoute(api_key="", provider="", model="", base_url=None, extra_headers=None)


def _resolve_ai_provider(settings) -> tuple[str, str, str | None, dict[str, str] | None]:
    """Backward-compatible provider-only resolver for extension callers."""

    route = _resolve_ai_request(settings, "draft")
    return route.api_key, route.provider, route.base_url, route.extra_headers


async def _run_collect_topics(settings=None, db=None):
    return await asyncio.to_thread(collect_topics, settings, db)


async def _run_collect_topics_with_diagnostics(settings=None, db=None):
    return await asyncio.to_thread(collect_topics_with_diagnostics, settings, db)


async def _safe_answer_callback(query, text: str | None = None, show_alert: bool = False) -> None:
    try:
        await query.answer(text=text, show_alert=show_alert)
    except BadRequest as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message:
            logger.debug("Ignoring stale callback answer error: %s", exc)
            return
        raise


async def _run_fetch_page_content(source_url: str):
    return await asyncio.to_thread(fetch_page_content, source_url)


async def _run_fetch_page_content_details(source_url: str):
    return await asyncio.to_thread(fetch_page_content_details, source_url)


async def _run_generate_post_draft(*args, **kwargs):
    return await asyncio.to_thread(generate_post_draft, *args, **kwargs)


async def _run_generate_post_draft_from_page(*args, **kwargs):
    return await asyncio.to_thread(generate_post_draft_from_page, *args, **kwargs)


async def _run_generate_post_draft_from_topic_metadata(*args, **kwargs):
    return await asyncio.to_thread(generate_post_draft_from_topic_metadata, *args, **kwargs)


async def _run_polish_post_draft(*args, **kwargs):
    return await asyncio.to_thread(polish_post_draft, *args, **kwargs)


async def _run_rewrite_post_draft(*args, **kwargs):
    return await asyncio.to_thread(rewrite_post_draft, *args, **kwargs)


async def _run_translate_topic_title_to_ru(*args, **kwargs):
    return await asyncio.to_thread(translate_topic_title_to_ru, *args, **kwargs)


async def _run_enrich_topic_metadata_ru(*args, **kwargs):
    return await asyncio.to_thread(enrich_topic_metadata_ru, *args, **kwargs)


async def _run_enrich_topic_understanding_ru(*args, **kwargs):
    return await asyncio.to_thread(enrich_topic_understanding_ru, *args, **kwargs)


async def _translate_topic_title_if_available(item, settings, db: DraftDatabase) -> None:
    if item.title_ru or not settings or not getattr(settings, "has_ai_provider", False):
        return
    route = _resolve_ai_request(settings, "topic_enrich")
    if not route.api_key:
        return
    result = await _run_translate_topic_title_to_ru(
        api_key=route.api_key,
        model=route.model,
        title=item.title,
        base_url=route.base_url,
        extra_headers=route.extra_headers,
        provider=route.provider,
        fallback=route.fallback,
    )
    if not result or not result.content.strip() or result.content.strip() == item.title.strip():
        return
    topic = db.find_topic_candidate_by_url(item.url)
    if not topic:
        return
    item.title_ru = result.content.strip()
    db.update_topic_candidate_display_fields(int(topic["id"]), title_ru=item.title_ru, reason_ru=item.reason_ru)
    used_provider = result.provider or route.provider
    estimated_cost = estimate_ai_cost(used_provider, result.prompt_tokens, result.completion_tokens, settings)
    db.record_ai_usage(
        provider=used_provider,
        model=result.model or route.model,
        operation="topic_translate_title",
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        estimated_cost_usd=estimated_cost,
        source_url=item.url,
        draft_id=None,
    )


def _parse_ai_value_score(value: object) -> int | None:
    text = str(value or "").strip()
    match = re.search(r"-?\d{1,3}", text)
    if not match:
        return None
    score = int(match.group(0))
    if not 0 <= score <= 100:
        return None
    return score


def _short_topic_reason_part(text: str, limit: int = 150) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip()).strip(" .")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rsplit(" ", 1)[0].rstrip(" .,;") + "…"


def _combined_topic_reason_ru(deterministic_reason_ru: str, ai_reason_ru: str = "", audience_fit_ru: str = "") -> str:
    base = _short_topic_reason_part(deterministic_reason_ru, 145)
    extras = []
    ai = _short_topic_reason_part(ai_reason_ru, 115)
    fit = _short_topic_reason_part(audience_fit_ru, 115)
    if ai:
        extras.append(ai)
    if fit and fit.casefold() != ai.casefold():
        extras.append(fit)
    if extras:
        suffix = _short_topic_reason_part("; ".join(extras), 155)
        return f"{base}. AI-оценка: {suffix}." if base else f"AI-оценка: {suffix}."
    return deterministic_reason_ru


def _parse_topic_metadata_result_content(content: str) -> dict[str, str] | None:
    values = _parse_topic_metadata_fields(content)
    required = (
        "title_ru",
        "summary_ru",
        "angle_ru",
        "reason_ru",
        "ai_value_score",
        "ai_value_reason_ru",
        "audience_fit_ru",
    )
    if all(values.get(k) for k in required) and _parse_ai_value_score(values.get("ai_value_score")) is not None:
        return values
    return None


def _topic_enrichment_failure_status(diagnostics: dict[str, int] | None) -> str:
    diagnostics = diagnostics or {}
    if int(diagnostics.get("ai_provider_errors", 0) or 0):
        return "provider_error"
    if int(diagnostics.get("ai_output_truncated", 0) or 0):
        return "output_truncated"
    if int(diagnostics.get("ai_invalid_json", 0) or 0):
        return "invalid_json"
    if int(diagnostics.get("ai_invalid_fields", 0) or 0):
        return "invalid_fields"
    return "invalid_model_output"


async def _ensure_topic_candidate_display_metadata(
    topic_id: int, settings, db: DraftDatabase, *, force: bool = False, debug: bool = False,
) -> dict | None:
    """Enrich one opened topic immediately; unlike /collect this has no bulk limit."""
    topic = db.get_topic_candidate(topic_id)
    if not topic:
        return None
    weak = is_weak_topic_metadata(
        topic.get("title_ru"), topic.get("summary_ru"), topic.get("angle_ru"),
        original_title=topic.get("title"), reason_ru=topic.get("reason_ru"),
    )
    attempted = False
    error = None
    if force or weak:
        attempted = True
        if not settings or not getattr(settings, "has_ai_provider", False):
            error = "AI-провайдер не настроен."
        else:
            route = _resolve_ai_request(settings, "topic_enrich")
            if not route.api_key:
                error = "AI-ключ не настроен."
            else:
                try:
                    result = await _run_enrich_topic_understanding_ru(
                        api_key=route.api_key, model=route.model, title=str(topic.get("title") or ""),
                        source=str(topic.get("source") or ""), description=topic.get("original_description"),
                        base_url=route.base_url, extra_headers=route.extra_headers,
                        provider=route.provider, fallback=route.fallback,
                    )
                except Exception as exc:
                    logger.warning("On-demand topic enrichment failed: topic_id=%s error=%s", topic_id, exc)
                    result = None
                    error = str(exc)
                parsed = _parse_topic_metadata_fields(result.content) if result and result.content.strip() else {}
                if all(parsed.get(key) for key in ("title_ru", "summary_ru", "angle_ru")) and not is_weak_topic_metadata(
                    parsed.get("title_ru"), parsed.get("summary_ru"), parsed.get("angle_ru"), original_title=topic.get("title"),
                ):
                    db.force_update_topic_candidate_display_fields(
                        topic_id, title_ru=parsed["title_ru"], summary_ru=parsed["summary_ru"],
                        angle_ru=parsed["angle_ru"], reason_ru=str(topic.get("reason_ru") or ""),
                        metadata_source="ai_on_demand",
                    )
                    used_provider = result.provider or route.provider
                    estimated_cost = estimate_ai_cost(used_provider, result.prompt_tokens, result.completion_tokens, settings)
                    db.record_ai_usage(provider=used_provider, model=result.model or route.model, operation="topic_enrich_on_demand",
                        prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens, total_tokens=result.total_tokens,
                        estimated_cost_usd=estimated_cost, source_url=str(topic.get("url") or ""), draft_id=None)
                elif not error:
                    error = "Модель не вернула понятное русское объяснение."
    topic = db.get_topic_candidate(topic_id) or topic
    weak = is_weak_topic_metadata(topic.get("title_ru"), topic.get("summary_ru"), topic.get("angle_ru"), original_title=topic.get("title"), reason_ru=topic.get("reason_ru"))
    if not topic.get("metadata_source"):
        topic["metadata_source"] = "fallback" if weak else "ai_bulk"
    topic["_metadata_is_weak"] = weak
    topic["_ai_enrichment_attempted"] = attempted
    topic["_ai_enrichment_error"] = error
    topic["_show_metadata_diagnostics"] = debug
    return topic


async def _reenrich_topic_candidate_display_metadata(
    topic_id: int, settings, db: DraftDatabase,
) -> tuple[dict | None, str | None]:
    topic = await _ensure_topic_candidate_display_metadata(topic_id, settings, db, force=True)
    if not topic:
        return None, f"Тема #{topic_id} не найдена."
    error = topic.get("_ai_enrichment_error")
    return topic, str(error) if error else None


async def _enrich_topic_metadata_if_available(item, settings, db: DraftDatabase) -> str:
    if not settings or not getattr(settings, "has_ai_provider", False):
        logger.info("Topic metadata AI enrichment skipped: no AI provider url=%s source=%s", item.url, item.source)
        _apply_topic_enrichment_fallback(item, db)
        return "skipped_no_provider"
    topic = db.find_topic_candidate_by_url(item.url)
    db_title_ru = str(topic.get("title_ru") or "") if topic else ""
    db_summary_ru = str(topic.get("summary_ru") or "") if topic else ""
    db_angle_ru = str(topic.get("angle_ru") or "") if topic else ""
    existing_title_ru = db_title_ru or (item.title_ru or "")
    existing_summary_ru = db_summary_ru or (item.summary_ru or "")
    existing_angle_ru = db_angle_ru or (item.angle_ru or "")
    existing_reason_ru = (str(topic.get("reason_ru") or "") if topic else "") or (item.reason_ru or "")
    existing_is_complete = bool(existing_title_ru and existing_summary_ru and existing_angle_ru)
    existing_is_weak = is_weak_topic_metadata(
        existing_title_ru,
        existing_summary_ru,
        existing_angle_ru,
        original_title=item.title,
    )
    is_github_topic = (getattr(item, "source_group", "") or (str(topic.get("source_group") or "") if topic else "")) == "github"
    display_existing_matches_item = bool(
        existing_title_ru
        and existing_title_ru == (item.title_ru or "")
        and existing_summary_ru == (item.summary_ru or "")
        and existing_angle_ru == (item.angle_ru or "")
    )
    github_existing_matches_item = bool(is_github_topic and display_existing_matches_item)
    item_has_deterministic_fallback = bool(getattr(item, "_deterministic_fallback_used", False))
    if existing_is_complete and not existing_is_weak and not (display_existing_matches_item and item_has_deterministic_fallback):
        item.title_ru = existing_title_ru
        item.summary_ru = existing_summary_ru
        item.angle_ru = existing_angle_ru
        item.reason_ru = existing_reason_ru or item.reason_ru
        return "already_good"
    route = _resolve_ai_request(settings, "topic_enrich")
    if not route.api_key:
        logger.info("Topic metadata AI enrichment skipped: no AI provider api key url=%s source=%s", item.url, item.source)
        _apply_topic_enrichment_fallback(item, db)
        return "skipped_no_provider"
    diagnostics: dict[str, int] = {}
    try:
        result = await _run_enrich_topic_metadata_ru(
            api_key=route.api_key,
            model=route.model,
            title=item.title,
            source=item.source,
            description=getattr(item, "original_description", None),
            published_at=getattr(item, "published_at", None),
            source_group=getattr(item, "source_group", None),
            base_url=route.base_url,
            extra_headers=route.extra_headers,
            diagnostics=diagnostics,
            provider=route.provider,
            fallback=route.fallback,
        )
    except TypeError as exc:
        if "diagnostics" not in str(exc):
            logger.warning("Topic metadata AI enrichment failed: provider exception url=%s source=%s error=%s", item.url, item.source, exc)
            _apply_topic_enrichment_fallback(item, db, force=True)
            return "provider_error"
        result = await _run_enrich_topic_metadata_ru(
            api_key=route.api_key,
            model=route.model,
            title=item.title,
            source=item.source,
            description=getattr(item, "original_description", None),
            published_at=getattr(item, "published_at", None),
            source_group=getattr(item, "source_group", None),
            base_url=route.base_url,
            extra_headers=route.extra_headers,
            provider=route.provider,
            fallback=route.fallback,
        )
    except Exception as exc:
        logger.warning("Topic metadata AI enrichment failed: provider exception url=%s source=%s error=%s", item.url, item.source, exc)
        _apply_topic_enrichment_fallback(item, db, force=True)
        return "provider_error"
    setattr(item, "_ai_enrichment_diagnostics", diagnostics)
    if not result or not result.content.strip():
        logger.warning("Topic metadata AI enrichment failed: empty AI response url=%s source=%s", item.url, item.source)
        _apply_topic_enrichment_fallback(item, db, force=True)
        return _topic_enrichment_failure_status(diagnostics)
    parsed = _parse_topic_metadata_result_content(result.content)
    if parsed is None:
        logger.warning("Topic metadata AI enrichment failed: invalid metadata format url=%s source=%s", item.url, item.source)
        _apply_topic_enrichment_fallback(item, db, force=True)
        return _topic_enrichment_failure_status(diagnostics)
    title_ru = parsed["title_ru"]
    summary_ru = parsed["summary_ru"]
    angle_ru = parsed["angle_ru"]
    ai_score = _parse_ai_value_score(parsed.get("ai_value_score"))
    content_format = (parsed.get("content_format") or "").strip()[:40]
    ai_value_reason_ru = (parsed.get("ai_value_reason_ru") or "").strip()[:180]
    audience_fit_ru = (parsed.get("audience_fit_ru") or "").strip()[:180]
    deterministic_reason_ru = existing_reason_ru or item.reason_ru or parsed["reason_ru"]
    final_score = hybrid_topic_score(item.score, ai_score)
    reason_ru = (
        _combined_topic_reason_ru(
            deterministic_reason_ru,
            ai_value_reason_ru,
            audience_fit_ru,
        )
        if ai_score is not None
        else parsed["reason_ru"]
    )
    if not all([title_ru, summary_ru, angle_ru, reason_ru]) or is_weak_topic_metadata(
        title_ru, summary_ru, angle_ru, original_title=item.title
    ):
        logger.warning("Topic metadata AI enrichment failed: failed Russian usefulness validation url=%s source=%s", item.url, item.source)
        _apply_topic_enrichment_fallback(item, db, force=True)
        return "invalid_fields"
    if not topic:
        topic = db.find_topic_candidate_by_url(item.url)
    if not topic:
        logger.warning("Topic metadata AI enrichment failed: topic row missing after collection url=%s source=%s", item.url, item.source)
        _apply_topic_enrichment_fallback(item, db, force=True)
        return "provider_error"

    should_force_update = existing_is_weak or github_existing_matches_item
    if should_force_update:
        item.title_ru = title_ru
        item.summary_ru = summary_ru
        item.angle_ru = angle_ru
        item.reason_ru = reason_ru
        setattr(item, "ai_value_score", ai_score)
        setattr(item, "ai_value_reason_ru", ai_value_reason_ru)
        setattr(item, "audience_fit_ru", audience_fit_ru)
        setattr(item, "content_format", content_format)
        if ai_score is not None:
            item.score = final_score
        db.force_update_topic_candidate_display_fields(
            int(topic["id"]),
            title_ru=title_ru,
            summary_ru=summary_ru,
            angle_ru=angle_ru,
            reason_ru=reason_ru,
            score=final_score if ai_score is not None else None,
            content_format=content_format,
            ai_value_score=ai_score,
            ai_value_reason_ru=ai_value_reason_ru,
            audience_fit_ru=audience_fit_ru,
            metadata_source="ai_bulk",
        )
    else:
        item.title_ru = existing_title_ru or title_ru
        item.summary_ru = existing_summary_ru or summary_ru
        item.angle_ru = existing_angle_ru or angle_ru
        item.reason_ru = reason_ru if ai_score is not None else (existing_reason_ru or reason_ru)
        setattr(item, "ai_value_score", ai_score)
        setattr(item, "ai_value_reason_ru", ai_value_reason_ru)
        setattr(item, "audience_fit_ru", audience_fit_ru)
        setattr(item, "content_format", content_format)
        if ai_score is not None:
            item.score = final_score
        db.update_topic_candidate_display_fields(
            int(topic["id"]),
            title_ru=title_ru,
            summary_ru=summary_ru,
            angle_ru=angle_ru,
            reason_ru=reason_ru,
            score=final_score if ai_score is not None else None,
            content_format=content_format,
            ai_value_score=ai_score,
            ai_value_reason_ru=ai_value_reason_ru,
            audience_fit_ru=audience_fit_ru,
            metadata_source="ai_bulk",
        )
    used_provider = result.provider or route.provider
    estimated_cost = estimate_ai_cost(used_provider, result.prompt_tokens, result.completion_tokens, settings)
    db.record_ai_usage(
        provider=used_provider,
        model=result.model or route.model,
        operation="topic_enrich_metadata",
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        estimated_cost_usd=estimated_cost,
        source_url=item.url,
        draft_id=None,
    )
    return "enriched"


def _moderation_keyboard(
    draft_id: int,
    status: str | None = None,
    has_media: bool = False,
    source_url: str | None = None,
    source_image_url: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status == "scheduled":
        rows.extend(
            [
                [InlineKeyboardButton("👀 Показать пост", callback_data=f"preview:{draft_id}")],
                [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data=f"publish:{draft_id}")],
                [InlineKeyboardButton("↩️ Снять с очереди", callback_data=f"unschedule:{draft_id}")],
                [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{draft_id}")],
            ]
        )
        return InlineKeyboardMarkup(rows)
    if status == "published":
        return InlineKeyboardMarkup([[InlineKeyboardButton("👀 Показать пост", callback_data=f"preview:{draft_id}")]])
    if status == "failed":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("👀 Показать пост", callback_data=f"preview:{draft_id}")],
                [InlineKeyboardButton("🔁 Восстановить", callback_data=f"restore_draft:{draft_id}")],
            ]
        )
    if status in ACTIONABLE_DRAFT_STATUSES:
        rows.extend(
            [
                [InlineKeyboardButton("👀 Показать пост", callback_data=f"preview:{draft_id}")],
                [InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}")],
                [InlineKeyboardButton("🗓️ Запланировать", callback_data=f"schedule:{draft_id}")],
                [InlineKeyboardButton("📅 В ближайший слот", callback_data=f"schedule_nearest:{draft_id}")],
                [InlineKeyboardButton("✨ Улучшить", callback_data=f"polish:{draft_id}")],
                [
                    InlineKeyboardButton("🧹 Убрать воду", callback_data=f"rewrite_remove_fluff:{draft_id}"),
                    InlineKeyboardButton("📉 Сделать короче", callback_data=f"rewrite_shorten:{draft_id}"),
                ],
                [InlineKeyboardButton("😐 Без рекламного тона", callback_data=f"rewrite_neutralize_ads:{draft_id}")],
                [InlineKeyboardButton("✏️ Редактировать текст", callback_data=f"edit_text:{draft_id}")],
            ]
        )
        if source_url:
            rows.append([InlineKeyboardButton("🔗 Открыть источник", url=source_url)])
            rows.append([InlineKeyboardButton("♻️ Перегенерировать", callback_data=f"regenerate:{draft_id}")])
        if source_image_url and not has_media:
            rows.append([InlineKeyboardButton("🖼 Прикрепить картинку источника", callback_data=f"attach_source_image:{draft_id}")])
        if has_media:
            rows.append([InlineKeyboardButton("🗑 Убрать медиа", callback_data=f"remove_media:{draft_id}")])
        else:
            rows.append([InlineKeyboardButton("📎 Прикрепить медиа", callback_data=f"attach_media_flow:{draft_id}")])
        rows.append([InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{draft_id}")])
        return InlineKeyboardMarkup(rows)
    return InlineKeyboardMarkup([[InlineKeyboardButton("👀 Показать пост", callback_data=f"preview:{draft_id}")]])


def _moderation_keyboard_for_draft(draft_id: int, draft: dict[str, object]) -> InlineKeyboardMarkup:
    return _moderation_keyboard(
        draft_id,
        str(draft.get("status") or ""),
        has_media=media_count(draft.get("media_url"), draft.get("media_type")) > 0,
        source_url=draft.get("source_url"),
        source_image_url=draft.get("source_image_url"),
    )


def _preview_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("↩️ Вернуть обратно", callback_data=f"preview_back:{draft_id}")],
            [InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}")],
            [InlineKeyboardButton("🗓️ Запланировать", callback_data=f"schedule:{draft_id}")],
        ]
    )


def _build_moderation_text(
    draft_id: int,
    content: str,
    source_url: str | None = None,
    media_type: str | None = None,
    media_url: str | None = None,
    source_image_url: str | None = None,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None = None,
) -> str:
    source = source_url or "не указан"
    count = media_count(media_url, media_type)
    media = "нет"
    if count == 1:
        items = decode_media_items(media_url, media_type)
        media = items[0]["type"] if items else "нет"
    elif count > 1:
        media = f"{count} файлов"
    body = strip_quote_markers(content, custom_emoji_aliases=custom_emoji_aliases).strip() or "[пусто]"
    return (
        f"📝 Черновик #{draft_id}\n"
        f"Источник: {source}\n"
        f"Картинка источника: {'есть' if source_image_url else 'нет'}\n"
        f"Медиа: {media}\n\n"
        f"Пост:\n{body}\n\n"
        "Выбери действие:"
    )


def _is_media_callback_message(query) -> bool:
    message = query.message if query else None
    if not message:
        return False
    return bool(message.photo or message.video or message.animation or message.document or message.caption)


async def _edit_callback_message(
    query, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    try:
        if _is_media_callback_message(query):
            if len(text) <= TELEGRAM_CAPTION_LIMIT:
                await query.edit_message_caption(caption=text, reply_markup=reply_markup)
                return
            await query.edit_message_caption(
                caption="Готово. Полный текст отправил отдельным сообщением ниже.",
                reply_markup=reply_markup,
            )
            if query.message:
                await safe_reply_text(
                    query.message,
                    text,
                    link_preview_options=_disabled_link_preview_options(),
                )
            return
        await safe_edit_or_send_callback_message(
            query,
            text,
            reply_markup=reply_markup,
            link_preview_options=_disabled_link_preview_options(),
            logger=logger,
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            await _safe_answer_callback(query, "Уже показано.")
            return
        logger.warning("Failed to edit callback message: %s", exc)
        raise


async def _run_sources_status_background(context: ContextTypes.DEFAULT_TYPE, settings, db: DraftDatabase) -> None:
    await source_handlers.run_sources_status_background(context, settings, db, _run_collect_topics_with_diagnostics, _render_sources_status, _back_to_menu_keyboard, logger)


async def _run_source_test_background(context: ContextTypes.DEFAULT_TYPE, settings, db: DraftDatabase, row: dict[str, object]) -> None:
    await source_handlers.run_source_test_background(context, settings, db, row, _run_collect_topics_with_diagnostics, logger)


def _build_media_preview_caption(
    draft_id: int,
    content: str,
    source_url: str | None = None,
    media_type: str | None = None,
    source_image_url: str | None = None,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None = None,
) -> str:
    source = source_url or "не указан"
    media = media_type or "нет"
    body = strip_quote_markers(content, custom_emoji_aliases=custom_emoji_aliases).strip() or "[пусто]"
    snippet = body[:500]
    caption = (
        f"📝 Черновик #{draft_id}\n"
        f"Источник: {source}\n"
        f"Картинка источника: {'есть' if source_image_url else 'нет'}\n"
        f"Медиа: {media}\n\n"
        f"Пост:\n{snippet}"
    )
    if len(body) > len(snippet):
        caption += f"\n...\nПолный текст можно открыть через /draft_info {draft_id}"
    caption += "\n\nВыбери действие:"
    if len(caption) > SHORT_MEDIA_PREVIEW_LIMIT:
        caption = caption[: SHORT_MEDIA_PREVIEW_LIMIT - 1].rstrip() + "…"
    return caption



def _draft_snippet_text(draft: dict[str, object]) -> str:
    content = str(draft.get("content") or "")
    short = (content[:120] + "...") if len(content) > 120 else content
    source = str(draft.get("source_url") or "не указан")
    media_type = str(draft.get("media_type") or "нет")
    created_at = str(draft.get("created_at") or "")
    return (
        f"📝 #{draft['id']} | {draft['status']}\n"
        f"Источник: {source}\n"
        f"Медиа: {media_type}\n"
        f"Создан: {created_at}\n"
        f"Текст: {short}"
    )


def _draft_actions_keyboard(draft_id: int, status: str | None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"Открыть #{draft_id}", callback_data=f"draft_info:{draft_id}")]]
    if status in ACTIONABLE_DRAFT_STATUSES:
        rows.append([InlineKeyboardButton(f"Опубликовать #{draft_id}", callback_data=f"publish:{draft_id}")])
        rows.append([InlineKeyboardButton(f"Запланировать #{draft_id}", callback_data=f"schedule:{draft_id}")])
    return InlineKeyboardMarkup(rows)


def _full_draft_text(draft: dict[str, object]) -> str:
    media_url = draft.get("media_url")
    scheduled_at = draft.get("scheduled_at")
    return (
        f"📝 Черновик #{draft['id']}\n"
        f"Статус: {draft.get('status')}\n"
        f"Источник: {draft.get('source_url') or 'не указан'}\n"
        f"Картинка источника: {'есть' if draft.get('source_image_url') else 'нет'}\n"
        f"Тип медиа: {draft.get('media_type') or 'нет'}\n"
        f"URL медиа: {media_url if media_url else 'нет'}\n"
        f"Запланирован: {scheduled_at if scheduled_at else 'нет'}\n"
        f"Создан: {draft.get('created_at')}\n"
        f"Обновлён: {draft.get('updated_at')}\n\n"
        f"Текст:\n{draft.get('content') or ''}"
    )


def _extract_draft_id_from_text(message_text: str) -> int | None:
    marker = "Черновик #"
    if marker not in message_text:
        return None
    tail = message_text.split(marker, maxsplit=1)[1]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


TOPIC_METADATA_FALLBACK_NOTE = "Источник не удалось прочитать напрямую, поэтому черновик создан по сохранённому описанию темы. Проверь факты перед публикацией."


def _is_blocked_source_url(url: str) -> bool:
    try:
        hostname = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return hostname == "reddit.com" or hostname.endswith(".reddit.com")


def _should_use_topic_metadata_fallback(url: str, error: BaseException | None = None) -> bool:
    if _is_blocked_source_url(url):
        return True
    if error is None:
        return False
    message = str(error).casefold()
    fallback_markers = (
        "403 client error",
        "403",
        "401",
        "429",
        "blocked",
        "forbidden",
        "too little page text",
        "not enough text",
        "слишком мало полезного текста",
        "не содержит html",
        "non-html",
        "not html",
        "content-type",
    )
    return any(marker in message for marker in fallback_markers)


async def _generate_topic_metadata_fallback_draft(
    *,
    route: AIRequestRoute,
    settings,
    topic: dict[str, object],
) -> GenerationResult:
    return await _run_generate_post_draft_from_topic_metadata(
        api_key=route.api_key,
        model=route.model,
        topic_title=str(topic.get("title") or ""),
        topic_title_ru=str(topic.get("title_ru") or ""),
        topic_summary_ru=str(topic.get("summary_ru") or ""),
        topic_angle_ru=str(topic.get("angle_ru") or ""),
        topic_original_description=str(topic.get("original_description") or ""),
        topic_source=str(topic.get("source") or ""),
        topic_source_group=str(topic.get("source_group") or ""),
        topic_category=str(topic.get("category") or ""),
        source_url=str(topic.get("url") or ""),
        max_chars=settings.post_max_chars,
        soft_chars=settings.post_soft_chars,
        base_url=route.base_url,
        extra_headers=route.extra_headers,
        provider=route.provider,
        fallback=route.fallback,
    )


async def _generate_url_draft_with_fallback(
    *,
    route: AIRequestRoute,
    settings,
    source_url: str,
    title: str,
    page_text: str,
) -> tuple[GenerationResult, bool, str]:
    try:
        result = await _run_generate_post_draft_from_page(
            route.api_key,
            model=route.model,
            source_url=source_url,
            title=title,
            page_text=page_text,
            base_url=route.base_url,
            extra_headers=route.extra_headers,
            provider=route.provider,
            fallback=route.fallback,
        )
        return result, False, "draft_from_url"
    except EmptyAIResponseError as exc:
        logger.warning("Draft model returned empty content for URL %s: %s", source_url, exc)
        fallback_route = _resolve_ai_request(settings, "polish")
        if fallback_route.model and fallback_route.model != route.model:
            logger.warning("Trying fallback generation with polish model=%s", fallback_route.model)
            result = await _run_generate_post_draft_from_page(
                fallback_route.api_key,
                model=fallback_route.model,
                source_url=source_url,
                title=title,
                page_text=page_text,
                max_chars=settings.post_max_chars,
                soft_chars=settings.post_soft_chars,
                base_url=fallback_route.base_url,
                extra_headers=fallback_route.extra_headers,
                provider=fallback_route.provider,
                fallback=fallback_route.fallback,
            )
            return result, True, "fallback_draft_from_url"
        raise

async def _regenerate_draft_from_source(
    *,
    db: DraftDatabase,
    settings,
    draft: dict[str, object],
) -> tuple[str | None, str | None]:
    source_url = str(draft.get("source_url") or "").strip()
    draft_id = int(draft["id"])
    if not source_url:
        return None, "У этого черновика нет source_url, перегенерация недоступна."
    if not settings.has_ai_provider:
        return None, "AI-провайдер не настроен."

    try:
        details = await _run_fetch_page_content_details(source_url)
    except Exception as exc:
        logger.exception("Failed to fetch source for draft #%s from %s: %s", draft_id, source_url, exc)
        return None, "Не удалось снова прочитать источник. Возможно, сайт закрыл доступ или страница изменилась."

    try:
        route = _resolve_ai_request(settings, "draft")
        generation_result, _used_fallback, operation = await _generate_url_draft_with_fallback(
            route=route,
            settings=settings,
            source_url=source_url,
            title=details.title,
            page_text=details.text,
        )
    except EmptyAIResponseError:
        return None, EMPTY_AI_REPLY_TEXT.replace("Черновик не создан", "Черновик не обновлён")
    except Exception as exc:
        logger.exception("Failed to regenerate draft #%s from %s: %s", draft_id, source_url, exc)
        return None, "Не удалось перегенерировать черновик из источника. Ошибка записана в логи."

    content = generation_result.content.strip()
    if not content:
        return None, "Модель вернула пустой ответ. Черновик не обновлён. Попробуй ещё раз или смени MODEL_DRAFT."

    used_provider = generation_result.provider or route.provider
    estimated_cost = estimate_ai_cost(used_provider, generation_result.prompt_tokens, generation_result.completion_tokens, settings)
    db.record_ai_usage(
        provider=used_provider,
        model=generation_result.model or route.model,
        operation=operation,
        prompt_tokens=generation_result.prompt_tokens,
        completion_tokens=generation_result.completion_tokens,
        total_tokens=generation_result.total_tokens,
        estimated_cost_usd=estimated_cost,
        source_url=source_url,
        draft_id=draft_id,
    )
    db.update_draft_content(draft_id, content)
    if details.preview_image_url:
        db.update_draft_source_image_url(draft_id, details.preview_image_url)
    db.update_status(draft_id, "draft")
    return content, None





async def _send_moderation_preview(
    context: ContextTypes.DEFAULT_TYPE,
    admin_id: int,
    draft_id: int,
    content: str,
    source_url: str | None = None,
    media_url: str | None = None,
    media_type: str | None = None,
    source_image_url: str | None = None,
) -> None:
    settings = context.bot_data["settings"]
    custom_emoji_aliases = getattr(settings, "custom_emoji_aliases", {})
    text = _build_moderation_text(
        draft_id,
        content,
        source_url,
        media_type,
        media_url,
        source_image_url=source_image_url,
        custom_emoji_aliases=custom_emoji_aliases,
    )
    has_media = media_count(media_url, media_type) > 0
    keyboard = _moderation_keyboard(draft_id, status="draft", has_media=has_media, source_url=source_url, source_image_url=source_image_url)
    items = decode_media_items(media_url, media_type)
    if len(items) == 1:
        short_caption = _build_media_preview_caption(
            draft_id,
            content,
            source_url,
            media_type,
            source_image_url=source_image_url,
            custom_emoji_aliases=custom_emoji_aliases,
        )
        if items[0]["type"] == "photo":
            await context.bot.send_photo(chat_id=admin_id, photo=items[0]["file_id"], caption=short_caption, reply_markup=keyboard)
            return
        if items[0]["type"] == "video":
            await context.bot.send_video(chat_id=admin_id, video=items[0]["file_id"], caption=short_caption, reply_markup=keyboard)
            return
        if items[0]["type"] == "animation":
            await context.bot.send_animation(chat_id=admin_id, animation=items[0]["file_id"], caption=short_caption, reply_markup=keyboard)
            return
    await context.bot.send_message(
        chat_id=admin_id,
        text=text,
        reply_markup=keyboard,
        link_preview_options=_disabled_link_preview_options(),
    )
    if len(items) > 1:
        await context.bot.send_message(chat_id=admin_id, text=f"Прикреплено медиа: {len(items)}")


async def _handle_pending_text_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending_draft_id = _get_pending_edit(context)
    if pending_draft_id is None or not update.message:
        return False

    db: DraftDatabase = context.bot_data["db"]
    settings = context.bot_data["settings"]
    message_text = (update.message.text or "").strip()

    if not message_text:
        await update.message.reply_text("Пришли новый текст обычным текстовым сообщением.")
        return True

    draft = db.get_draft(pending_draft_id)
    if not draft:
        _clear_pending_edit(context)
        await update.message.reply_text(f"Черновик #{pending_draft_id} не найден. Редактирование отменено.")
        return True

    status = str(draft.get("status") or "")
    if not _can_edit(status):
        _clear_pending_edit(context)
        await update.message.reply_text(_status_guard_message("edit", status))
        return True

    if len(message_text) < 10:
        await update.message.reply_text(
            "Текст слишком короткий. Пришли нормальный текст поста или отмени редактирование."
        )
        return True
    if len(message_text) > settings.post_max_chars:
        await update.message.reply_text(
            f"Текст длиннее лимита {settings.post_max_chars} символов. Сократи его и отправь ещё раз."
        )
        return True

    db.update_draft_content(pending_draft_id, message_text)
    db.update_status(pending_draft_id, "draft")
    _clear_pending_edit(context)
    await update.message.reply_text(f"Готово. Текст черновика #{pending_draft_id} обновлён.")
    await _send_moderation_preview(
        context,
        settings.admin_id,
        pending_draft_id,
        message_text,
        source_url=draft.get("source_url"),
        media_url=draft.get("media_url"),
        media_type=draft.get("media_type"),
        source_image_url=draft.get("source_image_url"),
    )
    return True


async def _handle_pending_media_attach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending_draft_id = _get_pending_media(context)
    if pending_draft_id is None or not update.message:
        return False

    db: DraftDatabase = context.bot_data["db"]
    draft = db.get_draft(pending_draft_id)
    if not draft:
        _clear_pending_media(context)
        await update.message.reply_text(f"Черновик #{pending_draft_id} не найден. Прикрепление медиа отменено.")
        return True

    status = str(draft.get("status") or "")
    if not _can_edit(status):
        _clear_pending_media(context)
        await update.message.reply_text(_status_guard_message("edit", status))
        return True

    pending_items = context.user_data.setdefault("pending_media_items", [])
    media_type = None
    media_url = None
    if update.message.photo:
        media_type = "photo"
        media_url = update.message.photo[-1].file_id
    elif update.message.video:
        media_type = "video"
        media_url = update.message.video.file_id
    elif update.message.animation:
        media_type = "animation"
        media_url = update.message.animation.file_id
    elif update.message.document:
        await update.message.reply_text(
            "Пока поддерживаются только фото, видео и GIF/анимации, отправленные обычным сообщением."
        )
        return True

    if not media_type or not media_url:
        await update.message.reply_text("Сейчас я жду медиа. Пришли фото/видео/GIF или нажми «✅ Готово».")
        return True
    if len(pending_items) >= 10:
        await update.message.reply_text("Достигнут лимит: 10/10. Нажми «✅ Готово» или «❌ Отменить прикрепление».")
        return True
    pending_items.append({"type": media_type, "file_id": media_url})
    await update.message.reply_text(
        f"Добавлено медиа: {len(pending_items)}/10. Можешь прислать ещё или нажать «✅ Готово»."
    )
    return True


def _admin_reply_keyboard() -> InlineKeyboardMarkup:
    """Compatibility alias for the single inline navigation menu."""
    return _main_menu_keyboard()


async def _reply_admin_text(update: Update, text: str, **kwargs) -> None:
    if update.message:
        reply_markup = kwargs.pop("reply_markup", _admin_reply_keyboard())
        await update.message.reply_text(text, reply_markup=reply_markup, **kwargs)


async def _handle_navigation_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return True
    if not update.message:
        return True

    if text == NAV_PLAN_DAY:
        await _send_daily_plan(context=context, settings=settings, db=db, day_offset=0)
        return True
    if text == NAV_GENERATE_PLAN:
        await update.message.reply_text("Создаю черновики из плана...", reply_markup=_admin_reply_keyboard())
        summary = await _generate_drafts_from_plan(context=context, settings=settings, db=db, day_offset=0)
        has_created = bool(context.user_data.get("pending_plan_schedule_items")) and context.user_data.get("pending_plan_schedule_day") == 0
        await update.message.reply_text(summary, reply_markup=_generated_plan_keyboard(0, has_created))
        return True
    if text == NAV_QUEUE:
        await update.message.reply_text(_render_queue_text(db, settings, day_offset=0), reply_markup=_queue_keyboard(db, settings, 0))
        return True
    if text == NAV_DRAFTS:
        await drafts_command(update, context)
        return True
    if text == NAV_TOPICS:
        await topics_menu_command(update, context)
        return True
    if text == NAV_SOURCES:
        await _reply_admin_text(update, "📡 Источники\nВыбери действие:", reply_markup=_sources_hub_keyboard())
        return True
    if text == NAV_USAGE:
        await usage_today_command(update, context)
        return True
    if text == NAV_STYLE:
        await style_guide_command(update, context)
        return True
    if text == NAV_SETTINGS:
        await _reply_admin_text(update, _settings_text(settings), reply_markup=_settings_keyboard(), link_preview_options=_disabled_link_preview_options())
        return True
    if text == NAV_HELP:
        await _reply_admin_text(update, "Нажми кнопку ниже для навигации или используй /menu.\nТакже можешь прислать ссылку для черновика.")
        return True
    return False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow /start only for admin user."""

    settings = context.bot_data["settings"]
    user_id = update.effective_user.id if update.effective_user else None

    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа. Этот бот только для администратора.")
        return

    if update.message:
        await update.message.reply_text(
            "Обновляю управление ботом…",
            reply_markup=ReplyKeyboardRemove(),
        )
        await update.message.reply_text(
            "Привет 👋\n\nЭто единое меню бота. Выбери действие или просто пришли ссылку на материал.",
            reply_markup=_main_menu_keyboard(),
        )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа. Этот бот только для администратора.")
        return
    if update.message:
        await update.message.reply_text(
            "Убираю старую клавиатуру…",
            reply_markup=ReplyKeyboardRemove(),
        )
        await update.message.reply_text(
            _main_menu_text(),
            reply_markup=_main_menu_keyboard(),
        )



async def drafts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    status = context.args[0].strip().lower() if context.args else None
    if status and status not in ALLOWED_DRAFT_STATUSES:
        await update.message.reply_text(
            "Неизвестный статус. Доступные статусы: draft, approved, scheduled, publishing, published, rejected, failed"
        )
        return

    drafts = db.list_drafts(limit=10, status=status)
    if not drafts:
        await update.message.reply_text("Черновики не найдены.")
        return

    for draft in drafts:
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=_draft_snippet_text(draft),
            reply_markup=_draft_actions_keyboard(int(draft["id"]), str(draft.get("status") or "")),
            link_preview_options=_disabled_link_preview_options(),
        )


async def draft_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /draft_info <id>")
        return
    try:
        draft_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")
        return

    draft = db.get_draft(draft_id)
    if not draft:
        await update.message.reply_text(f"Черновик #{draft_id} не найден.")
        return

    reply_markup = _moderation_keyboard_for_draft(draft_id, draft)
    await update.message.reply_text(
        _full_draft_text(draft),
        reply_markup=reply_markup,
        link_preview_options=_disabled_link_preview_options(),
    )


async def delete_draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /delete_draft <id>")
        return
    try:
        draft_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")
        return

    draft = db.get_draft(draft_id)
    if not draft:
        await update.message.reply_text(f"Черновик #{draft_id} не найден.")
        return

    if draft.get("status") == "published":
        await update.message.reply_text("Нельзя удалить опубликованный черновик. Он остаётся в истории.")
        return

    deleted = db.delete_draft(draft_id)
    if deleted:
        await update.message.reply_text(f"Черновик #{draft_id} удалён.")
    else:
        await update.message.reply_text(f"Черновик #{draft_id} не найден.")


async def queue_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    if update.message:
        await update.message.reply_text(
            _render_queue_text(db, settings, day_offset=0),
            reply_markup=_queue_keyboard(db, settings, 0),
        )


async def queue_tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    if update.message:
        await update.message.reply_text(
            _render_queue_text(db, settings, day_offset=1),
            reply_markup=_queue_keyboard(db, settings, 1),
        )


async def _send_daily_plan(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    settings,
    db: DraftDatabase,
    day_offset: int,
    summary_query=None,
) -> None:
    day_name = "сегодня" if day_offset == 0 else "завтра"
    slots = _empty_slots_for_day(db, settings, day_offset)
    topics = _select_daily_plan_topics(db, len(slots))
    can_generate = bool(slots and topics)
    keyboard = _plan_summary_keyboard(day_offset, can_generate=can_generate)
    summary_text = _render_plan_text(day_name, slots, topics)
    if summary_query is not None:
        await _edit_callback_message(summary_query, summary_text, reply_markup=keyboard)
    else:
        await safe_send_message(
            context.bot,
            chat_id=settings.admin_id, text=summary_text, reply_markup=keyboard
        )
    for slot, topic in zip(slots, topics):
        await safe_send_message(
            context.bot,
            chat_id=settings.admin_id,
            text=truncate_telegram_text(f"🕒 Слот: {slot}\n\n{_topic_card_text(topic)}"),
            reply_markup=_topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or "")),
            link_preview_options=_disabled_link_preview_options(),
        )


async def plan_day_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    await _send_daily_plan(context=context, settings=settings, db=db, day_offset=0)


async def plan_tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    await _send_daily_plan(context=context, settings=settings, db=db, day_offset=1)


async def _create_draft_from_topic(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    settings,
    db: DraftDatabase,
    topic_id: int,
) -> tuple[int | None, str | None]:
    try:
        topic = db.get_topic_candidate(topic_id)
        if not topic:
            return None, f"Тема #{topic_id} не найдена."
        if str(topic.get("status") or "") != "new":
            return None, f"Тема #{topic_id} уже не новая."
        if not settings.has_ai_provider:
            return None, "ИИ-провайдер не настроен."
        route = _resolve_ai_request(settings, "draft")
        logger.info("topic_generate provider=%s model=%s", route.provider, route.model)
        source_url = str(topic.get("url") or "")
        details = None
        used_metadata_fallback = False
        operation = "topic_generate"
        try:
            if _is_blocked_source_url(source_url):
                raise RuntimeError("Blocked source URL: using saved topic metadata fallback")
            details = await _run_fetch_page_content_details(source_url)
            generation_result, used_fallback, _operation = await _generate_url_draft_with_fallback(
                route=route,
                settings=settings,
                source_url=source_url,
                title=details.title,
                page_text=details.text,
            )
        except Exception as fetch_or_generation_exc:
            if not _should_use_topic_metadata_fallback(source_url, fetch_or_generation_exc):
                raise
            logger.warning(
                "Using topic metadata fallback for topic_id=%s source_url=%s: %s",
                topic_id,
                source_url,
                fetch_or_generation_exc,
            )
            generation_result = await _generate_topic_metadata_fallback_draft(
                route=route,
                settings=settings,
                topic=topic,
            )
            used_fallback = False
            used_metadata_fallback = True
            operation = "generate_topic_metadata_fallback"
        if not generation_result.content.strip():
            if used_metadata_fallback and _is_blocked_source_url(source_url):
                return None, REDDIT_METADATA_EMPTY_REPLY_TEXT
            return None, EMPTY_AI_REPLY_TEXT
        source_image_url = details.preview_image_url if details else None
        new_draft_id = db.create_draft(
            generation_result.content,
            source_url=source_url,
            source_image_url=source_image_url,
        )
        used_provider = generation_result.provider or route.provider
        estimated_cost = estimate_ai_cost(used_provider, generation_result.prompt_tokens, generation_result.completion_tokens, settings)
        db.record_ai_usage(
            provider=used_provider,
            model=generation_result.model or route.model,
            operation=operation,
            prompt_tokens=generation_result.prompt_tokens,
            completion_tokens=generation_result.completion_tokens,
            total_tokens=generation_result.total_tokens,
            estimated_cost_usd=estimated_cost,
            source_url=source_url,
            draft_id=new_draft_id,
        )
        logger.info(
            "AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s",
            used_provider,
            generation_result.model or route.model,
            operation,
            generation_result.prompt_tokens,
            generation_result.completion_tokens,
            generation_result.total_tokens,
            estimated_cost,
        )
        db.update_topic_status(topic_id, "used")
        await _send_moderation_preview(
            context,
            settings.admin_id,
            new_draft_id,
            generation_result.content,
            source_url,
            source_image_url=source_image_url,
        )
        if used_fallback:
            logger.info(
                "Topic draft created with fallback model: topic_id=%s draft_id=%s source_url=%s",
                topic_id,
                new_draft_id,
                source_url,
            )
        if used_metadata_fallback:
            logger.info(
                "Topic draft created from metadata fallback: topic_id=%s draft_id=%s source_url=%s",
                topic_id,
                new_draft_id,
                source_url,
            )
            return new_draft_id, TOPIC_METADATA_FALLBACK_NOTE
        return new_draft_id, None
    except Exception as exc:
        logger.exception("Failed to create topic draft: topic_id=%s", topic_id)
        return None, f"Не удалось создать черновик из темы #{topic_id}: {exc}"


async def _generate_drafts_from_plan(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    settings,
    db: DraftDatabase,
    day_offset: int,
) -> str:
    _clear_pending_plan_schedule(context)
    day_name = "сегодня" if day_offset == 0 else "завтра"
    queue_hint = "/queue_today" if day_offset == 0 else "/queue_tomorrow"
    empty_slots = _empty_slots_for_day(db, settings, day_offset)
    if not empty_slots:
        return f"На {day_name} все слоты уже заняты. Проверь {queue_hint}"
    topics = _select_daily_plan_topics(db, len(empty_slots))
    if not topics:
        return "Нет подходящих тем. Запусти /collect_debug или /topics_hot"
    seen_topic_ids: set[int] = set()
    ok_lines: list[str] = []
    error_lines: list[str] = []
    pending_items: list[dict[str, int | str]] = []
    created_count = 0
    for slot, topic in zip(empty_slots, topics):
        topic_id = int(topic["id"])
        if topic_id in seen_topic_ids:
            error_lines.append(f"{slot} - тема #{topic_id}: дубликат темы в плане")
            continue
        seen_topic_ids.add(topic_id)
        new_draft_id, error = await _create_draft_from_topic(
            context=context, settings=settings, db=db, topic_id=topic_id
        )
        if new_draft_id is not None:
            created_count += 1
            pending_items.append({"slot": slot, "draft_id": new_draft_id, "topic_id": topic_id})
            ok_lines.append(f"{slot} - черновик #{new_draft_id} из темы #{topic_id}")
        else:
            error_lines.append(f"{slot} - тема #{topic_id}: {error or 'неизвестная ошибка'}")
    if pending_items:
        context.user_data["pending_plan_schedule_day"] = day_offset
        context.user_data["pending_plan_schedule_items"] = pending_items
    else:
        context.user_data.pop("pending_plan_schedule_day", None)
        context.user_data.pop("pending_plan_schedule_items", None)

    lines = ["🧩 Черновики из плана созданы", "", f"День: {day_name}", f"Создано: {created_count}", f"Ошибок: {len(error_lines)}", ""]
    if ok_lines:
        lines.extend(["Готово:", *ok_lines, ""])
    if error_lines:
        lines.extend(["Ошибки:", *error_lines, ""])
    lines.extend(["Дальше:", 'проверь черновики и поставь их в очередь через кнопки "🗓️ Запланировать".'])
    return "\n".join(lines).rstrip()


def _scheduled_at_for_slot(day_offset: int, slot: str, timezone_str: str) -> str:
    start_local, _end_local = _get_day_range(day_offset, timezone_str)
    slot_hour, slot_minute = slot.split(":", 1)
    local_dt = start_local.replace(hour=int(slot_hour), minute=int(slot_minute), second=0, microsecond=0)
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _schedule_generated_plan(*, context, settings, db, day_offset: int) -> str:
    pending_day = context.user_data.get("pending_plan_schedule_day")
    pending_items = context.user_data.get("pending_plan_schedule_items")
    if pending_day != day_offset or not pending_items:
        generate_hint = "/generate_plan_day" if day_offset == 0 else "/generate_plan_tomorrow"
        return f"Нет свежего плана для постановки в очередь. Сначала запусти {generate_hint}"

    day_name = "сегодня" if day_offset == 0 else "завтра"
    queue_hint = "/queue_today" if day_offset == 0 else "/queue_tomorrow"
    empty_slots = set(_empty_slots_for_day(db, settings, day_offset))
    ok_lines: list[str] = []
    error_lines: list[str] = []
    scheduled_count = 0

    for item in pending_items:
        slot = str(item.get("slot") or "")
        draft_id = int(item.get("draft_id"))
        if slot not in empty_slots:
            error_lines.append(f"{slot} - слот уже занят")
            continue
        draft = db.get_draft(draft_id)
        if not draft:
            error_lines.append(f"{slot} - черновик #{draft_id} не найден")
            continue
        status = str(draft.get("status") or "")
        if status not in {"draft", "approved"}:
            error_lines.append(f"{slot} - черновик #{draft_id} уже не черновик")
            continue
        scheduled_at_utc = _scheduled_at_for_slot(day_offset, slot, settings.schedule_timezone)
        if not db.schedule_draft(draft_id, scheduled_at_utc):
            error_lines.append(f"{slot} - слот уже занят или статус черновика изменился")
            empty_slots.discard(slot)
            continue
        empty_slots.remove(slot)
        scheduled_count += 1
        ok_lines.append(f"{slot} - черновик #{draft_id}")

    if scheduled_count > 0:
        context.user_data.pop("pending_plan_schedule_day", None)
        context.user_data.pop("pending_plan_schedule_items", None)

    lines = ["📅 Черновики поставлены в очередь", "", f"День: {day_name}", f"Запланировано: {scheduled_count}", f"Ошибок: {len(error_lines)}", ""]
    if ok_lines:
        lines.extend(["Готово:", *ok_lines, ""])
    if error_lines:
        lines.extend(["Ошибки:", *error_lines, ""])
    lines.append(f"Проверь: {queue_hint}")
    return "\n".join(lines).rstrip()


async def generate_plan_day_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    if update.message:
        await update.message.reply_text("Создаю черновики из плана...")
        summary = await _generate_drafts_from_plan(context=context, settings=settings, db=db, day_offset=0)
        has_created = bool(context.user_data.get("pending_plan_schedule_items")) and context.user_data.get("pending_plan_schedule_day") == 0
        await update.message.reply_text(summary, reply_markup=_generated_plan_keyboard(0, has_created))


async def generate_plan_tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    if update.message:
        await update.message.reply_text("Создаю черновики из плана...")
        summary = await _generate_drafts_from_plan(context=context, settings=settings, db=db, day_offset=1)
        has_created = bool(context.user_data.get("pending_plan_schedule_items")) and context.user_data.get("pending_plan_schedule_day") == 1
        await update.message.reply_text(summary, reply_markup=_generated_plan_keyboard(1, has_created))


async def schedule_generated_plan_day_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    if update.message:
        summary = await _schedule_generated_plan(context=context, settings=settings, db=db, day_offset=0)
        await update.message.reply_text(summary, reply_markup=_admin_reply_keyboard())


async def schedule_generated_plan_tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    if update.message:
        summary = await _schedule_generated_plan(context=context, settings=settings, db=db, day_offset=1)
        await update.message.reply_text(summary, reply_markup=_admin_reply_keyboard())


async def unschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /unschedule <draft_id>")
        return
    try:
        draft_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("draft_id должен быть числом.")
        return
    draft = db.get_draft(draft_id)
    if not draft:
        await update.message.reply_text(f"Черновик #{draft_id} не найден.")
        return
    if draft.get("status") != "scheduled":
        await update.message.reply_text(f"Черновик #{draft_id} сейчас не в очереди.")
        return
    db.unschedule_draft(draft_id)
    await update.message.reply_text(f"Черновик #{draft_id} снят с очереди и снова доступен как черновик.")


async def restore_draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /restore_draft <draft_id>")
        return
    try:
        draft_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("draft_id должен быть числом.")
        return
    draft = db.get_draft(draft_id)
    if not draft:
        await update.message.reply_text(f"Черновик #{draft_id} не найден.")
        return
    if draft.get("status") != "failed":
        await update.message.reply_text(
            f"Черновик #{draft_id} нельзя восстановить из статуса {draft.get('status')}. "
            "Восстановление доступно только для failed, чтобы не создать дубли публикаций."
        )
        return
    if not db.restore_draft(draft_id):
        await update.message.reply_text(
            f"Черновик #{draft_id} уже не в статусе failed и не был восстановлен."
        )
        return
    await update.message.reply_text(f"Черновик #{draft_id} возвращён в черновики. Проверь его перед новой публикацией.")


async def failed_drafts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    drafts = db.list_drafts(limit=10, status="failed")
    if not drafts:
        await update.message.reply_text("Нет черновиков в статусе failed.")
        return
    await context.bot.send_message(
        chat_id=settings.admin_id,
        text=_render_failed_drafts_text(drafts),
        reply_markup=_failed_drafts_keyboard(drafts),
        link_preview_options=_disabled_link_preview_options(),
    )


async def attach_media_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    if len(context.args) < 3:
        await update.message.reply_text("Использование: /attach_media <draft_id> <photo|video|animation> <media_url>")
        return

    try:
        draft_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("draft_id должен быть числом.")
        return

    media_type = context.args[1].lower().strip()
    media_url = " ".join(context.args[2:]).strip()
    if media_type not in ALLOWED_MEDIA_TYPES:
        await update.message.reply_text("media_type должен быть одним из: photo, video, animation.")
        return
    if not media_url:
        await update.message.reply_text("media_url не может быть пустым.")
        return

    draft = db.get_draft(draft_id)
    if not draft:
        await update.message.reply_text(f"Черновик #{draft_id} не найден.")
        return

    db.attach_media(draft_id, media_url, media_type)
    await update.message.reply_text(f"Медиа добавлено к черновику #{draft_id}: {media_type}.")


async def draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a test draft and send moderation message to admin."""

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None

    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    content = create_test_draft()
    draft_id = db.create_draft(content)

    await _send_moderation_preview(context, settings.admin_id, draft_id, content)

    if update.message:
        await update.message.reply_text(f"Тестовый черновик #{draft_id} создан и отправлен на модерацию.")


async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate an OpenAI-powered draft and send moderation message to admin."""

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None

    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    source_url_arg = " ".join(context.args).strip() if context.args else None
    await _generate_from_command(context, settings, db, source_url_arg, update.message)


async def _generate_from_command(context, settings, db: DraftDatabase, source_url_arg: str | None, message) -> None:

    if not settings.has_ai_provider:
        if message:
            await message.reply_text("AI-провайдер не настроен. Добавь OPENROUTER_API_KEY или OPENAI_API_KEY и перезапусти бота.")
        return

    try:
        route = _resolve_ai_request(settings, "draft")
        logger.info("/generate provider=%s model=%s", route.provider, route.model)
        source_url = None
        source_image_url = None
        if source_url_arg:
            source_url_raw = find_first_url(source_url_arg)
            if not source_url_raw:
                if message:
                    await message.reply_text("Не вижу корректной ссылки. Пришли URL в формате https://...")
                return
            source_url = normalize_url(source_url_raw)
            duplicate = db.find_by_source_url(source_url)
            if duplicate:
                if message:
                    await message.reply_text(
                        f"Похоже, эта ссылка уже обрабатывалась: черновик #{duplicate['id']} (статус: {duplicate['status']}).",
                        link_preview_options=_disabled_link_preview_options(),
                    )
                return
            if message:
                await message.reply_text("Нашёл ссылку. Читаю страницу и готовлю черновик...")
            details = await _run_fetch_page_content_details(source_url)
            source_image_url = details.preview_image_url
            generation_result, used_fallback, operation = await _generate_url_draft_with_fallback(
                route=route,
                settings=settings,
                source_url=source_url,
                title=details.title,
                page_text=details.text,
            )
        else:
            if message:
                await message.reply_text("Генерирую черновик...")
            generation_result = await _run_generate_post_draft(
                route.api_key,
                model=route.model,
                source_url=None,
                max_chars=settings.post_max_chars,
                soft_chars=settings.post_soft_chars,
                base_url=route.base_url,
                extra_headers=route.extra_headers,
                provider=route.provider,
                fallback=route.fallback,
            )
            used_fallback = False
            operation = "draft"
    except EmptyAIResponseError:
        if message:
            await message.reply_text(EMPTY_AI_REPLY_TEXT)
        return
    except Exception as exc:
        logger.exception("Error during generation: %s", exc)
        if message:
            if source_url_arg:
                await message.reply_text(
                    "Не удалось нормально прочитать страницу. Возможно, там мало текста, сайт закрыл доступ или страница требует JavaScript. Попробуй другую ссылку или пришли текст новости вручную."
                )
            else:
                await message.reply_text("Не удалось сгенерировать черновик. Попробуй ещё раз.")
        return

    content = generation_result.content
    if not content.strip():
        if message:
            await message.reply_text(EMPTY_AI_REPLY_TEXT)
        return
    draft_id = db.create_draft(content, source_url=source_url, source_image_url=source_image_url)
    used_provider = generation_result.provider or route.provider
    estimated_cost = estimate_ai_cost(
        used_provider,
        generation_result.prompt_tokens,
        generation_result.completion_tokens,
        settings,
    )
    db.record_ai_usage(
        provider=used_provider,
        model=generation_result.model or route.model,
        operation=operation,
        prompt_tokens=generation_result.prompt_tokens,
        completion_tokens=generation_result.completion_tokens,
        total_tokens=generation_result.total_tokens,
        estimated_cost_usd=estimated_cost,
        source_url=source_url,
        draft_id=draft_id,
    )
    logger.info(
        "AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s",
        used_provider,
        generation_result.model or route.model,
        operation,
        generation_result.prompt_tokens,
        generation_result.completion_tokens,
        generation_result.total_tokens,
        estimated_cost,
    )
    await _send_moderation_preview(
        context,
        settings.admin_id,
        draft_id,
        content,
        source_url,
        source_image_url=source_image_url,
    )

    if message:
        await message.reply_text(f"Черновик #{draft_id} создан и отправлен на модерацию.")
        if source_url and used_fallback:
            logger.info(
                "Draft created with fallback model: draft_id=%s source_url=%s",
                draft_id,
                source_url,
            )





def _topic_candidate_keys(item) -> list[str]:
    keys: list[str] = []
    for attr in ("id", "url", "canonical_key", "normalized_title", "title"):
        raw_value = item.get(attr, "") if isinstance(item, dict) else getattr(item, attr, "")
        value = str(raw_value or "").strip().casefold()
        if value:
            keys.append(f"{attr}:{value}")
    if not any(key.startswith("canonical_key:") for key in keys):
        title = str((item.get("title", "") if isinstance(item, dict) else getattr(item, "title", "")) or "")
        source_group = str((item.get("source_group", "") if isinstance(item, dict) else getattr(item, "source_group", "")) or "")
        canonical = canonical_topic_key(title, source_group).strip().casefold()
        if canonical:
            keys.append(f"canonical_key:{canonical}")
    return keys or [f"object:{id(item)}"]


def _dedupe_topic_items_by_identity(candidates: list) -> list:
    seen: set[str] = set()
    unique: list = []
    for item in candidates:
        keys = _topic_candidate_keys(item)
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        unique.append(item)
    return unique


def select_topic_ai_enrichment_candidates(candidates: list, limit: int) -> tuple[list, int]:
    """Select the highest-value unique topic-card enrichment candidates.

    Pure helper: no DB/network access. It deduplicates by URL/canonical-ish key and
    preserves the caller's final-preview ranking. Callers should pass already ranked
    preview candidates, not raw collection items.
    """
    unique = _dedupe_topic_items_by_identity(candidates)
    if limit <= 0:
        return [], len(unique)
    return unique[:limit], max(0, len(unique) - limit)


def _is_ai_enriched_topic(topic: object) -> bool:
    value = topic.get("ai_value_score") if isinstance(topic, dict) else getattr(topic, "ai_value_score", None)
    return _parse_ai_value_score(value) is not None


def _topic_ai_marker(topic: object) -> str:
    return "[AI]" if _is_ai_enriched_topic(topic) else "[fallback]"


def _collect_preview_candidates(inserted: list, accepted_items: list) -> list:
    """Return and mark the exact ranked candidate pool used by /collect preview sections."""
    top_new = sorted(inserted, key=lambda i: int(getattr(i, "score", 0) or 0), reverse=True)[:5]
    lively = [
        i
        for i in sorted(accepted_items, key=lambda i: int(getattr(i, "score", 0) or 0), reverse=True)
        if int(getattr(i, "score", 0) or 0) >= 50
        and (
            getattr(i, "source_group", "") in {"community", "github", "x", "tools", "custom"}
            or getattr(i, "category", "") in {"drama", "meme", "guide", "creator"}
        )
    ][:5]
    for index, item in enumerate(top_new):
        setattr(item, "_collect_preview_top_new", True)
        setattr(item, "_collect_preview_top_new_order", index)
    for index, item in enumerate(lively):
        setattr(item, "_collect_preview_lively", True)
        setattr(item, "_collect_preview_lively_order", index)
    return _dedupe_topic_items_by_identity([*top_new, *lively])


def _short_debug_topic_label(item) -> str:
    score = int(getattr(item, "score", 0) or 0)
    title = str(getattr(item, "title", "") or getattr(item, "title_ru", "") or "").strip()
    if len(title) > 72:
        title = title[:71].rstrip() + "…"
    topic_id = getattr(item, "id", None)
    prefix = f"#{topic_id} " if topic_id else ""
    return f"- {prefix}{score} {getattr(item, 'source', '')}: {title}"


def _topic_ai_zero_reason_ru(stats: TopicCollectStats) -> str:
    if stats.ai_enriched > 0:
        return ""
    if stats.ai_enrichment_skipped_no_provider:
        return "AI не запускался: не настроен провайдер (OPENROUTER_API_KEY или OPENAI_API_KEY)."
    if stats.ai_enrich_limit <= 0 and (stats.new or stats.existing or stats.merged_story):
        return "AI не запускался: TOPIC_AI_ENRICH_LIMIT=0."
    if stats.ai_enrichment_skipped_no_candidates:
        return "AI не запускался: нет подходящих тем для обогащения."
    if stats.ai_enrichment_skipped_existing_metadata:
        return "AI не запускался: у подходящих тем уже есть хорошие русские метаданные."
    model = stats.ai_enrichment_model or "MODEL_TOPIC_ENRICH"
    attempts = stats.ai_enrichment_attempted or stats.ai_enrichment_failed
    if stats.ai_enrichment_provider_error:
        return f"AI не сработал: {attempts} попыток, {stats.ai_enrichment_provider_error} provider errors. Модель: {model}. Проверь MODEL_TOPIC_ENRICH или ключ провайдера."
    if stats.ai_enrichment_output_truncated:
        return f"AI не сработал: {attempts} попыток, {stats.ai_enrichment_output_truncated} truncated output. Модель: {model}. Ответ модели упёрся в лимит токенов."
    if stats.ai_enrichment_invalid_json:
        return f"AI не сработал: {attempts} попыток, {stats.ai_enrichment_invalid_json} invalid JSON. Модель: {model}. Проверь MODEL_TOPIC_ENRICH или включи JSON-compatible model."
    if stats.ai_enrichment_invalid_fields:
        return f"AI не сработал: {attempts} попыток, {stats.ai_enrichment_invalid_fields} invalid fields. Модель: {model}. Проверь MODEL_TOPIC_ENRICH или включи JSON-compatible model."
    if stats.ai_enrichment_invalid_model_output:
        return f"AI не сработал: {attempts} попыток, {stats.ai_enrichment_invalid_model_output} invalid JSON/fields. Модель: {model}. Проверь MODEL_TOPIC_ENRICH или включи JSON-compatible model."
    if stats.ai_enrichment_failed:
        return f"AI не сработал: {attempts} попыток, обогащение завершилось ошибкой, использован fallback. Модель: {model}."
    return "AI не запускался: нет подходящих тем для обогащения."


async def _collect_topics_with_stats(db: DraftDatabase, items: list | None = None, settings=None) -> tuple[TopicCollectStats, list, list]:
    total_started = time.monotonic()
    source_started = time.monotonic()
    if items is None:
        items = await _run_collect_topics(settings=settings, db=db)
    source_seconds = time.monotonic() - source_started if items is not None else 0.0

    stats = TopicCollectStats(total=len(items), source_seconds=source_seconds)
    stats.skipped_examples = {"low_score": [], "stale": [], "spam": [], "invalid": []}
    inserted = []
    accepted_items = []
    borderline_items = []
    skipped_existing_metadata = 0
    max_topic_age_days = int(getattr(settings, "max_topic_age_days", 14) or 14)

    def _remember_skip(kind: str, item, reason: str) -> None:
        if not stats.skipped_examples:
            return
        bucket = stats.skipped_examples.get(kind)
        if bucket is None or len(bucket) >= 3:
            return
        title = (getattr(item, "title", "") or "").strip()
        source = (getattr(item, "source", "") or "unknown").strip()
        score = getattr(item, "score", None)
        score_part = f"{int(score)}" if isinstance(score, int) else "—"
        short_title = title[:85] + ("…" if len(title) > 85 else "")
        bucket.append(f"- {source} | {score_part} | {short_title} | {reason}")

    store_started = time.monotonic()
    for item in items:
        if len(item.title.strip()) < 8 or not item.url.strip() or not item.normalized_title.strip():
            stats.invalid += 1
            _remember_skip("invalid", item, "пустой title/url")
            continue
        if _is_stale_topic(item, max_topic_age_days):
            stats.stale += 1
            _remember_skip("stale", item, "старее лимита")
            continue
        if not getattr(item, "published_at", None):
            stats.missing_date += 1
        if re.search(r"\b(?:casino|porn|xxx|viagra|airdrop)\b|\btoken\s+presale\b", item.title, re.IGNORECASE):
            stats.spam += 1
            _remember_skip("spam", item, "спам-слова в title")
            continue
        # Scores below 35 are too weak to justify an AI call. Borderline 35-49
        # candidates are stored temporarily so AI can correct false negatives;
        # unrescued new rows are removed after the review pass.
        if item.score < 35:
            stats.low_score += 1
            stats.low_quality += 1
            _remember_skip("low_score", item, item.reason or "score < 35")
            continue
        is_borderline = item.score < 50
        needs_ai_enrichment_initial = is_weak_topic_metadata(item.title_ru, item.summary_ru, item.angle_ru, original_title=item.title)
        if needs_ai_enrichment_initial:
            if _apply_topic_enrichment_fallback(item, db):
                stats.deterministic_fallback_used += 1
        result = db.upsert_topic_candidate_with_reason(
            item.title, item.url, item.source, item.published_at, item.category, item.score, item.reason, item.normalized_title, item.source_group, item.title_ru, item.summary_ru, item.angle_ru, item.reason_ru, item.original_description
        )
        if result == "inserted":
            if not is_borderline:
                stats.new += 1
                inserted.append(item)
        elif result == "existing_url":
            stats.existing += 1
        elif result == "merged_story":
            stats.merged_story += 1
        else:
            stats.near_duplicate += 1
        stored_topic = db.find_topic_candidate_by_url(item.url)
        if stored_topic:
            setattr(item, "id", stored_topic.get("id"))
            setattr(item, "canonical_key", stored_topic.get("canonical_key") or canonical_topic_key(item.title, item.source_group))
            setattr(item, "ai_value_score", stored_topic.get("ai_value_score"))
            setattr(item, "ai_value_reason_ru", stored_topic.get("ai_value_reason_ru"))
            setattr(item, "audience_fit_ru", stored_topic.get("audience_fit_ru"))
            setattr(item, "content_format", stored_topic.get("content_format"))
        setattr(item, "_collection_result", result)

        # A lower-quality related URL was attached to a stronger primary story.
        # Do not render or AI-enrich the secondary item's mismatched metadata.
        if stored_topic and str(stored_topic.get("url") or "") != item.url:
            setattr(item, "_related_source_only", True)
            continue

        if is_borderline:
            borderline_items.append(item)
            continue

        if result in {"inserted", "existing_url", "merged_story"}:
            setattr(item, "_accepted_for_preview", True)
            accepted_items.append(item)
            stored_title_ru = str(stored_topic.get("title_ru") or "") if stored_topic else (item.title_ru or "")
            stored_summary_ru = str(stored_topic.get("summary_ru") or "") if stored_topic else (item.summary_ru or "")
            stored_angle_ru = str(stored_topic.get("angle_ru") or "") if stored_topic else (item.angle_ru or "")
            if not is_weak_topic_metadata(stored_title_ru, stored_summary_ru, stored_angle_ru, original_title=item.title):
                skipped_existing_metadata += 1
    stats.store_seconds = time.monotonic() - store_started
    stats.ai_enrichment_skipped_existing_metadata = skipped_existing_metadata

    enrich_limit = int(getattr(settings, "topic_ai_enrich_limit", 8) or 0)
    stats.ai_enrich_limit = max(0, min(30, enrich_limit))
    stats.ai_enrichment_model = _topic_enrich_model(settings) if settings else ""
    ai_started = time.monotonic()
    regular_preview_candidates = _collect_preview_candidates(inserted, accepted_items)
    borderline_ranked = sorted(borderline_items, key=lambda item: int(getattr(item, "score", 0) or 0), reverse=True)
    # Reserve at most three calls for possible false-negative rescue; keep the
    # rest of the budget for metadata on the strongest cards the admin will see.
    preview_candidates = _dedupe_topic_items_by_identity(
        [*borderline_ranked[:3], *regular_preview_candidates, *borderline_ranked[3:]]
    )
    selected_candidates, skipped_by_limit_count = select_topic_ai_enrichment_candidates(preview_candidates, stats.ai_enrich_limit)
    stats.ai_enrichment_selected = [_short_debug_topic_label(item) for item in selected_candidates]
    if not preview_candidates:
        stats.ai_enrichment_skipped_no_candidates = 1
    elif not (settings and getattr(settings, "has_ai_provider", False)):
        stats.ai_enrichment_skipped_no_provider = len(preview_candidates)
        if preview_candidates:
            logger.info("Topic metadata AI enrichment skipped: no AI provider for %s final preview candidates; deterministic fallback is used", len(preview_candidates))
    elif stats.ai_enrich_limit <= 0:
        stats.ai_enrichment_skipped_limit = len(preview_candidates)
        if preview_candidates:
            logger.info("Topic metadata AI enrichment skipped: enrichment limit reached/disabled for %s final preview candidates", len(preview_candidates))
    else:
        stats.ai_enrichment_skipped_limit = skipped_by_limit_count
        if skipped_by_limit_count:
            logger.info("Topic metadata AI enrichment skipped: enrichment limit reached for %s candidates", skipped_by_limit_count)
        for item in selected_candidates:
            try:
                before = (item.title_ru, item.summary_ru, item.angle_ru, item.reason_ru)
                stats.ai_enrichment_attempted += 1
                status = await _enrich_topic_metadata_if_available(item, settings, db)
                diagnostics = getattr(item, "_ai_enrichment_diagnostics", {}) or {}
                stats.ai_enrichment_json_mode_unsupported += int(diagnostics.get("ai_json_mode_unsupported", 0) or 0)
                stats.ai_enrichment_output_truncated += int(diagnostics.get("ai_output_truncated", 0) or 0)
                after = (item.title_ru, item.summary_ru, item.angle_ru, item.reason_ru)
                if status == "enriched" or (status is None and after != before and any(after)):
                    stats.ai_enriched += 1
                elif status == "failed":
                    stats.ai_enrichment_failed += 1
                    stats.ai_enrichment_invalid_model_output += 1
                    stats.ai_enrichment_invalid_fields += 1
                    if after != before:
                        stats.deterministic_fallback_used += 1
                elif status == "provider_error":
                    stats.ai_enrichment_failed += 1
                    stats.ai_enrichment_provider_error += 1
                    if after != before:
                        stats.deterministic_fallback_used += 1
                elif status == "invalid_json":
                    stats.ai_enrichment_failed += 1
                    stats.ai_enrichment_invalid_model_output += 1
                    stats.ai_enrichment_invalid_json += 1
                    if after != before:
                        stats.deterministic_fallback_used += 1
                elif status == "output_truncated":
                    stats.ai_enrichment_failed += 1
                    stats.ai_enrichment_invalid_model_output += 1
                    if after != before:
                        stats.deterministic_fallback_used += 1
                elif status in {"invalid_model_output", "invalid_fields"}:
                    stats.ai_enrichment_failed += 1
                    stats.ai_enrichment_invalid_model_output += 1
                    stats.ai_enrichment_invalid_fields += 1
                    if after != before:
                        stats.deterministic_fallback_used += 1
                elif status == "already_good":
                    stats.ai_enrichment_skipped_existing_metadata += 1
                    stats.ai_enrichment_attempted -= 1
                elif status == "skipped_no_provider":
                    stats.ai_enrichment_skipped_no_provider += 1
                    stats.ai_enrichment_attempted -= 1
            except Exception as exc:
                stats.ai_enrichment_failed += 1
                stats.ai_enrichment_provider_error += 1
                logger.warning("Topic enrichment skipped after exception from provider/fallback: %s", exc)
                continue

    # Finalize the temporary review pool only after AI has had a chance to move
    # the score. Newly inserted candidates that remain weak are deleted so they
    # can be reconsidered on a later collection instead of polluting /topics.
    for item in borderline_items:
        if int(getattr(item, "score", 0) or 0) >= 50:
            setattr(item, "_accepted_for_preview", True)
            accepted_items.append(item)
            if getattr(item, "_collection_result", "") == "inserted":
                stats.new += 1
                inserted.append(item)
        else:
            stats.low_score += 1
            stats.low_quality += 1
            _remember_skip("low_score", item, item.reason or "score < 50 after editorial review")
            if getattr(item, "_collection_result", "") == "inserted" and getattr(item, "id", None):
                db.delete_topic_candidate(int(item.id))

    _collect_preview_candidates(inserted, accepted_items)
    stats.ai_seconds = time.monotonic() - ai_started
    stats.total_seconds = time.monotonic() - total_started
    return stats, items, inserted


def _render_sources_status(reports: list[SourceReport], db: DraftDatabase | None = None) -> str:
    return source_handlers.render_sources_status(reports, db, SOURCE_GROUP_LABELS)


def _render_sources_health(db: DraftDatabase) -> str:
    return source_handlers.render_sources_health(db)


def _topic_get(topic, key: str, default=None):
    return topic.get(key, default) if isinstance(topic, dict) else getattr(topic, key, default)


def _render_collect_topic_line(topic, *, debug: bool = False) -> list[str]:
    score = int(_topic_get(topic, "score", 0) or 0)
    category = _topic_get(topic, "category")
    title = truncate_telegram_text(re.sub(r"\s+", " ", topic_display_title(topic)).strip(), limit=100)
    summary = truncate_telegram_text(re.sub(r"\s+", " ", topic_summary_ru(topic)).strip(), limit=150)
    angle = truncate_telegram_text(re.sub(r"\s+", " ", topic_angle_ru(topic)).strip(), limit=120)
    marker = f" {_topic_ai_marker(topic)}" if debug else ""
    lines = [
        f"- {score} - {_category_label(category)} - {title}{marker}",
        f"  О чем: {summary}",
    ]
    if angle:
        lines.append(f"  Идея: {angle}")
    content_format = str(_topic_get(topic, "content_format", "") or "").strip()
    if debug and content_format:
        lines.append(f"  Формат: {_format_label_ru(content_format)}")
    return lines


def _render_collect_text(stats: TopicCollectStats, items: list, inserted: list, debug: bool = False) -> str:
    lines = [
        "🧠 Темы собраны",
        "",
        f"Всего найдено: {stats.total}",
        f"Новых: {stats.new}",
        f"Уже были: {stats.existing}",
        f"Дубли по смыслу: {stats.near_duplicate}",
        f"Объединено с похожими: {stats.merged_story}",
        f"Старые: {stats.stale}",
        f"Без даты: {stats.missing_date}",
        f"Низкое качество: {stats.low_quality or stats.low_score}",
        f"Мусор/спам: {stats.spam}",
        f"Некорректные: {stats.invalid}",
        f"Время: {int(round(stats.total_seconds))} сек.",
        f"Обогащено AI: {stats.ai_enriched} тем. Попыток: {stats.ai_enrichment_attempted}, ошибок: {stats.ai_enrichment_failed}.",
    ]
    zero_reason = _topic_ai_zero_reason_ru(stats)
    if zero_reason:
        lines.append(zero_reason)
    lines.append("")
    if debug:
        lines.extend(
            [
                f"Источники: {stats.source_seconds:.1f} сек",
                f"Сохранение/скоринг: {stats.store_seconds:.1f} сек",
                f"AI-обогащение: {stats.ai_seconds:.1f} сек",
                f"AI-обогащено: {stats.ai_enriched} / {stats.ai_enrich_limit}",
                "Диагностика метаданных: "
                f"ai_enrichment_attempted={stats.ai_enrichment_attempted}, "
                f"ai_enriched={stats.ai_enriched}, "
                f"ai_invalid_json={stats.ai_enrichment_invalid_json}, "
                f"ai_invalid_fields={stats.ai_enrichment_invalid_fields}, "
                f"ai_output_truncated={stats.ai_enrichment_output_truncated}, "
                f"ai_provider_errors={stats.ai_enrichment_provider_error}, "
                f"ai_json_mode_unsupported={stats.ai_enrichment_json_mode_unsupported}, "
                f"deterministic_fallback_used={stats.deterministic_fallback_used}, "
                f"ai_enrichment_skipped_no_provider={stats.ai_enrichment_skipped_no_provider}, "
                f"ai_enrichment_skipped_limit={stats.ai_enrichment_skipped_limit}, "
                f"ai_enrichment_skipped_no_candidates={stats.ai_enrichment_skipped_no_candidates}, "
                f"skipped_existing_metadata={stats.ai_enrichment_skipped_existing_metadata}",
                "",
            ]
        )
        if stats.ai_enrichment_selected:
            lines.append("Выбраны для AI-обогащения:")
            lines.extend(stats.ai_enrichment_selected[:8])
            lines.append("")
        if stats.skipped_examples:
            lines.append("Примеры пропусков:")
            labels = {"low_score": "low_score", "stale": "stale", "spam": "spam", "invalid": "invalid"}
            for key in ("low_score", "stale", "spam", "invalid"):
                examples = stats.skipped_examples.get(key) or []
                if not examples:
                    continue
                lines.append(f"{labels[key]}:")
                lines.extend(examples[:2])
            lines.append("")
    displayed_preview_topics = []
    if inserted:
        marked_top = [i for i in inserted if getattr(i, "_collect_preview_top_new", False)]
        top = sorted(marked_top, key=lambda i: getattr(i, "_collect_preview_top_new_order", 999)) if marked_top else sorted(inserted, key=lambda i: i.score, reverse=True)[:5]
        top = top[: 5 if debug else 3]
        displayed_preview_topics.extend(top)
        lines.append("Лучшие новые:")
        for item in top:
            lines.extend(_render_collect_topic_line(item, debug=debug))
    else:
        lines.append("Новых сильных тем нет. Посмотри старые через /topics_all или добавь источники в CUSTOM_TOPIC_FEEDS.")
    preview_source_items = [i for i in items if getattr(i, "_accepted_for_preview", False)] or items
    marked_lively = [i for i in preview_source_items if getattr(i, "_collect_preview_lively", False)]
    lively = (
        sorted(marked_lively, key=lambda i: getattr(i, "_collect_preview_lively_order", 999))
        if marked_lively
        else [i for i in sorted(preview_source_items, key=lambda i: i.score, reverse=True) if i.score >= 50 and (i.source_group in {"community","github","x","tools","custom"} or i.category in {"drama","meme","guide","creator"})][:5]
    )
    lively = lively[: 5 if debug else 3]
    lines.extend(["", "Живые темы:"])
    if lively:
        displayed_preview_topics.extend(lively)
        for item in lively:
            lines.extend(_render_collect_topic_line(item, debug=debug))
    else:
        lines.append("- пока нет")
    if debug:
        unique_displayed = _dedupe_topic_items_by_identity(displayed_preview_topics)
        ai_count = sum(1 for item in unique_displayed if _is_ai_enriched_topic(item))
        fallback_count = max(0, len(unique_displayed) - ai_count)
        lines.extend(["", f"Покрытие preview: [AI] {ai_count}, [fallback] {fallback_count}"])
        if stats.ai_enriched and ai_count == 0:
            lines.append("AI enriched topics are not present in preview list. Check enrichment candidate selection.")
    rendered = "\n".join(lines)
    if not debug and telegram_text_len(rendered) > TELEGRAM_SAFE_TEXT_LIMIT:
        logger.warning("Compact /collect summary exceeded safe limit: length=%s", telegram_text_len(rendered))
        return truncate_telegram_text(rendered, limit=TELEGRAM_SAFE_TEXT_LIMIT)
    return rendered


def _render_sources_inventory(settings, db: DraftDatabase) -> list[str]:
    return source_handlers.render_sources_inventory(settings, db, _detect_railway_with_local_db_path)


sources_status_command = source_handlers.sources_status_command


async def _usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int, period_title: str) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    summary = db.get_ai_usage_summary(days=days)
    costs_enabled = any([
        settings.openrouter_input_cost_per_1m, settings.openrouter_output_cost_per_1m,
        settings.openai_input_cost_per_1m, settings.openai_output_cost_per_1m,
    ])
    if update.message:
        await update.message.reply_text(_render_usage_text(summary, period_title, costs_enabled), reply_markup=_admin_reply_keyboard())


async def usage_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _usage_command(update, context, days=1, period_title="сегодня")


async def usage_7d_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _usage_command(update, context, days=7, period_title="7 дней")


async def usage_month_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _usage_command(update, context, days=30, period_title="30 дней")


async def style_guide_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        return
    summary = (
        "Текущий стиль @simplify_ai:\n"
        "- простой русский, короткие фразы, без AI-клише\n"
        "- формат: emoji-заголовок, короткий ввод, при необходимости список с ➖\n"
        "- обязательно практический смысл и короткий человеческий финал\n"
        "- без 'не про..., а про...', без эм-даша и без выдуманных фактов"
    )
    if update.message:
        await update.message.reply_text(summary, reply_markup=_admin_reply_keyboard())


def _extract_entity_text(message, entity) -> str:
    try:
        if message.text:
            return message.parse_entity(entity)
        if message.caption:
            return message.parse_caption_entity(entity)
    except Exception:
        return ""
    return ""


def _extract_custom_emoji_lines(message) -> list[str]:
    lines: list[str] = []
    if not message:
        return lines
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for entity in entities:
        if getattr(entity, "type", "") != "custom_emoji":
            continue
        emoji_id = getattr(entity, "custom_emoji_id", "") or ""
        if not str(emoji_id).isdigit():
            continue
        fragment = _extract_entity_text(message, entity)
        fallback = fragment or "?"
        lines.append(f"emoji: {fallback}\ncustom_emoji_id: {emoji_id}\nalias template:\nemoji_{len(lines) + 1}|{fallback}|{emoji_id}")
    return lines


async def emoji_ids_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.", reply_markup=_admin_reply_keyboard())
        return
    if not update.message:
        return
    target = update.message.reply_to_message or update.message
    lines = _extract_custom_emoji_lines(target)
    if not lines:
        await update.message.reply_text(
            "Кастомные emoji не найдены. Пришли сообщение с нужным кастомным emoji или ответь на него командой /emoji_ids.",
            reply_markup=_admin_reply_keyboard(),
        )
        return
    await update.message.reply_text("\n\n".join(lines), reply_markup=_admin_reply_keyboard())


def _configured_custom_emoji_ids(settings) -> list[str]:
    ids = list(settings.custom_emoji_map.values())
    ids.extend(emoji_id for _, emoji_id in settings.custom_emoji_aliases.values())
    return list(dict.fromkeys(str(emoji_id) for emoji_id in ids if str(emoji_id).isdigit()))


async def _validate_custom_emoji_ids(bot, emoji_ids: list[str]) -> tuple[set[str] | None, set[str] | None, str | None]:
    if not emoji_ids:
        return set(), set(), None
    method = getattr(bot, "get_custom_emoji_stickers", None)
    if not callable(method):
        return None, None, "method unavailable"

    try:
        valid_ids: set[str] = set()
        for offset in range(0, len(emoji_ids), 200):
            stickers = await method(custom_emoji_ids=emoji_ids[offset:offset + 200])
            valid_ids.update(
                str(sticker.custom_emoji_id)
                for sticker in stickers
                if getattr(sticker, "custom_emoji_id", None)
            )
    except Exception as exc:
        logger.warning("custom emoji API validation failed error=%s", type(exc).__name__)
        return None, None, type(exc).__name__

    return valid_ids, set(emoji_ids) - valid_ids, None


def _custom_emoji_test_sources(
    settings,
    validation_lines: list[str],
    valid_ids: set[str] | None,
) -> list[str]:
    entries: list[str] = []
    for fallback, emoji_id in settings.custom_emoji_map.items():
        if valid_ids is not None and emoji_id not in valid_ids:
            entries.append(f"map id={emoji_id} INVALID")
        else:
            entries.append(f"{fallback} map id={emoji_id}")
    for alias, (_, emoji_id) in settings.custom_emoji_aliases.items():
        if valid_ids is not None and emoji_id not in valid_ids:
            entries.append(f"alias={alias} id={emoji_id} INVALID")
        else:
            entries.append(f"[[EMOJI:{alias}]] alias={alias} id={emoji_id}")

    sources: list[str] = []
    for offset in range(0, len(entries), 25):
        header = [
            "Custom emoji diagnostics",
            f"CUSTOM_EMOJI_MAP: {len(settings.custom_emoji_map)}",
            f"CUSTOM_EMOJI_ALIASES: {len(settings.custom_emoji_aliases)}",
        ]
        if offset == 0:
            header.extend(validation_lines)
        else:
            header.append(f"Preview continued: {offset + 1}-{min(offset + 25, len(entries))}")
        sample = []
        if offset == 0:
            sample = [
                "Post-style raw emoji sample",
                "🔥 Заголовок",
                "",
                "➖ пункт один",
                "➖ пункт два",
                "",
                "💭 финальная мысль",
                "",
            ]
        sources.append("\n".join([*header, "", *sample, *entries[offset:offset + 25]]))
    return sources


def _render_custom_emoji_test_preview(
    sources: list[str],
    custom_emoji_map: dict[str, str],
    custom_emoji_aliases: dict[str, tuple[str, str]],
) -> list[str]:
    return [
        render_post_html(
            source,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
            strict_custom_emoji=True,
        )
        for source in sources
    ]


async def _send_custom_emoji_test_preview(bot, chat_id, rendered_messages: list[str]) -> None:
    for rendered in rendered_messages:
        await bot.send_message(
            chat_id=chat_id,
            text=rendered,
            parse_mode="HTML",
            link_preview_options=_disabled_link_preview_options(),
        )


async def emoji_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    if not update.message:
        return

    mode = context.args[0].strip().lower() if context.args else ""
    if mode not in {"", "channel", "debug"} or len(context.args) > 1:
        await update.message.reply_text("Использование: /emoji_test, /emoji_test channel или /emoji_test debug")
        return
    send_to_channel = mode == "channel"
    debug_mode = mode == "debug"

    if not settings.custom_emoji_map and not settings.custom_emoji_aliases:
        await update.message.reply_text(
            "Custom emoji не настроены.\n"
            "CUSTOM_EMOJI_MAP: 🔥|custom_emoji_id;💭|custom_emoji_id\n"
            "CUSTOM_EMOJI_ALIASES: fire|🔥|custom_emoji_id;thought|💭|custom_emoji_id"
        )
        return

    emoji_ids = _configured_custom_emoji_ids(settings)
    valid_ids, invalid_ids, validation_error = await _validate_custom_emoji_ids(context.bot, emoji_ids)
    if validation_error:
        validation_lines = [f"Bot API validation: unavailable ({validation_error})"]
    else:
        validation_lines = [
            f"Bot API valid ids: {len(valid_ids or set())}",
            f"Bot API invalid ids: {', '.join(sorted(invalid_ids or set())) or 'none'}",
        ]

    render_map = {
        fallback: emoji_id
        for fallback, emoji_id in settings.custom_emoji_map.items()
        if valid_ids is None or emoji_id in valid_ids
    }
    render_aliases = {
        alias: emoji_data
        for alias, emoji_data in settings.custom_emoji_aliases.items()
        if valid_ids is None or emoji_data[1] in valid_ids
    }
    sources = _custom_emoji_test_sources(settings, validation_lines, valid_ids)
    rendered_messages = _render_custom_emoji_test_preview(sources, render_map, render_aliases)
    admin_chat_id = settings.admin_id
    try:
        await _send_custom_emoji_test_preview(context.bot, admin_chat_id, rendered_messages)
    except Exception as exc:
        logger.warning("custom emoji admin preview failed error=%s", type(exc).__name__)
        await update.message.reply_text(
            "Telegram отклонил HTML preview. Форматирование построено, но custom emoji недоступны "
            f"для этого чата или бота ({type(exc).__name__})."
        )
        return

    if debug_mode:
        for index, rendered in enumerate(rendered_messages, start=1):
            await safe_reply_text(
                update.message,
                f"Rendered HTML #{index} (без секретов):\n{rendered}",
                parse_mode=None,
            )

    if send_to_channel:
        try:
            await _send_custom_emoji_test_preview(context.bot, settings.channel_id, rendered_messages)
            await update.message.reply_text("Тест custom emoji отправлен в CHANNEL_ID.")
        except Exception as exc:
            logger.warning("custom emoji channel preview failed error=%s", type(exc).__name__)
            await update.message.reply_text(
                "Telegram отклонил channel preview. Проверь права бота и поддержку custom emoji "
                f"для канала ({type(exc).__name__})."
            )



async def moderation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Publish/Reject/Rewrite button clicks."""

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    query = update.callback_query

    if not query:
        return

    user_id = query.from_user.id if query.from_user else None
    if not _is_admin(user_id, settings.admin_id):
        await _safe_answer_callback(query, "Только администратор может модерировать.", show_alert=True)
        return

    await _safe_answer_callback(query)
    data = query.data or ""
    if data.startswith("menu_"):
        await _handle_menu_callback(update, context, data)
        return
    if data.startswith("topics_"):
        await _handle_topics_callback(update, context, data)
        return
    if data == "sources_health":
        await _handle_menu_callback(update, context, data)
        return
    if data in {"sources_list", "sources_inventory", "source_add_rss", "source_add_telegram", "source_confirm_rss", "source_cancel_add"} or data.startswith("source_toggle:") or data.startswith("source_delete:") or data.startswith("source_test:"):
        await _handle_sources_callback(update, context, data)
        return

    try:
        action, draft_id, slot = _parse_callback_data(data)
    except (AttributeError, ValueError):
        await _edit_callback_message(query, "Некорректное действие.")
        return

    try:
        if await handle_cleanup_callback(
            query, context, db, settings, _edit_callback_message, _back_to_menu_keyboard
        ):
            return

        if await handle_topic_moderation_action(update, context, action, draft_id, query):
            return

        if action == "queue_today":
            await _edit_callback_message(
                query,
                _render_queue_text(db, settings, day_offset=0),
                reply_markup=_queue_keyboard(db, settings, 0),
            )
            return

        if action == "queue_tomorrow":
            await _edit_callback_message(
                query,
                _render_queue_text(db, settings, day_offset=1),
                reply_markup=_queue_keyboard(db, settings, 1),
            )
            return

        if action == "queue_pick_slot":
            day_offset = draft_id
            normalized_slot = _normalize_slot_hhmm(slot or "")
            configured_slots = [_normalize_slot_hhmm(item) for item in settings.daily_post_slots]
            if normalized_slot not in configured_slots:
                await _edit_callback_message(query, "Такого слота нет в настройках расписания.")
                return
            tz = ZoneInfo(settings.schedule_timezone)
            now_local = datetime.now(tz)
            day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
            hour, minute = _parse_slot_hhmm(normalized_slot)
            selected_local = day_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if selected_local <= now_local:
                await _edit_callback_message(query, "Этот слот уже в прошлом. Выбери другой слот.", reply_markup=_queue_keyboard(db, settings, day_offset))
                return
            if not _is_local_slot_free(db, settings, day_offset, normalized_slot):
                await _edit_callback_message(query, "Этот слот уже занят. Обнови очередь и выбери свободный слот.", reply_markup=_queue_keyboard(db, settings, day_offset))
                return
            drafts = _latest_actionable_drafts(db, limit=10)
            if not drafts:
                await _edit_callback_message(query, "Нет черновиков в статусе draft или approved для постановки в очередь.", reply_markup=_queue_keyboard(db, settings, day_offset))
                return
            await _edit_callback_message(
                query,
                f"Выбери черновик для слота {selected_local.strftime('%d.%m %H:%M')}:",
                reply_markup=_queue_draft_pick_keyboard(db, day_offset, normalized_slot),
            )
            return

        if action == "queue_schedule_draft":
            if not slot or ":" not in slot:
                await _edit_callback_message(query, "Некорректный слот времени.")
                return
            day_offset_raw, slot_hhmm = slot.split(":", 1)
            try:
                day_offset = int(day_offset_raw)
            except ValueError:
                await _edit_callback_message(query, "Некорректный день очереди.")
                return
            try:
                scheduled_text = _schedule_draft_to_local_slot(db, settings, draft_id, day_offset, slot_hhmm)
            except ValueError as exc:
                await _edit_callback_message(query, str(exc), reply_markup=_queue_keyboard(db, settings, day_offset))
                return
            await _edit_callback_message(
                query,
                f"Черновик #{draft_id} поставлен на {scheduled_text}",
                reply_markup=_queue_keyboard(db, settings, day_offset),
            )
            return


        handled = await handle_draft_moderation_callback(
            update,
            context,
            action,
            draft_id,
            slot,
            ModerationCallbackDeps(
                edit_callback_message=_edit_callback_message,
                publish_to_channel=publish_to_channel,
                schedule_keyboard=_schedule_keyboard,
                queue_keyboard=_queue_keyboard,
                schedule_draft_to_nearest_slot=_schedule_draft_to_nearest_slot,
                can_publish=_can_publish,
                can_schedule=_can_schedule,
                can_edit=_can_edit,
                status_guard_message=_status_guard_message,
                regenerate_draft_from_source=_regenerate_draft_from_source,
                build_moderation_text=_build_moderation_text,
                moderation_keyboard=_moderation_keyboard,
                moderation_keyboard_for_draft=_moderation_keyboard_for_draft,
                preview_keyboard=_preview_keyboard,
                full_draft_text=_full_draft_text,
                clear_pending_edit=_clear_pending_edit,
                set_pending_edit=_set_pending_edit,
                clear_pending_media=_clear_pending_media,
                get_pending_media=_get_pending_media,
                set_pending_media=_set_pending_media,
                send_moderation_preview=_send_moderation_preview,
                resolve_ai_request=_resolve_ai_request,
                run_rewrite_post_draft=_run_rewrite_post_draft,
                run_polish_post_draft=_run_polish_post_draft,
                rewrite_test_draft=rewrite_test_draft,
                encode_media_group=encode_media_group,
                estimate_ai_cost=estimate_ai_cost,
                empty_ai_reply_text=EMPTY_AI_REPLY_TEXT,
            ),
        )
        if not handled:
            draft = db.get_draft(draft_id)
            if not draft and action not in {"edit_cancel", "attach_media_cancel"}:
                await _edit_callback_message(query, f"Черновик #{draft_id} не найден.")
                return
            await _edit_callback_message(query, "Неизвестное действие.")

    except Exception as exc:  # Keep user-facing flow stable on runtime errors.
        logger.exception("Error while handling moderation callback: %s", exc)
        try:
            if "draft_id" in locals():
                await _edit_callback_message(
                    query,
                    f"Что-то пошло не так. Попробуй ещё раз или открой черновик через /draft_info {draft_id}.",
                )
            else:
                await _edit_callback_message(
                    query,
                    "Что-то пошло не так. Попробуй ещё раз или открой черновик через /draft_info <id>.",
                )
        except Exception as edit_exc:
            logger.exception("Failed to edit callback message after error: %s", edit_exc)
            if query.message:
                await query.message.reply_text("Что-то пошло не так. Посмотри логи.")


async def _run_menu_collect_background(query, context, settings, db: DraftDatabase) -> None:
    try:
        stats, items, inserted = await _collect_topics_with_stats(db, settings=settings)
        await _edit_callback_message(
            query,
            _render_collect_text(stats, items, inserted),
            reply_markup=_collect_result_keyboard(),
        )
    except Exception:
        logger.exception("Background topic collection from menu failed")
        await _edit_callback_message(
            query,
            "Не удалось собрать темы. Проверь источники и попробуй ещё раз.",
            reply_markup=_back_to_menu_keyboard(),
        )
    finally:
        context.application.bot_data["topics_collect_running"] = False


async def _handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    query = update.callback_query
    if not query:
        return

    if data == "menu_back":
        await _edit_callback_message(query, _main_menu_text(), reply_markup=_main_menu_keyboard())
    elif data == "menu_generate":
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔗 Из ссылки", callback_data="menu_url_help")],
                [InlineKeyboardButton("🧪 Тестовый черновик", callback_data="menu_test_draft")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")],
            ]
        )
        await _edit_callback_message(
            query,
            "✍️ Создание черновика\n\nВыбери способ:",
            reply_markup=keyboard,
        )
    elif data == "menu_test_draft":
        content = create_test_draft()
        draft_id = db.create_draft(content)
        await _send_moderation_preview(context, settings.admin_id, draft_id, content)
        await _edit_callback_message(
            query,
            f"Тестовый черновик #{draft_id} создан и отправлен на модерацию.",
            reply_markup=_back_to_menu_keyboard(),
        )
    elif data == "menu_url_help":
        await _edit_callback_message(
            query,
            "Пришли ссылку одним сообщением, и я сделаю черновик поста из страницы.",
            reply_markup=_back_to_menu_keyboard(),
        )
    elif data == "menu_drafts":
        drafts = db.list_drafts(limit=10)
        if not drafts:
            await _edit_callback_message(query, "Черновики не найдены.", reply_markup=_back_to_menu_keyboard())
            return
        await _edit_callback_message(query, "Последние черновики:", reply_markup=_back_to_menu_keyboard())
        for draft in drafts:
            await context.bot.send_message(
                chat_id=settings.admin_id,
                text=_draft_snippet_text(draft),
                reply_markup=_draft_actions_keyboard(int(draft["id"]), str(draft.get("status") or "")),
                link_preview_options=_disabled_link_preview_options(),
            )
    elif data == "menu_topics":
        hot_topics = _topics_for_kind(db, "hot", limit=5)
        new_topics = _topics_for_kind(db, "new", limit=5)
        hub_text = _render_topics_hub_text(db)
        if not hot_topics and not new_topics:
            hub_text += "\n\nТем пока нет. Запусти /collect или /collect_debug."
        elif not hot_topics:
            hub_text += "\n\nГорячих тем пока нет, но есть свежие темы. Показываю лучшие новые.\n\n" + "\n".join(_topic_preview_line(t) for t in new_topics[:5])
        else:
            hub_text += "\n\n" + "\n".join(_topic_preview_line(t) for t in hot_topics[:5])
        await _edit_callback_message(query, hub_text, reply_markup=_topics_hub_keyboard())
    elif data == "menu_collect":
        if context.application.bot_data.get("topics_collect_running"):
            await _edit_callback_message(
                query,
                "Сбор тем уже идёт. Я обновлю это сообщение, когда закончу.",
                reply_markup=_back_to_menu_keyboard(),
            )
            return
        context.application.bot_data["topics_collect_running"] = True
        await _edit_callback_message(query, "🔄 Начал сбор тем. Ботом можно пользоваться дальше — результат появится здесь.")
        context.application.create_task(
            _run_menu_collect_background(query=query, context=context, settings=settings, db=db)
        )
        return
    elif data == "menu_show_topics":
        limit = _parse_topic_limit(context, default=10)
        topics = db.list_topic_candidates(limit=limit, status="new", order_by_score=True)
        if not topics:
            await _edit_callback_message(query, "Пока нет тем. Запусти /collect", reply_markup=_back_to_menu_keyboard())
            return
        await _edit_callback_message(query, "Найденные темы:", reply_markup=_back_to_menu_keyboard())
        for topic in topics:
            text = _topic_card_text(topic)
            keyboard = _topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or ""))
            await safe_send_message(
                context.bot,
                chat_id=settings.admin_id,
                text=truncate_telegram_text(text),
                reply_markup=keyboard,
                link_preview_options=_disabled_link_preview_options(),
            )
    elif data == "menu_sources":
        await _edit_callback_message(query, "📡 Источники\nВыбери действие:", reply_markup=_sources_hub_keyboard())
    elif data == "sources_health":
        await _edit_callback_message(query, _render_sources_health(db), reply_markup=_back_to_menu_keyboard())
    elif data == "menu_sources_status":
        if context.application.bot_data.get("sources_check_running"):
            await _edit_callback_message(query, "Проверка источников уже идёт. Дождись результата.", reply_markup=_back_to_menu_keyboard())
            return
        context.application.bot_data["sources_check_running"] = True
        await _edit_callback_message(query, "Проверяю источники... Это может занять до минуты.", reply_markup=_back_to_menu_keyboard())
        context.application.create_task(_run_sources_status_background(context=context, settings=settings, db=db))
        return
    elif data == "menu_queue":
        await _edit_callback_message(
            query,
            _render_queue_text(db, settings, day_offset=0),
            reply_markup=_queue_keyboard(db, settings, 0),
        )
    elif data == "menu_plan_day":
        await _send_daily_plan(context=context, settings=settings, db=db, day_offset=0, summary_query=query)
    elif data == "menu_plan_tomorrow":
        await _send_daily_plan(context=context, settings=settings, db=db, day_offset=1, summary_query=query)
    elif data == "menu_generate_plan_day":
        await _edit_callback_message(query, "Создаю черновики...")
        summary = await _generate_drafts_from_plan(context=context, settings=settings, db=db, day_offset=0)
        has_created = bool(context.user_data.get("pending_plan_schedule_items")) and context.user_data.get("pending_plan_schedule_day") == 0
        await context.bot.send_message(chat_id=settings.admin_id, text=summary, reply_markup=_generated_plan_keyboard(0, has_created))
    elif data == "menu_generate_plan_tomorrow":
        await _edit_callback_message(query, "Создаю черновики...")
        summary = await _generate_drafts_from_plan(context=context, settings=settings, db=db, day_offset=1)
        has_created = bool(context.user_data.get("pending_plan_schedule_items")) and context.user_data.get("pending_plan_schedule_day") == 1
        await context.bot.send_message(chat_id=settings.admin_id, text=summary, reply_markup=_generated_plan_keyboard(1, has_created))
    elif data == "menu_schedule_generated_plan_day":
        await _edit_callback_message(query, "Ставлю черновики в очередь...")
        summary = await _schedule_generated_plan(context=context, settings=settings, db=db, day_offset=0)
        await context.bot.send_message(chat_id=settings.admin_id, text=summary)
    elif data == "menu_schedule_generated_plan_tomorrow":
        await _edit_callback_message(query, "Ставлю черновики в очередь...")
        summary = await _schedule_generated_plan(context=context, settings=settings, db=db, day_offset=1)
        await context.bot.send_message(chat_id=settings.admin_id, text=summary)
    elif data == "menu_settings":
        await _edit_callback_message(query, _settings_text(settings), reply_markup=_settings_keyboard())
    elif data == "menu_cleanup_preview":
        counts = db.cleanup_preview()
        _store_cleanup_preview(context, counts)
        await _edit_callback_message(query, _render_cleanup_preview_text(counts), reply_markup=_cleanup_keyboard())
    elif data == "menu_usage":
        summary = db.get_ai_usage_summary(days=1)
        costs_enabled = any([
            settings.openrouter_input_cost_per_1m, settings.openrouter_output_cost_per_1m,
            settings.openai_input_cost_per_1m, settings.openai_output_cost_per_1m,
        ])
        await _edit_callback_message(
            query,
            _render_usage_text(summary, "сегодня", costs_enabled) + "\n\nДля других периодов: /usage_7d и /usage_month",
            reply_markup=_back_to_menu_keyboard(),
        )
    elif data == "menu_help":
        await _edit_callback_message(
            query,
            "Команды:\n"
            "/menu - открыть меню\n"
            "/generate - черновик через draft-модель\n"
            "/generate <ссылка> - пост из ссылки\n"
            "/drafts - последние черновики\n"
            "/topics - найденные темы\n"
            "/topics_tools - инструменты и гайды\n"
            "/topics_news - новости и модели\n"
            "/topics_fun - живые/мемные темы\n"
            "/topics_hot - самые сильные темы\n"
            "/topics_all - последние темы (все статусы)\n"
            "/plan_day - подобрать темы под пустые слоты сегодня\n"
            "/plan_tomorrow - подобрать темы на завтра\n"
            "/generate_plan_day - создать черновики из плана на сегодня\n"
            "/generate_plan_tomorrow - создать черновики из плана на завтра\n"
            "/schedule_generated_plan_day - поставить созданные черновики в очередь на сегодня\n"
            "/schedule_generated_plan_tomorrow - поставить созданные черновики в очередь на завтра\n"
            "/queue_today - план публикаций на сегодня\n"
            "/queue_tomorrow - план публикаций на завтра\n"
            "/failed_drafts - последние неудачные публикации\n"
            "/unschedule <id> - снять черновик с очереди\n"
            "/restore_draft <id> - вернуть failed в черновики\n"
            "/usage_today - расходы ИИ за сегодня\n"
            "/usage_7d - расходы ИИ за 7 дней\n"
            "/usage_month - расходы ИИ за 30 дней\n"
            "/cleanup_preview - показать безопасную очистку базы\n"
            "/cleanup_confirm - применить свежий предпросмотр очистки\n"
            "/style_guide - краткая сводка текущего стиля генерации\n"
            "/emoji_ids - показать custom_emoji_id из сообщения/реплая\n"
            "/collect - собрать темы\n"
            "/sources_status - проверить источники тем\n"
            "/collect_debug - собрать темы с диагностикой\n"
            "/draft_info <id> - открыть черновик\n"
            "/delete_draft <id> - удалить черновик\n"
            "/attach_media <id> <photo|video|animation> <url> - прикрепить медиа\n\n"
            "Кнопка «📎 Прикрепить медиа» поддерживает до 10 фото/видео/GIF.\n"
            "Кнопка «🗑 Убрать медиа» появляется, когда у черновика есть медиа.\n"
            "Можно отправить фото, видео или GIF/анимацию прямо боту.\n"
            "Команда /attach_media остаётся для URL/ручного режима.\n\n"
            "В меню «✍️ Создать черновик» сначала выбери способ создания.\n"
            "Для реального поста самый быстрый путь — прислать ссылку одним сообщением.",
            reply_markup=_back_to_menu_keyboard(),
        )


async def _handle_sources_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    await source_handlers.handle_sources_callback(
        update=update,
        context=context,
        data=data,
        edit_callback_message=_edit_callback_message,
        sources_hub_keyboard=_sources_hub_keyboard,
        source_card_keyboard=_source_card_keyboard,
        run_source_test_background=_run_source_test_background,
        render_sources_inventory=_render_sources_inventory,
    )


async def admin_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a draft from any URL sent by admin in a regular message."""

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    message_text = (update.message.text or "").strip() if update.message else ""

    if not _is_admin(user_id, settings.admin_id):
        return

    handled_pending_edit = await _handle_pending_text_edit(update, context)
    if handled_pending_edit:
        return
    handled_pending_media = await _handle_pending_media_attach(update, context)
    if handled_pending_media:
        return
    flow = context.user_data.get("source_add_flow") or {}
    if update.message and message_text and flow:
        db: DraftDatabase = context.bot_data["db"]
        flow_type = flow.get("type")
        step = flow.get("step")
        if flow_type == "rss" and step == "name":
            context.user_data["source_add_flow"] = {"type": "rss", "step": "url", "name": message_text[:80]}
            await update.message.reply_text("Пришлите RSS-ссылку или URL страницы. Проверяю автоматически.")
            return
        if flow_type == "rss" and step == "url":
            if not is_valid_rss_input_url(message_text):
                await update.message.reply_text("Нужен URL, который начинается с http/https.")
                return
            await update.message.reply_text("Проверяю источник...")
            feed_url, error = await asyncio.to_thread(discover_rss_feed_url, message_text.strip())
            if not feed_url:
                await update.message.reply_text("Не нашёл RSS/Atom-ленту. Пришли прямую RSS-ссылку или другой источник.")
                return
            duplicate = find_duplicate_source("rss", feed_url, settings, db)
            if duplicate:
                await update.message.reply_text(
                    f"Такой источник уже есть: {duplicate.get('name')} ({duplicate.get('location')})."
                )
                return
            context.user_data["source_add_flow"] = {
                "type": "rss",
                "step": "confirm",
                "name": flow.get("name", "RSS"),
                "feed_url": feed_url,
            }
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Добавить", callback_data="source_confirm_rss"), InlineKeyboardButton("❌ Отмена", callback_data="source_cancel_add")]])
            await update.message.reply_text(f"Нашёл RSS: {feed_url}. Добавить источник?", reply_markup=kb)
            return
        if flow_type == "telegram" and step == "value":
            username = normalize_telegram_channel_input(message_text)
            if not username:
                await update.message.reply_text("Не удалось распознать канал. Пришли @username или t.me/username.")
                return
            duplicate = find_duplicate_source("telegram", username, settings, db)
            if duplicate:
                await update.message.reply_text(f"Такой Telegram-источник уже есть: {duplicate.get('name')} ({duplicate.get('location')}).")
                return
            try:
                db.create_managed_source("telegram", f"Telegram @{username}", username, "telegram")
            except ValueError as exc:
                await update.message.reply_text(f"Не удалось добавить источник: {exc}")
                return
            context.user_data.pop("source_add_flow", None)
            await update.message.reply_text("Telegram-источник добавлен.", reply_markup=_sources_hub_keyboard())
            return

    if update.message and update.message.reply_to_message:
        reply_text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        draft_id = _extract_draft_id_from_text(reply_text)
        if draft_id is not None:
            media_url = None
            media_type = None
            if update.message.photo:
                media_type = "photo"
                media_url = update.message.photo[-1].file_id
            elif update.message.animation:
                media_type = "animation"
                media_url = update.message.animation.file_id
            elif update.message.video:
                media_type = "video"
                media_url = update.message.video.file_id

            if media_type and media_url:
                draft = db.get_draft(draft_id)
                if not draft:
                    await update.message.reply_text(f"Черновик #{draft_id} не найден.")
                    return
                db.attach_media(draft_id, media_url, media_type)
                await update.message.reply_text(f"Привязал {media_type} к черновику #{draft_id}.")
                return

    if not message_text:
        return

    if await _handle_navigation_text(update, context, message_text):
        return

    source_url_raw = find_first_url(message_text)
    if not source_url_raw:
        return

    source_url = normalize_url(source_url_raw)
    duplicate = db.find_by_source_url(source_url)
    if duplicate:
        await update.message.reply_text(
            f"Похоже, эта ссылка уже обрабатывалась: черновик #{duplicate['id']} (статус: {duplicate['status']}).",
            link_preview_options=_disabled_link_preview_options(),
        )
        return

    if not settings.has_ai_provider:
        await update.message.reply_text("AI-провайдер не настроен. Добавь OPENROUTER_API_KEY или OPENAI_API_KEY и перезапусти бота.")
        return

    await update.message.reply_text("Нашёл ссылку. Читаю страницу и готовлю черновик...")

    try:
        details = await _run_fetch_page_content_details(source_url)
        route = _resolve_ai_request(settings, "draft")
        logger.info("url_generate provider=%s model=%s", route.provider, route.model)
        generation_result, used_fallback, operation = await _generate_url_draft_with_fallback(
            route=route,
            settings=settings,
            source_url=source_url,
            title=details.title,
            page_text=details.text,
        )
    except EmptyAIResponseError:
        await update.message.reply_text(EMPTY_AI_REPLY_TEXT)
        return
    except Exception as exc:
        logger.exception("Failed to process URL %s: %s", source_url, exc)
        await update.message.reply_text(
            "Не удалось нормально прочитать страницу. Возможно, там мало текста, сайт закрыл доступ или страница требует JavaScript. Попробуй другую ссылку или пришли текст новости вручную."
        )
        return

    try:
        content = generation_result.content
        if not content.strip():
            await update.message.reply_text(EMPTY_AI_REPLY_TEXT)
            return
        draft_id = db.create_draft(content, source_url=source_url, source_image_url=details.preview_image_url)
        used_provider = generation_result.provider or route.provider
        estimated_cost = estimate_ai_cost(used_provider, generation_result.prompt_tokens, generation_result.completion_tokens, settings)
        db.record_ai_usage(
            provider=used_provider, model=generation_result.model or route.model, operation=operation,
            prompt_tokens=generation_result.prompt_tokens, completion_tokens=generation_result.completion_tokens,
            total_tokens=generation_result.total_tokens, estimated_cost_usd=estimated_cost, source_url=source_url, draft_id=draft_id
        )
        logger.info("AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s", used_provider, generation_result.model or route.model, operation, generation_result.prompt_tokens, generation_result.completion_tokens, generation_result.total_tokens, estimated_cost)
        await _send_moderation_preview(
            context,
            settings.admin_id,
            draft_id,
            content,
            source_url,
            source_image_url=details.preview_image_url,
        )
        await update.message.reply_text(f"Черновик #{draft_id} создан и отправлен на модерацию.")
        if used_fallback:
            logger.info(
                "Draft created with fallback model: draft_id=%s source_url=%s",
                draft_id,
                source_url,
            )
    except EmptyAIResponseError:
        await update.message.reply_text(EMPTY_AI_REPLY_TEXT)
    except Exception as exc:
        logger.exception("Failed to finalize URL draft creation for %s", source_url)
        await update.message.reply_text(
            f"Не удалось создать черновик: {type(exc).__name__}. Ошибка записана в логи.",
            reply_markup=_admin_reply_keyboard(),
        )
