"""Telegram handlers for admin commands and moderation callbacks."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from telegram import KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot.config import _detect_railway_with_local_db_path
from bot.database import DraftDatabase
from bot.cleanup_handlers import (
    CLEANUP_PREVIEW_COUNTS_KEY,
    CLEANUP_PREVIEW_GENERATED_AT_KEY,
    _cleanup_keyboard,
    _render_cleanup_preview_text,
    _store_cleanup_preview,
    handle_cleanup_callback,
)
from bot.drafts import create_test_draft, rewrite_test_draft
from bot.media_utils import decode_media_items, encode_media_group, media_count
from bot.publisher import publish_to_channel
from bot.queue_helpers import (
    ACTIONABLE_DRAFT_STATUSES,
    _empty_slots_for_day,
    _find_nearest_available_slot,
    _get_day_range,
    _is_local_slot_free,
    _latest_actionable_drafts,
    _normalize_slot_hhmm,
    _parse_slot_hhmm,
    _queue_day_slots,
    _queue_draft_ids_for_day,
    _queue_draft_pick_keyboard,
    _queue_keyboard,
    _render_queue_day_text,
    _render_queue_text,
    _schedule_draft_to_local_slot,
    _schedule_draft_to_nearest_slot,
    _short_post_preview,
    _slot_callback_hhmm,
    _busy_slots_for_local_day,
)
from bot.telegram_formatting import strip_quote_markers
from bot.sources import SourceReport, collect_topics, collect_topics_with_diagnostics
from bot.topic_display import related_sources_summary, topic_angle_ru, topic_compact_preview_ru, topic_display_reason, topic_display_title, topic_original_title_line, topic_summary_ru
from bot.writer import (
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
    polish_post_draft,
    rewrite_post_draft,
)

logger = logging.getLogger(__name__)
ALLOWED_MEDIA_TYPES = {"photo", "video", "animation"}
ALLOWED_DRAFT_STATUSES = {"draft", "approved", "scheduled", "publishing", "published", "rejected", "failed"}
TELEGRAM_CAPTION_LIMIT = 1024
SHORT_MEDIA_PREVIEW_LIMIT = 850
EMPTY_AI_REPLY_TEXT = "Модель вернула пустой ответ. Черновик не создан. Попробуй ещё раз или смени MODEL_DRAFT."
REDDIT_METADATA_EMPTY_REPLY_TEXT = "Reddit-источник заблокирован, а по сохранённому описанию не удалось собрать нормальный черновик. Лучше отклонить тему или открыть источник вручную."

TOPIC_ENRICH_FALLBACK_SUMMARY_RU = "Нужен ручной просмотр: не удалось нормально обработать тему."
TOPIC_ENRICH_FALLBACK_ANGLE_RU = "Открой источник и проверь тему вручную перед генерацией поста."


def _topic_enrich_model(settings) -> str:
    return (getattr(settings, "model_topic_enrich", "") or getattr(settings, "model_draft", "")).strip() or getattr(settings, "model_draft", "")


def _apply_topic_enrichment_fallback(item, db: DraftDatabase) -> None:
    item.title_ru = item.title_ru or item.title
    item.summary_ru = item.summary_ru or TOPIC_ENRICH_FALLBACK_SUMMARY_RU
    item.angle_ru = item.angle_ru or TOPIC_ENRICH_FALLBACK_ANGLE_RU
    topic = db.find_topic_candidate_by_url(item.url)
    if topic:
        db.update_topic_candidate_display_fields(
            int(topic["id"]),
            title_ru=item.title_ru,
            summary_ru=item.summary_ru,
            angle_ru=item.angle_ru,
            reason_ru=item.reason_ru,
        )

REWRITE_DRAFT_ACTIONS = {
    "rewrite_remove_fluff": {
        "mode": "remove_fluff",
        "operation": "rewrite_remove_fluff",
        "progress": "🧹 Убираю воду из черновика #{draft_id}...",
    },
    "rewrite_shorten": {
        "mode": "shorten",
        "operation": "rewrite_shorten",
        "progress": "📉 Сокращаю черновик #{draft_id}...",
    },
    "rewrite_neutralize_ads": {
        "mode": "neutralize_ads",
        "operation": "rewrite_neutralize_ads",
        "progress": "😐 Убираю рекламный тон из черновика #{draft_id}...",
    },
}


def _rewrite_action_config(action: str) -> dict[str, str]:
    try:
        return REWRITE_DRAFT_ACTIONS[action]
    except KeyError as exc:
        raise ValueError(f"Unsupported rewrite action: {action}") from exc



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
    "other": "Другое",
}


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


def _topic_card_text(topic: dict) -> str:
    score = int(topic.get("score") or 0)
    lines = [
        f"🧠 Тема #{topic['id']} - {score} - {_category_label(topic.get('category'))}",
        f"Вес: {_score_label(score)}",
        "",
        topic_display_title(topic),
        "",
        "О чем:",
        topic_summary_ru(topic),
        "",
        "Идея поста:",
        topic_angle_ru(topic),
    ]
    original_line = topic_original_title_line(topic)
    if original_line:
        lines.extend(["", original_line])
    related_line = related_sources_summary(topic)
    if related_line:
        lines.extend(["", related_line])
    lines.extend(
        [
            f"Источник: {topic['source']} / {_source_group_label(topic.get('source_group'))}",
            f"Почему: {topic_display_reason(topic)}",
            f"URL: {topic['url']}",
        ]
    )
    return "\n".join(lines)


def _topic_actions_keyboard(topic_id: int, source_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("✍️ Создать черновик", callback_data=f"topic_generate:{topic_id}")]]
    rows.append([InlineKeyboardButton("🔁 Перевести заново", callback_data=f"topic_reenrich:{topic_id}")])
    if source_url:
        rows.append([InlineKeyboardButton("🔗 Открыть источник", url=source_url)])
    rows.append([InlineKeyboardButton("❌ Отклонить тему", callback_data=f"reject_topic:{topic_id}")])
    return InlineKeyboardMarkup(rows)


def _topics_hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔥 Горячие", callback_data="topics_hot:0"), InlineKeyboardButton("🆕 Новые", callback_data="topics_new:0")],
            [InlineKeyboardButton("🛠 Инструменты", callback_data="topics_tools:0"), InlineKeyboardButton("📰 Новости", callback_data="topics_news:0")],
            [InlineKeyboardButton("😄 Живые", callback_data="topics_fun:0")],
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
    lines = [
        "Бот запущен",
        f"Провайдер AI: {provider}",
        f"Draft model: {settings.model_draft}",
        f"Topic enrich model: {settings.model_topic_enrich}",
        f"Polish model: {settings.model_polish}",
        f"Таймзона: {settings.schedule_timezone}",
        f"Слоты: {', '.join(settings.daily_post_slots)}",
        f"DB path: {settings.db_path}",
        f"AI настроен: {'да' if settings.has_ai_provider else 'нет'}",
        f"Emoji aliases: {len(settings.custom_emoji_aliases)}",
        f"Emoji map: {len(settings.custom_emoji_map)}",
    ]
    if _detect_railway_with_local_db_path(settings.db_path):
        lines.append("⚠️ Railway: локальный DB_PATH может потеряться без persistent volume.")
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
    candidates = db.list_topic_candidates(limit=max(50, limit * 5), status="new", order_by_score=True)
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
            [InlineKeyboardButton("🗓️ План на день", callback_data="menu_plan_day")],
            [InlineKeyboardButton("📅 Очередь", callback_data="menu_queue")],
            [InlineKeyboardButton("📊 Расходы ИИ", callback_data="menu_usage")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")],
            [InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
        ]
    )


def _back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")]])


def _settings_text(settings) -> str:
    ai_provider = _ai_provider_for_status(settings)
    return (
        "⚙️ Настройки\n\n"
        f"Провайдер ИИ: {ai_provider}\n"
        f"Модель черновика: {settings.model_draft}\n"
        f"Модель тем: {settings.model_topic_enrich}\n"
        f"Модель улучшения: {settings.model_polish}\n"
        f"Часовой пояс: {settings.schedule_timezone}\n"
        f"Длина поста: до {settings.post_soft_chars} / максимум {settings.post_max_chars} символов\n"
        f"База данных: {settings.db_path}"
    )


def _resolve_ai_provider(settings) -> tuple[str, str, str | None, dict[str, str] | None]:
    if settings.openrouter_api_key:
        headers = {"X-Title": settings.openrouter_app_name}
        if settings.openrouter_site_url:
            headers["HTTP-Referer"] = settings.openrouter_site_url
        return settings.openrouter_api_key, "openrouter", "https://openrouter.ai/api/v1", headers
    return settings.openai_api_key, "openai", None, None


async def _run_collect_topics():
    return await asyncio.to_thread(collect_topics)


async def _run_collect_topics_with_diagnostics():
    return await asyncio.to_thread(collect_topics_with_diagnostics)


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


async def _translate_topic_title_if_available(item, settings, db: DraftDatabase) -> None:
    if item.title_ru or not settings or not getattr(settings, "has_ai_provider", False):
        return
    api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
    if not api_key:
        return
    result = await _run_translate_topic_title_to_ru(
        api_key=api_key,
        model=_topic_enrich_model(settings),
        title=item.title,
        base_url=base_url,
        extra_headers=extra_headers,
    )
    if not result or not result.content.strip() or result.content.strip() == item.title.strip():
        return
    topic = db.find_topic_candidate_by_url(item.url)
    if not topic:
        return
    item.title_ru = result.content.strip()
    db.update_topic_candidate_display_fields(int(topic["id"]), title_ru=item.title_ru, reason_ru=item.reason_ru)
    estimated_cost = estimate_ai_cost(provider, result.prompt_tokens, result.completion_tokens, settings)
    db.record_ai_usage(
        provider=provider,
        model=result.model or _topic_enrich_model(settings),
        operation="topic_translate_title",
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        estimated_cost_usd=estimated_cost,
        source_url=item.url,
        draft_id=None,
    )


def _parse_topic_metadata_result_content(content: str) -> tuple[str, str, str, str] | None:
    parts = [part.strip() for part in content.splitlines() if part.strip()]
    if len(parts) < 3:
        return None
    return parts[0], parts[1], parts[2], (parts[3] if len(parts) >= 4 else "")


async def _reenrich_topic_candidate_display_metadata(
    topic_id: int,
    settings,
    db: DraftDatabase,
) -> tuple[dict | None, str | None]:
    topic = db.get_topic_candidate(topic_id)
    if not topic:
        return None, f"Тема #{topic_id} не найдена."
    if not settings or not getattr(settings, "has_ai_provider", False):
        return None, "AI-провайдер не настроен."
    api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
    if not api_key:
        return None, "AI-ключ не настроен."
    model = _topic_enrich_model(settings)
    try:
        result = await _run_enrich_topic_metadata_ru(
            api_key=api_key,
            model=model,
            title=str(topic.get("title") or ""),
            source=str(topic.get("source") or ""),
            description=topic.get("original_description"),
            base_url=base_url,
            extra_headers=extra_headers,
        )
    except Exception as exc:
        logger.warning("Manual topic re-enrichment failed: topic_id=%s error=%s", topic_id, exc)
        return None, "Не удалось заново перевести тему."
    if not result or not result.content.strip():
        return None, "Модель не вернула перевод темы."
    parsed = _parse_topic_metadata_result_content(result.content)
    if parsed is None:
        return None, "Модель вернула неполные данные темы."
    title_ru, summary_ru, angle_ru, reason_ru = parsed
    if not all([title_ru, summary_ru, angle_ru, reason_ru]):
        return None, "Модель вернула неполные данные темы."
    db.force_update_topic_candidate_display_fields(
        topic_id,
        title_ru=title_ru,
        summary_ru=summary_ru,
        angle_ru=angle_ru,
        reason_ru=reason_ru,
    )
    estimated_cost = estimate_ai_cost(provider, result.prompt_tokens, result.completion_tokens, settings)
    db.record_ai_usage(
        provider=provider,
        model=result.model or model,
        operation="topic_reenrich_metadata",
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        estimated_cost_usd=estimated_cost,
        source_url=str(topic.get("url") or ""),
        draft_id=None,
    )
    return db.get_topic_candidate(topic_id), None


async def _enrich_topic_metadata_if_available(item, settings, db: DraftDatabase) -> None:
    if not settings or not getattr(settings, "has_ai_provider", False):
        return
    if item.title_ru and item.summary_ru and item.angle_ru:
        return
    api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
    if not api_key:
        return
    try:
        result = await _run_enrich_topic_metadata_ru(
            api_key=api_key,
            model=_topic_enrich_model(settings),
            title=item.title,
            source=item.source,
            description=getattr(item, "original_description", None),
            base_url=base_url,
            extra_headers=extra_headers,
        )
    except Exception as exc:
        logger.warning("Topic metadata enrichment failed: %s", exc)
        _apply_topic_enrichment_fallback(item, db)
        return
    if not result or not result.content.strip():
        _apply_topic_enrichment_fallback(item, db)
        return
    parsed = _parse_topic_metadata_result_content(result.content)
    if parsed is None:
        _apply_topic_enrichment_fallback(item, db)
        return
    title_ru, summary_ru, angle_ru, reason_ru = parsed
    topic = db.find_topic_candidate_by_url(item.url)
    if not topic:
        _apply_topic_enrichment_fallback(item, db)
        return
    item.title_ru = item.title_ru or title_ru
    item.summary_ru = item.summary_ru or summary_ru
    item.angle_ru = item.angle_ru or angle_ru
    item.reason_ru = item.reason_ru or reason_ru
    db.update_topic_candidate_display_fields(
        int(topic["id"]),
        title_ru=item.title_ru,
        summary_ru=item.summary_ru,
        angle_ru=item.angle_ru,
        reason_ru=item.reason_ru,
    )
    estimated_cost = estimate_ai_cost(provider, result.prompt_tokens, result.completion_tokens, settings)
    db.record_ai_usage(
        provider=provider,
        model=result.model or _topic_enrich_model(settings),
        operation="topic_enrich_metadata",
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        estimated_cost_usd=estimated_cost,
        source_url=item.url,
        draft_id=None,
    )


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
                await query.message.reply_text(text, link_preview_options=_disabled_link_preview_options())
            return
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            link_preview_options=_disabled_link_preview_options(),
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            try:
                await query.answer("Уже показано.")
            except Exception as answer_exc:
                logger.warning("Failed to answer not-modified callback: %s", answer_exc)
            return
        logger.warning("Failed to edit callback message: %s", exc)
        raise


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
    api_key: str,
    settings,
    topic: dict[str, object],
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> GenerationResult:
    return await _run_generate_post_draft_from_topic_metadata(
        api_key=api_key,
        model=settings.model_draft,
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
        base_url=base_url,
        extra_headers=extra_headers,
    )


async def _generate_url_draft_with_fallback(
    *,
    api_key: str,
    settings,
    source_url: str,
    title: str,
    page_text: str,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[GenerationResult, bool, str]:
    try:
        result = await _run_generate_post_draft_from_page(
            api_key,
            model=settings.model_draft,
            source_url=source_url,
            title=title,
            page_text=page_text,
            base_url=base_url,
            extra_headers=extra_headers,
        )
        return result, False, "draft_from_url"
    except EmptyAIResponseError as exc:
        logger.warning("Draft model returned empty content for URL %s: %s", source_url, exc)
        fallback_model = (settings.model_polish or "").strip()
        if fallback_model and fallback_model != settings.model_draft:
            logger.warning("Trying fallback generation with MODEL_POLISH=%s", fallback_model)
            result = await _run_generate_post_draft_from_page(
                api_key,
                model=fallback_model,
                source_url=source_url,
                title=title,
                page_text=page_text,
                max_chars=settings.post_max_chars,
                soft_chars=settings.post_soft_chars,
                base_url=base_url,
                extra_headers=extra_headers,
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
        api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
        generation_result, _used_fallback, operation = await _generate_url_draft_with_fallback(
            api_key=api_key,
            settings=settings,
            source_url=source_url,
            title=details.title,
            page_text=details.text,
            base_url=base_url,
            extra_headers=extra_headers,
        )
    except EmptyAIResponseError:
        return None, EMPTY_AI_REPLY_TEXT.replace("Черновик не создан", "Черновик не обновлён")
    except Exception as exc:
        logger.exception("Failed to regenerate draft #%s from %s: %s", draft_id, source_url, exc)
        return None, "Не удалось перегенерировать черновик из источника. Ошибка записана в логи."

    content = generation_result.content.strip()
    if not content:
        return None, "Модель вернула пустой ответ. Черновик не обновлён. Попробуй ещё раз или смени MODEL_DRAFT."

    estimated_cost = estimate_ai_cost(provider, generation_result.prompt_tokens, generation_result.completion_tokens, settings)
    db.record_ai_usage(
        provider=provider,
        model=generation_result.model or settings.model_draft,
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
    settings = context.bot_data["settings"]
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


def _admin_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(NAV_PLAN_DAY), KeyboardButton(NAV_GENERATE_PLAN)],
            [KeyboardButton(NAV_QUEUE), KeyboardButton(NAV_DRAFTS)],
            [KeyboardButton(NAV_TOPICS), KeyboardButton(NAV_SOURCES)],
            [KeyboardButton(NAV_USAGE), KeyboardButton(NAV_STYLE)],
            [KeyboardButton(NAV_SETTINGS), KeyboardButton(NAV_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие или пришли ссылку",
    )


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
        await sources_status_command(update, context)
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
        await _reply_admin_text(
            update,
            "Привет 👋\nКлавиатура навигации включена.\n\nМожешь нажать кнопку ниже или просто прислать ссылку.",
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
            "Меню навигации открыто.\nВыбери действие кнопкой ниже.",
            reply_markup=_admin_reply_keyboard(),
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
        await context.bot.send_message(
            chat_id=settings.admin_id, text=summary_text, reply_markup=keyboard
        )
    for slot, topic in zip(slots, topics):
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=f"🕒 Слот: {slot}\n\n{_topic_card_text(topic)}",
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
        api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
        logger.info("topic_generate provider=%s model=%s", provider, settings.model_draft)
        source_url = str(topic.get("url") or "")
        details = None
        used_metadata_fallback = False
        operation = "topic_generate"
        try:
            if _is_blocked_source_url(source_url):
                raise RuntimeError("Blocked source URL: using saved topic metadata fallback")
            details = await _run_fetch_page_content_details(source_url)
            generation_result, used_fallback, _operation = await _generate_url_draft_with_fallback(
                api_key=api_key,
                settings=settings,
                source_url=source_url,
                title=details.title,
                page_text=details.text,
                base_url=base_url,
                extra_headers=extra_headers,
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
                api_key=api_key,
                settings=settings,
                topic=topic,
                base_url=base_url,
                extra_headers=extra_headers,
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
        estimated_cost = estimate_ai_cost(provider, generation_result.prompt_tokens, generation_result.completion_tokens, settings)
        db.record_ai_usage(
            provider=provider,
            model=generation_result.model or settings.model_draft,
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
            provider,
            generation_result.model or settings.model_draft,
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
        db.schedule_draft(draft_id, scheduled_at_utc)
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
        api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
        logger.info("/generate provider=%s model=%s", provider, settings.model_draft)
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
                api_key=api_key,
                settings=settings,
                source_url=source_url,
                title=details.title,
                page_text=details.text,
                base_url=base_url,
                extra_headers=extra_headers,
            )
        else:
            if message:
                await message.reply_text("Генерирую черновик...")
            generation_result = await _run_generate_post_draft(
                api_key,
                model=settings.model_draft,
                source_url=None,
                max_chars=settings.post_max_chars,
                soft_chars=settings.post_soft_chars,
                base_url=base_url,
                extra_headers=extra_headers,
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
    estimated_cost = estimate_ai_cost(
        provider,
        generation_result.prompt_tokens,
        generation_result.completion_tokens,
        settings,
    )
    db.record_ai_usage(
        provider=provider,
        model=generation_result.model or settings.model_draft,
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
        provider,
        generation_result.model or settings.model_draft,
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




async def _collect_topics_with_stats(db: DraftDatabase, items: list | None = None, settings=None) -> tuple[TopicCollectStats, list, list]:
    total_started = time.monotonic()
    source_started = time.monotonic()
    if items is None:
        items = await _run_collect_topics()
    source_seconds = time.monotonic() - source_started if items is not None else 0.0

    stats = TopicCollectStats(total=len(items), source_seconds=source_seconds)
    inserted = []
    enrichment_candidates = []
    spam_words = ["casino", "porn", "xxx", "bet", "viagra", "airdrop", "token presale"]
    max_topic_age_days = int(getattr(settings, "max_topic_age_days", 14) or 14)
    store_started = time.monotonic()
    for item in items:
        if len(item.title.strip()) < 8 or not item.url.strip() or not item.normalized_title.strip():
            stats.invalid += 1
            continue
        if _is_stale_topic(item, max_topic_age_days):
            stats.stale += 1
            continue
        if not getattr(item, "published_at", None):
            stats.missing_date += 1
        if any(w in item.title.lower() for w in spam_words):
            stats.spam += 1
            continue
        if item.score < 50 and item.source_group != "custom":
            stats.low_score += 1
            stats.low_quality += 1
            continue
        result = db.upsert_topic_candidate_with_reason(
            item.title, item.url, item.source, item.published_at, item.category, item.score, item.reason, item.normalized_title, item.source_group, item.title_ru, item.summary_ru, item.angle_ru, item.reason_ru, item.original_description
        )
        if result == "inserted":
            stats.new += 1
            inserted.append(item)
        elif result == "existing_url":
            stats.existing += 1
        elif result == "merged_story":
            stats.merged_story += 1
        else:
            stats.near_duplicate += 1
        if result in {"inserted", "existing_url", "merged_story"} and item.score >= 50 and not (item.title_ru and item.summary_ru and item.angle_ru):
            stored_topic = db.find_topic_candidate_by_url(item.url)
            if not stored_topic or str(stored_topic.get("status") or "new") == "new":
                enrichment_candidates.append(item)
    stats.store_seconds = time.monotonic() - store_started

    enrich_limit = int(getattr(settings, "topic_ai_enrich_limit", 8) or 0)
    translate_limit = int(getattr(settings, "topic_ai_translate_limit", 8) or 0)
    stats.ai_enrich_limit = max(0, min(30, enrich_limit, translate_limit))
    ai_started = time.monotonic()
    if stats.ai_enrich_limit > 0 and settings and getattr(settings, "has_ai_provider", False):
        enrichment_candidates = sorted(enrichment_candidates, key=lambda i: i.score, reverse=True)[: stats.ai_enrich_limit]
        for item in enrichment_candidates:
            try:
                before = (item.title_ru, item.summary_ru, item.angle_ru)
                await _enrich_topic_metadata_if_available(item, settings, db)
                after = (item.title_ru, item.summary_ru, item.angle_ru)
                if after != before and any(after):
                    stats.ai_enriched += 1
            except Exception as exc:
                logger.warning("Topic enrichment skipped after error: %s", exc)
                continue
    stats.ai_seconds = time.monotonic() - ai_started
    stats.total_seconds = time.monotonic() - total_started
    return stats, items, inserted


def _render_sources_status(reports: list[SourceReport]) -> str:
    total = len(reports)
    ok = sum(1 for r in reports if r.status == "ok")
    empty = sum(1 for r in reports if r.status == "empty")
    skipped = sum(1 for r in reports if r.status == "skipped")
    errors = sum(1 for r in reports if r.status == "error")
    lines = ["📡 Статус источников", "", f"Всего источников: {total}", f"Работают: {ok}", f"Пустые: {empty}", f"Отключены/пропущены: {skipped}", f"Ошибки: {errors}", "", "По группам:"]
    for group, label in SOURCE_GROUP_LABELS.items():
        group_reports = [r for r in reports if (r.source_group or "other") == group]
        if not group_reports:
            if group == "custom":
                lines.append(f"{label}: 0/0")
            continue
        group_ok = sum(1 for r in group_reports if r.status == "ok")
        group_skipped = sum(1 for r in group_reports if r.status == "skipped")
        suffix = f", пропущено {group_skipped}" if group_skipped else ""
        lines.append(f"{label}: {group_ok}/{len(group_reports)}{suffix}")

    skipped_reports = [r for r in reports if r.status == "skipped"]
    if skipped_reports:
        lines.append("")
        lines.append("Отключено/пропущено:")
        for rep in skipped_reports[:8]:
            lines.append(f"- {rep.name}: {rep.error or 'пропущено'}")

    problems = [r for r in reports if r.status in {"error", "empty"}]
    if problems:
        lines.append("")
        lines.append("Проблемы:")
        limited = problems[:12]
        for rep in limited:
            if rep.status == "error":
                lines.append(f"- {rep.name}: {rep.error or 'ошибка'}")
            else:
                lines.append(f"- {rep.name}: 0 тем")
        if len(problems) > 12:
            lines.append("Показаны первые 12 проблем.")
    return "\n".join(lines)[:3900]


def _render_collect_topic_line(topic) -> list[str]:
    preview = topic_compact_preview_ru(topic, max_len=160).splitlines()
    score = int(getattr(topic, "score", 0) or 0) if not isinstance(topic, dict) else int(topic.get("score") or 0)
    category = getattr(topic, "category", None) if not isinstance(topic, dict) else topic.get("category")
    title = preview[0] if preview else topic_display_title(topic)
    details = preview[1:] or [f"  О чем: {topic_summary_ru(topic)}"]
    return [f"- {score} - {_category_label(category)} - {title}", *details]


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
        f"Время: {int(round(stats.total_seconds))} сек. Обогащено AI: {stats.ai_enriched} тем.",
        "",
    ]
    if debug:
        lines.extend(
            [
                f"Источники: {stats.source_seconds:.1f} сек",
                f"Сохранение/скоринг: {stats.store_seconds:.1f} сек",
                f"AI-обогащение: {stats.ai_seconds:.1f} сек",
                f"AI-обогащено: {stats.ai_enriched} / {stats.ai_enrich_limit}",
                "",
            ]
        )
    if inserted:
        top = sorted(inserted, key=lambda i: i.score, reverse=True)[:5]
        lines.append("Лучшие новые:")
        for item in top:
            lines.extend(_render_collect_topic_line(item))
    else:
        lines.append("Новых сильных тем нет. Посмотри старые через /topics_all или добавь источники в CUSTOM_TOPIC_FEEDS.")
    lively = [i for i in sorted(items, key=lambda i: i.score, reverse=True) if i.score >= 50 and (i.source_group in {"community","github","x","tools","custom"} or i.category in {"drama","meme","guide","creator"})][:5]
    lines.extend(["", "Живые темы:"])
    if lively:
        for item in lively:
            lines.extend(_render_collect_topic_line(item))
    else:
        lines.append("- пока нет")
    return "\n".join(lines)


async def collect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    progress_message = None
    if update.message:
        progress_message = await update.message.reply_text("🔄 Собираю темы... Это может занять до пары минут.")

    stats, items, inserted = await _collect_topics_with_stats(db, settings=settings)

    if update.message:
        text = _render_collect_text(stats, items, inserted)
        if progress_message:
            try:
                await progress_message.edit_text(text, reply_markup=_collect_result_keyboard())
                return
            except Exception as exc:
                logger.warning("Failed to edit collect progress message: %s", exc)
        await update.message.reply_text(text, reply_markup=_collect_result_keyboard())


async def sources_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    if update.message:
        await update.message.reply_text("Проверяю источники...")
    _items, reports = await _run_collect_topics_with_diagnostics()
    if update.message:
        await update.message.reply_text(_render_sources_status(reports), reply_markup=_admin_reply_keyboard())


async def collect_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    progress_message = None
    if update.message:
        progress_message = await update.message.reply_text("🔄 Собираю темы... Это может занять до пары минут.")
    debug_started = time.monotonic()
    items, reports = await _run_collect_topics_with_diagnostics()
    source_seconds = time.monotonic() - debug_started
    stats, _all_items, inserted = await _collect_topics_with_stats(db, items=items, settings=settings)
    stats.source_seconds = source_seconds
    stats.total_seconds = source_seconds + stats.store_seconds + stats.ai_seconds
    text = _render_collect_text(stats, items, inserted, debug=True)
    ok = sum(1 for r in reports if r.status == "ok")
    empty = sum(1 for r in reports if r.status == "empty")
    skipped = sum(1 for r in reports if r.status == "skipped")
    errors = sum(1 for r in reports if r.status == "error")
    problems = [r for r in reports if r.status in {"error", "empty"}][:12]
    problem_lines = [f"- {r.name}: {r.error or 'empty'}" if r.status == "error" else f"- {r.name}: empty" for r in problems]
    skipped_lines = [f"- {r.name}: {r.error or 'пропущено'}" for r in reports if r.status == "skipped"][:8]
    combined = text.replace("🧠 Темы собраны", "🧠 Темы собраны с диагностикой")
    combined += f"\n\nСвежесть: пропущено старых тем: {stats.stale} (лимит {getattr(settings, 'max_topic_age_days', 14)} дн.)"
    combined += f"\n\nИсточники:\nРаботают: {ok}\nПустые: {empty}\nОтключены/пропущены: {skipped}\nОшибки: {errors}"
    if skipped_lines:
        combined += "\n\nОтключено/пропущено:\n" + "\n".join(skipped_lines)
    if problem_lines:
        combined += "\n\nПроблемы:\n" + "\n".join(problem_lines)
    if len([r for r in reports if r.status in {'error', 'empty'}]) > 12:
        combined += "\nПоказаны первые 12 проблем."
    if update.message:
        if progress_message:
            try:
                await progress_message.edit_text(combined[:3900], reply_markup=_collect_result_keyboard())
                return
            except Exception as exc:
                logger.warning("Failed to edit collect debug progress message: %s", exc)
        await update.message.reply_text(combined[:3900], reply_markup=_collect_result_keyboard())



async def _send_topic_cards(context: ContextTypes.DEFAULT_TYPE, settings, topics: list[dict]) -> None:
    for topic in topics:
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=_topic_card_text(topic),
            reply_markup=_topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or "")),
            link_preview_options=_disabled_link_preview_options(),
        )


async def topics_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    hot_topics = _topics_for_kind(db, "hot", limit=5)
    new_topics = _topics_for_kind(db, "new", limit=5)
    if not hot_topics and not new_topics:
        text = _render_topics_hub_text(db) + "\n\nТем пока нет. Запусти /collect или /collect_debug."
        if update.message:
            await update.message.reply_text(text, reply_markup=_topics_hub_keyboard())
        return
    if update.message:
        await update.message.reply_text(_render_topics_hub_text(db), reply_markup=_topics_hub_keyboard())
    if hot_topics:
        if update.message:
            await update.message.reply_text(_render_topic_preview_list("🔥 Лучшие горячие", hot_topics), reply_markup=_topics_hub_keyboard())
    else:
        if update.message:
            await update.message.reply_text(
                "Горячих тем пока нет, но есть свежие темы. Показываю лучшие новые.",
                reply_markup=_topics_hub_keyboard(),
            )
            await update.message.reply_text(_render_topic_preview_list("🆕 Лучшие новые", new_topics), reply_markup=_topics_hub_keyboard())



async def topics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    limit = _parse_topic_limit(context, default=10)
    topics = db.list_topic_candidates(limit=limit, status="new", order_by_score=True)
    if not topics:
        if update.message:
            await update.message.reply_text("Пока нет тем. Запусти /collect")
        return

    for topic in topics:
        text = _topic_card_text(topic)
        keyboard = _topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or ""))
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=_disabled_link_preview_options(),
        )
    if update.message:
        next_limit = min(30, max(limit + 10, 20))
        await update.message.reply_text(f"Показал {len(topics)} тем. Можно открыть больше: /topics {next_limit}")


async def topics_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    topics = db.list_topic_candidates(limit=15, status=None, order_by_score=True)
    if not topics:
        if update.message:
            await update.message.reply_text("Пока нет тем. Запусти /collect")
        return
    for topic in topics:
        status = topic.get("status") or "new"
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=f"{_topic_card_text(topic)}\nСтатус: {status}",
            link_preview_options=_disabled_link_preview_options(),
        )




async def topics_tools_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _topics_filtered_command(update, context, categories=["tool", "creator", "guide", "dev", "mobile"], command_name="topics_tools")


async def topics_news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _topics_filtered_command(update, context, categories=["news", "model", "agent", "research", "business", "privacy"], command_name="topics_news")


async def topics_fun_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _topics_fun_command(update, context)


async def _topics_fun_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    topics_by_category = db.list_topic_candidates_filtered(limit=20, status="new", categories=["drama", "meme"])
    topics_by_group = db.list_topic_candidates_filtered(limit=20, status="new", source_groups=["community", "github", "x", "custom"])

    merged: dict[int, dict] = {}
    for topic in topics_by_category + topics_by_group:
        topic_id = int(topic["id"])
        merged[topic_id] = topic

    limit = _parse_topic_limit(context, default=10)
    topics = sorted(merged.values(), key=lambda t: (int(t.get("score") or 0), str(t.get("created_at") or "")), reverse=True)[:limit]
    if not topics:
        if update.message:
            await update.message.reply_text("По фильтру пока нет тем. Запусти /collect")
        return

    for topic in topics:
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=_topic_card_text(topic),
            reply_markup=_topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or "")),
            link_preview_options=_disabled_link_preview_options(),
        )
    if update.message:
        next_limit = min(30, max(limit + 10, 20))
        await update.message.reply_text(f"Показал {len(topics)} тем. Можно открыть больше: /topics_fun {next_limit}")


async def _topics_filtered_command(update: Update, context: ContextTypes.DEFAULT_TYPE, categories=None, source_groups=None, command_name: str = "topics") -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    limit = _parse_topic_limit(context, default=10)
    topics = db.list_topic_candidates_filtered(limit=limit, status="new", categories=categories, source_groups=source_groups)
    if not topics:
        if update.message:
            await update.message.reply_text("По фильтру пока нет тем. Запусти /collect")
        return
    for topic in topics:
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=_topic_card_text(topic),
            reply_markup=_topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or "")),
            link_preview_options=_disabled_link_preview_options(),
        )
    if update.message:
        next_limit = min(30, max(limit + 10, 20))
        await update.message.reply_text(f"Показал {len(topics)} тем. Можно открыть больше: /{command_name} {next_limit}")


async def topics_hot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    limit = _parse_topic_limit(context, default=15)
    topics = db.list_topic_candidates_min_score(limit=limit, status="new", min_score=75)
    if not topics:
        fallback_topics = db.list_topic_candidates(limit=limit, status="new", order_by_score=True)
        if fallback_topics:
            if update.message:
                await update.message.reply_text("Горячих тем пока нет, но есть свежие темы. Показываю лучшие новые.", reply_markup=_topics_hub_keyboard())
            topics = fallback_topics
        else:
            if update.message:
                await update.message.reply_text("Тем пока нет. Запусти /collect или /collect_debug.", reply_markup=_topics_hub_keyboard())
            return
    for topic in topics:
        await context.bot.send_message(chat_id=settings.admin_id, text=_topic_card_text(topic), reply_markup=_topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or "")), link_preview_options=_disabled_link_preview_options())
    if update.message:
        await update.message.reply_text(f"Показал {len(topics)} тем. Можно открыть больше: /topics_hot 30")

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



async def _handle_topics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    query = update.callback_query
    if not query:
        return
    kind = data.split(":", 1)[0].removeprefix("topics_")
    titles = {
        "hot": "🔥 Горячие темы",
        "new": "🆕 Лучшие новые темы",
        "tools": "🛠 Инструменты",
        "news": "📰 Новости",
        "fun": "😄 Живые темы",
    }
    topics = _topics_for_kind(db, kind, limit=10)
    if kind == "hot" and not topics:
        topics = _topics_for_kind(db, "new", limit=10)
        text = "Горячих тем пока нет, но есть свежие темы. Показываю лучшие новые."
    else:
        text = _render_topic_preview_list(titles.get(kind, "🧠 Темы"), topics)
    if not topics:
        text = "Тем пока нет. Запусти /collect или /collect_debug."
    await _edit_callback_message(query, text, reply_markup=_topics_hub_keyboard())
    for topic in topics[:5]:
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=_topic_card_text(topic),
            reply_markup=_topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or "")),
            link_preview_options=_disabled_link_preview_options(),
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
        await query.answer("Только администратор может модерировать.", show_alert=True)
        return

    await query.answer()
    data = query.data or ""
    if data.startswith("menu_"):
        await _handle_menu_callback(update, context, data)
        return
    if data.startswith("topics_"):
        await _handle_topics_callback(update, context, data)
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

        if action == "topic_generate":
            topic_id = draft_id
            new_draft_id, error = await _create_draft_from_topic(
                context=context, settings=settings, db=db, topic_id=topic_id
            )
            if new_draft_id is None:
                await _edit_callback_message(query, error or "Не удалось создать черновик.")
                return
            success_text = f"Создан черновик #{new_draft_id} из темы #{topic_id}."
            if error:
                success_text = f"{success_text}\n\n⚠️ {error}"
            await _edit_callback_message(query, success_text)
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

        if action == "topic_reenrich":
            topic_id = draft_id
            await _edit_callback_message(query, f"🔁 Перевожу тему #{topic_id} заново...")
            topic, error = await _reenrich_topic_candidate_display_metadata(topic_id, settings, db)
            if error or not topic:
                await _edit_callback_message(query, error or "Не удалось заново перевести тему.")
                return
            await _edit_callback_message(
                query,
                _topic_card_text(topic),
                reply_markup=_topic_actions_keyboard(topic_id, str(topic.get("url") or "")),
            )
            return

        if action == "reject_topic":
            topic = db.get_topic_candidate(draft_id)
            if not topic:
                await _edit_callback_message(query, f"Тема #{draft_id} не найдена.")
                return
            db.update_topic_status(draft_id, "rejected")
            await _edit_callback_message(query, f"Тема #{draft_id} отклонена.")
            return

        draft = db.get_draft(draft_id)
        if not draft and action not in {"edit_cancel", "attach_media_cancel"}:
            await _edit_callback_message(query, f"Черновик #{draft_id} не найден.")
            return

        if action == "publish":
            if not _can_publish(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("publish", draft.get("status")))
                return
            was_scheduled = draft.get("status") == "scheduled"
            draft_to_publish = draft
            if was_scheduled:
                if not db.mark_draft_publishing(draft_id):
                    await _edit_callback_message(query, "Черновик уже не в очереди.")
                    return
                draft_to_publish = db.get_draft(draft_id)
                if not draft_to_publish:
                    await _edit_callback_message(query, f"Черновик #{draft_id} не найден.")
                    return
            try:
                await publish_to_channel(
                    context.bot,
                    settings.channel_id,
                    draft_to_publish["content"],
                    draft_to_publish.get("media_url"),
                    draft_to_publish.get("media_type"),
                    settings.custom_emoji_map,
                    settings.custom_emoji_aliases,
                )
            except Exception:
                if was_scheduled:
                    db.mark_draft_failed(draft_id)
                raise
            db.mark_draft_published(draft_id)
            await _edit_callback_message(query, f"✅ Черновик #{draft_id} опубликован в канал.")

        elif action == "schedule":
            if not _can_schedule(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("schedule", draft.get("status")))
                return
            db.update_status(draft_id, "approved")
            schedule_text = f"Выбери слот публикации для черновика #{draft_id} (часовой пояс: {settings.schedule_timezone}):"
            await context.bot.send_message(
                chat_id=settings.admin_id,
                text=schedule_text,
                reply_markup=_schedule_keyboard(draft_id, settings.daily_post_slots),
            )
            try:
                await query.answer("Меню слотов отправлено отдельным сообщением.")
            except Exception as answer_exc:
                logger.warning("Failed to answer schedule callback for draft #%s: %s", draft_id, answer_exc)

        elif action == "schedule_slot":
            if not slot:
                await _edit_callback_message(query, "Некорректный слот времени.")
                return
            draft_for_slot = db.get_draft(draft_id)
            if not draft_for_slot:
                await _edit_callback_message(query, f"Черновик #{draft_id} не найден.")
                return
            slot_status = str(draft_for_slot.get("status") or "")
            if slot_status not in {"draft", "approved", "scheduled"}:
                await _edit_callback_message(
                    query,
                    _status_guard_message("schedule", slot_status),
                )
                return
            tz = ZoneInfo(settings.schedule_timezone)
            now_local = datetime.now(tz)
            try:
                hour, minute = map(int, slot.split(":"))
            except (TypeError, ValueError):
                await _edit_callback_message(query, "Некорректный слот времени.")
                return
            scheduled_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled_local <= now_local:
                scheduled_local += timedelta(days=1)

            scheduled_utc = scheduled_local.astimezone(ZoneInfo("UTC"))
            db.schedule_draft(draft_id, scheduled_utc.strftime("%Y-%m-%d %H:%M:%S"))
            await _edit_callback_message(
                query,
                f"🗓️ Черновик #{draft_id} запланирован на {scheduled_local.strftime('%Y-%m-%d %H:%M')}.\nОчередь: /queue_today"
            )

        elif action == "schedule_nearest":
            if not _can_schedule(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("schedule", draft.get("status")))
                return
            try:
                scheduled_text = _schedule_draft_to_nearest_slot(db, settings, draft_id)
            except ValueError as exc:
                await _edit_callback_message(query, str(exc))
                return
            await _edit_callback_message(
                query,
                f"Черновик #{draft_id} поставлен в ближайший слот: {scheduled_text}",
                reply_markup=_queue_keyboard(db, settings, 0),
            )

        elif action == "regenerate":
            status = str(draft.get("status") or "")
            if not _can_edit(status):
                await _edit_callback_message(query, _status_guard_message("edit", status))
                return
            if not str(draft.get("source_url") or "").strip():
                await _edit_callback_message(query, "У этого черновика нет source_url, перегенерация недоступна.")
                return
            await _edit_callback_message(query, f"♻️ Перегенерирую черновик #{draft_id} из того же источника...")
            regenerated, error = await _regenerate_draft_from_source(db=db, settings=settings, draft=draft)
            if error or regenerated is None:
                await _edit_callback_message(query, error or "Не удалось перегенерировать черновик.")
                return
            refreshed = db.get_draft(draft_id) or draft
            await _edit_callback_message(
                query,
                _build_moderation_text(
                    draft_id,
                    regenerated,
                    refreshed.get("source_url"),
                    refreshed.get("media_type"),
                    refreshed.get("media_url"),
                    source_image_url=refreshed.get("source_image_url"),
                    custom_emoji_aliases=settings.custom_emoji_aliases,
                ),
                reply_markup=_moderation_keyboard(
                    draft_id,
                    "draft",
                    has_media=media_count(refreshed.get("media_url"), refreshed.get("media_type")) > 0,
                    source_url=refreshed.get("source_url"),
                    source_image_url=refreshed.get("source_image_url"),
                ),
            )

        elif action == "unschedule":
            if not draft:
                await _edit_callback_message(query, f"Черновик #{draft_id} не найден.")
                return
            if draft.get("status") != "scheduled":
                await _edit_callback_message(query, f"Черновик #{draft_id} сейчас не в очереди.")
                return
            db.unschedule_draft(draft_id)
            await _edit_callback_message(
                query,
                "Черновик #{0} снят с очереди.".format(draft_id),
                reply_markup=_queue_keyboard(db, settings, 0),
            )

        elif action == "preview":
            preview_text = strip_quote_markers(str(draft.get("content") or ""), custom_emoji_aliases=settings.custom_emoji_aliases).strip() or "[пусто]"
            await _edit_callback_message(
                query,
                preview_text,
                reply_markup=_preview_keyboard(draft_id),
            )

        elif action == "preview_back":
            refreshed = db.get_draft(draft_id) or draft
            await _edit_callback_message(
                query,
                _build_moderation_text(
                    draft_id,
                    refreshed["content"],
                    refreshed.get("source_url"),
                    refreshed.get("media_type"),
                    refreshed.get("media_url"),
                    source_image_url=refreshed.get("source_image_url"),
                    custom_emoji_aliases=settings.custom_emoji_aliases,
                ),
                reply_markup=_moderation_keyboard(
                    draft_id,
                    str(refreshed.get("status") or ""),
                    has_media=media_count(refreshed.get("media_url"), refreshed.get("media_type")) > 0,
                    source_url=refreshed.get("source_url"),
                    source_image_url=refreshed.get("source_image_url"),
                ),
            )

        elif action == "reject":
            if draft.get("status") in {"published", "publishing"}:
                await _edit_callback_message(query, "Опубликованный черновик нельзя отклонить.")
                return
            db.update_status(draft_id, "rejected")
            await _edit_callback_message(query, f"❌ Черновик #{draft_id} отклонён.")
        elif action == "restore_draft":
            if draft.get("status") != "failed":
                await _edit_callback_message(
                    query,
                    f"Черновик #{draft_id} нельзя восстановить из статуса {draft.get('status')}. "
                    "Восстановление доступно только для failed, чтобы не создать дубли публикаций.",
                )
                return
            if not db.restore_draft(draft_id):
                await _edit_callback_message(
                    query, f"Черновик #{draft_id} уже не в статусе failed и не был восстановлен."
                )
                return
            refreshed = db.get_draft(draft_id) or draft
            await _edit_callback_message(
                query,
                _build_moderation_text(
                    draft_id,
                    str(refreshed.get("content") or ""),
                    refreshed.get("source_url"),
                    refreshed.get("media_type"),
                    refreshed.get("media_url"),
                    source_image_url=refreshed.get("source_image_url"),
                    custom_emoji_aliases=settings.custom_emoji_aliases,
                ),
                reply_markup=_moderation_keyboard(
                    draft_id,
                    str(refreshed.get("status") or ""),
                    has_media=media_count(refreshed.get("media_url"), refreshed.get("media_type")) > 0,
                    source_url=refreshed.get("source_url"),
                    source_image_url=refreshed.get("source_image_url"),
                ),
            )

        elif action == "rewrite":
            if not _can_edit(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("edit", draft.get("status")))
                return
            rewritten = rewrite_test_draft(draft["content"])
            db.update_draft_content(draft_id, rewritten)
            db.update_status(draft_id, "draft")
            await _edit_callback_message(
                query,
                _build_moderation_text(draft_id, rewritten, draft.get("source_url"), custom_emoji_aliases=settings.custom_emoji_aliases),
                reply_markup=_moderation_keyboard(
                    draft_id,
                    "draft",
                    has_media=media_count(draft.get("media_url"), draft.get("media_type")) > 0,
                    source_url=draft.get("source_url"),
                    source_image_url=draft.get("source_image_url"),
                ),
            )

        elif action == "edit_text":
            if not _can_edit(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("edit", draft.get("status")))
                return
            _clear_pending_media(context)
            _set_pending_edit(context, draft_id)
            await _edit_callback_message(
                query,
                f"✏️ Редактирование черновика #{draft_id}\n\n"
                "Пришли новый текст поста одним сообщением.\n\n"
                "Чтобы отменить, нажми кнопку ниже.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Отменить редактирование", callback_data=f"edit_cancel:{draft_id}")]]
                ),
            )

        elif action == "attach_source_image":
            status = str(draft.get("status") or "")
            if not _can_edit(status):
                await _edit_callback_message(query, _status_guard_message("edit", status))
                return
            source_image_url = str(draft.get("source_image_url") or "").strip()
            if not source_image_url:
                await _edit_callback_message(query, "У черновика нет сохранённой картинки источника.")
                return
            if media_count(draft.get("media_url"), draft.get("media_type")) > 0:
                await _edit_callback_message(
                    query,
                    "У черновика уже есть медиа. Сначала убери его, потом прикрепи картинку источника.",
                )
                return
            db.attach_media(draft_id, source_image_url, "photo")
            db.update_status(draft_id, "draft")
            refreshed = db.get_draft(draft_id) or draft
            await _edit_callback_message(query, f"Картинка источника прикреплена к черновику #{draft_id}.")
            await _send_moderation_preview(
                context,
                settings.admin_id,
                draft_id,
                str(refreshed.get("content") or ""),
                source_url=refreshed.get("source_url"),
                media_url=refreshed.get("media_url"),
                media_type=refreshed.get("media_type"),
                source_image_url=refreshed.get("source_image_url"),
            )

        elif action == "attach_media_flow":
            if not _can_edit(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("edit", draft.get("status")))
                return
            _clear_pending_edit(context)
            _set_pending_media(context, draft_id)
            await _edit_callback_message(
                query,
                f"📎 Прикрепление медиа к черновику #{draft_id}\n\n"
                "Пришли одно или несколько фото/видео/GIF.\n"
                "Можно отправлять по одному сообщению.\n"
                "Когда закончишь, нажми «✅ Готово».\n\n"
                "Лимит: до 10 файлов.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ Готово", callback_data=f"attach_media_done:{draft_id}")],
                        [InlineKeyboardButton("❌ Отменить прикрепление", callback_data=f"attach_media_cancel:{draft_id}")],
                    ]
                ),
            )
        elif action == "attach_media_done":
            if _get_pending_media(context) != draft_id:
                await _edit_callback_message(query, "Нет активного режима прикрепления для этого черновика.")
                return
            status = str(draft.get("status") or "")
            if not _can_edit(status):
                _clear_pending_media(context)
                await _edit_callback_message(query, _status_guard_message("edit", status))
                return
            items = context.user_data.get("pending_media_items") or []
            if not items:
                await query.answer("Медиа ещё не добавлено. Пришли фото, видео или GIF/анимацию.", show_alert=True)
                return
            if len(items) == 1:
                db.attach_media(draft_id, items[0]["file_id"], items[0]["type"])
            else:
                db.attach_media(draft_id, encode_media_group(items[:10]), "media_group")
            db.update_status(draft_id, "draft")
            _clear_pending_media(context)
            await _edit_callback_message(query, f"Готово. Медиа прикреплено к черновику #{draft_id}: {len(items)} файл(ов).")
            refreshed = db.get_draft(draft_id) or draft
            await _send_moderation_preview(
                context,
                settings.admin_id,
                draft_id,
                str(refreshed.get('content') or ''),
                source_url=refreshed.get("source_url"),
                media_url=refreshed.get("media_url"),
                media_type=refreshed.get("media_type"),
                source_image_url=refreshed.get("source_image_url"),
            )

        elif action == "attach_media_cancel":
            _clear_pending_media(context)
            if not draft:
                await _edit_callback_message(query, "Прикрепление медиа отменено.")
                return
            await _edit_callback_message(
                query,
                _build_moderation_text(
                    draft_id,
                    draft["content"],
                    draft.get("source_url"),
                    draft.get("media_type"),
                    draft.get("media_url"),
                    source_image_url=draft.get("source_image_url"),
                    custom_emoji_aliases=settings.custom_emoji_aliases,
                ),
                reply_markup=_moderation_keyboard(
                    draft_id,
                    str(draft.get("status") or ""),
                    has_media=media_count(draft.get("media_url"), draft.get("media_type")) > 0,
                    source_url=draft.get("source_url"),
                    source_image_url=draft.get("source_image_url"),
                ),
            )
            if query.message:
                await query.message.reply_text("Прикрепление медиа отменено.")
        elif action == "remove_media":
            if not _can_edit(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("edit", draft.get("status")))
                return
            db.clear_media(draft_id)
            db.update_status(draft_id, "draft")
            await _edit_callback_message(query, f"Медиа удалено из черновика #{draft_id}.")
            refreshed = db.get_draft(draft_id) or draft
            await _send_moderation_preview(
                context,
                settings.admin_id,
                draft_id,
                str(refreshed.get("content") or ""),
                source_url=refreshed.get("source_url"),
                source_image_url=refreshed.get("source_image_url"),
            )

        elif action == "edit_cancel":
            _clear_pending_edit(context)
            if not draft:
                await _edit_callback_message(query, "Редактирование отменено.")
                return
            await _edit_callback_message(
                query,
                _build_moderation_text(
                    draft_id,
                    draft["content"],
                    draft.get("source_url"),
                    draft.get("media_type"),
                    draft.get("media_url"),
                    source_image_url=draft.get("source_image_url"),
                    custom_emoji_aliases=settings.custom_emoji_aliases,
                ),
                reply_markup=_moderation_keyboard_for_draft(draft_id, draft),
            )
            if query.message:
                await query.message.reply_text("Редактирование отменено.")

        elif action in REWRITE_DRAFT_ACTIONS:
            status = str(draft.get("status") or "")
            if status not in ACTIONABLE_DRAFT_STATUSES:
                await _edit_callback_message(query, _status_guard_message("edit", status))
                return
            if not settings.has_ai_provider:
                await _edit_callback_message(query, "AI-провайдер не настроен.")
                return
            config = _rewrite_action_config(action)
            await _edit_callback_message(query, config["progress"].format(draft_id=draft_id))
            api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
            logger.info("rewrite provider=%s model=%s mode=%s", provider, settings.model_polish, config["mode"])
            try:
                rewritten = await _run_rewrite_post_draft(
                    api_key,
                    model=settings.model_polish,
                    draft_text=draft["content"],
                    source_url=draft.get("source_url"),
                    mode=config["mode"],
                    max_chars=settings.post_max_chars,
                    soft_chars=settings.post_soft_chars,
                    base_url=base_url,
                    extra_headers=extra_headers,
                )
            except EmptyAIResponseError:
                await _edit_callback_message(
                    query,
                    EMPTY_AI_REPLY_TEXT.replace("Черновик не создан", "Черновик не обновлён"),
                )
                return
            estimated_cost = estimate_ai_cost(provider, rewritten.prompt_tokens, rewritten.completion_tokens, settings)
            db.record_ai_usage(
                provider=provider,
                model=rewritten.model or settings.model_polish,
                operation=config["operation"],
                prompt_tokens=rewritten.prompt_tokens,
                completion_tokens=rewritten.completion_tokens,
                total_tokens=rewritten.total_tokens,
                estimated_cost_usd=estimated_cost,
                source_url=draft.get("source_url"),
                draft_id=draft_id,
            )
            logger.info(
                "AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s",
                provider,
                rewritten.model or settings.model_polish,
                config["operation"],
                rewritten.prompt_tokens,
                rewritten.completion_tokens,
                rewritten.total_tokens,
                estimated_cost,
            )
            db.update_draft_content(draft_id, rewritten.content)
            db.update_status(draft_id, "draft")
            refreshed = db.get_draft(draft_id) or draft
            await _edit_callback_message(
                query,
                _build_moderation_text(
                    draft_id,
                    rewritten.content,
                    refreshed.get("source_url"),
                    refreshed.get("media_type"),
                    refreshed.get("media_url"),
                    source_image_url=refreshed.get("source_image_url"),
                    custom_emoji_aliases=settings.custom_emoji_aliases,
                ),
                reply_markup=_moderation_keyboard(
                    draft_id,
                    "draft",
                    has_media=media_count(refreshed.get("media_url"), refreshed.get("media_type")) > 0,
                    source_url=refreshed.get("source_url"),
                    source_image_url=refreshed.get("source_image_url"),
                ),
            )

        elif action == "polish":
            if not _can_edit(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("edit", draft.get("status")))
                return
            if not settings.has_ai_provider:
                await _edit_callback_message(query, "AI-провайдер не настроен.")
                return
            await _edit_callback_message(query, "Улучшаю текст через Claude...")
            api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
            logger.info("polish provider=%s model=%s", provider, settings.model_polish)
            polished = await _run_polish_post_draft(
                api_key,
                model=settings.model_polish,
                draft_text=draft["content"],
                source_url=draft.get("source_url"),
                max_chars=settings.post_max_chars,
                soft_chars=settings.post_soft_chars,
                base_url=base_url,
                extra_headers=extra_headers,
            )
            estimated_cost = estimate_ai_cost(provider, polished.prompt_tokens, polished.completion_tokens, settings)
            db.record_ai_usage(
                provider=provider, model=polished.model or settings.model_polish, operation="polish",
                prompt_tokens=polished.prompt_tokens, completion_tokens=polished.completion_tokens,
                total_tokens=polished.total_tokens, estimated_cost_usd=estimated_cost, source_url=draft.get("source_url"), draft_id=draft_id
            )
            logger.info("AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s", provider, polished.model or settings.model_polish, "polish", polished.prompt_tokens, polished.completion_tokens, polished.total_tokens, estimated_cost)
            db.update_draft_content(draft_id, polished.content)
            db.update_status(draft_id, "draft")
            await _edit_callback_message(
                query,
                _build_moderation_text(
                    draft_id,
                    polished.content,
                    draft.get("source_url"),
                    draft.get("media_type"),
                    draft.get("media_url"),
                    source_image_url=draft.get("source_image_url"),
                    custom_emoji_aliases=settings.custom_emoji_aliases,
                ),
                reply_markup=_moderation_keyboard(
                    draft_id,
                    "draft",
                    has_media=media_count(draft.get("media_url"), draft.get("media_type")) > 0,
                    source_url=draft.get("source_url"),
                    source_image_url=draft.get("source_image_url"),
                ),
            )

        elif action == "draft_info":
            reply_markup = _moderation_keyboard_for_draft(draft_id, draft)
            await _edit_callback_message(
                query,
                _full_draft_text(draft),
                reply_markup=reply_markup,
            )

        else:
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
        await _edit_callback_message(query, "🔄 Собираю темы... Это может занять до пары минут.")
        stats, items, inserted = await _collect_topics_with_stats(db, settings=settings)
        await _edit_callback_message(
            query,
            _render_collect_text(stats, items, inserted),
            reply_markup=_collect_result_keyboard(),
        )
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
            await context.bot.send_message(chat_id=settings.admin_id, text=text, reply_markup=keyboard, link_preview_options=_disabled_link_preview_options())
    elif data == "menu_sources_status":
        _items, reports = await _run_collect_topics_with_diagnostics()
        await _edit_callback_message(query, _render_sources_status(reports), reply_markup=_back_to_menu_keyboard())
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
        api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
        logger.info("url_generate provider=%s model=%s", provider, settings.model_draft)
        generation_result, used_fallback, operation = await _generate_url_draft_with_fallback(
            api_key=api_key,
            settings=settings,
            source_url=source_url,
            title=details.title,
            page_text=details.text,
            base_url=base_url,
            extra_headers=extra_headers,
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
        estimated_cost = estimate_ai_cost(provider, generation_result.prompt_tokens, generation_result.completion_tokens, settings)
        db.record_ai_usage(
            provider=provider, model=generation_result.model or settings.model_draft, operation=operation,
            prompt_tokens=generation_result.prompt_tokens, completion_tokens=generation_result.completion_tokens,
            total_tokens=generation_result.total_tokens, estimated_cost_usd=estimated_cost, source_url=source_url, draft_id=draft_id
        )
        logger.info("AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s", provider, generation_result.model or settings.model_draft, operation, generation_result.prompt_tokens, generation_result.completion_tokens, generation_result.total_tokens, estimated_cost)
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
