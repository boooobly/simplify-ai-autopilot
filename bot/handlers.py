"""Telegram handlers for admin commands and moderation callbacks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot.database import DraftDatabase
from bot.drafts import create_test_draft, rewrite_test_draft
from bot.media_utils import decode_media_items, encode_media_group, media_count
from bot.publisher import publish_to_channel
from bot.telegram_formatting import strip_quote_markers
from bot.sources import collect_topics
from bot.writer import (
    EmptyAIResponseError,
    GenerationResult,
    fetch_page_content,
    find_first_url,
    generate_post_draft,
    generate_post_draft_from_page,
    normalize_url,
    polish_post_draft,
)

logger = logging.getLogger(__name__)
ALLOWED_MEDIA_TYPES = {"photo", "video", "animation"}
ALLOWED_DRAFT_STATUSES = {"draft", "approved", "scheduled", "published", "rejected"}
ACTIONABLE_DRAFT_STATUSES = {"draft", "approved"}
TELEGRAM_CAPTION_LIMIT = 1024
SHORT_MEDIA_PREVIEW_LIMIT = 850
EMPTY_AI_REPLY_TEXT = "Модель вернула пустой ответ. Черновик не создан. Попробуй ещё раз или смени MODEL_DRAFT."


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


def _disabled_link_preview_options() -> LinkPreviewOptions:
    return LinkPreviewOptions(is_disabled=True)


def _is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


def _parse_callback_data(data: str) -> tuple[str, int, str | None]:
    if data.startswith("schedule_slot:"):
        action, draft_id_raw, slot = data.split(":", 2)
        return action, int(draft_id_raw), slot
    action, draft_id_raw = data.split(":", 1)
    return action, int(draft_id_raw), None


def _queue_draft_ids_for_day(db: DraftDatabase, settings, day_offset: int) -> list[int]:
    start_local, end_local = _get_day_range(day_offset, settings.schedule_timezone)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    return [int(d["id"]) for d in db.list_scheduled_drafts_between(start_utc, end_utc)]


def _main_menu_text() -> str:
    return "🤖 Simplify AI Autopilot\n\nВыбери действие:"


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✍️ Создать черновик", callback_data="menu_generate")],
            [InlineKeyboardButton("🔗 Пост из ссылки", callback_data="menu_url_help")],
            [InlineKeyboardButton("📝 Черновики", callback_data="menu_drafts")],
            [InlineKeyboardButton("🧠 Темы", callback_data="menu_topics")],
            [InlineKeyboardButton("📅 Очередь", callback_data="menu_queue")],
            [InlineKeyboardButton("📊 Расходы ИИ", callback_data="menu_usage")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")],
            [InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
        ]
    )


def _back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")]])


def _settings_text(settings) -> str:
    ai_provider = "OpenRouter" if settings.openrouter_api_key else ("OpenAI" if settings.openai_api_key else "не настроен")
    return (
        "⚙️ Настройки\n\n"
        f"Провайдер ИИ: {ai_provider}\n"
        f"Модель черновика: {settings.model_draft}\n"
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


def _moderation_keyboard(draft_id: int, status: str | None = None, has_media: bool = False) -> InlineKeyboardMarkup:
    if status == "scheduled":
        rows = [
            [InlineKeyboardButton("👀 Показать пост", callback_data=f"preview:{draft_id}")],
            [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data=f"publish:{draft_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{draft_id}")],
        ]
    elif status in {"published", "rejected"}:
        rows = [[InlineKeyboardButton("👀 Показать пост", callback_data=f"preview:{draft_id}")]]
    else:
        rows = [
            [InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}")],
            [InlineKeyboardButton("🗓️ Запланировать", callback_data=f"schedule:{draft_id}")],
            [InlineKeyboardButton("👀 Показать пост", callback_data=f"preview:{draft_id}")],
            [InlineKeyboardButton("✨ Улучшить Claude", callback_data=f"polish:{draft_id}")],
            [InlineKeyboardButton("✏️ Редактировать текст", callback_data=f"edit_text:{draft_id}")],
            [InlineKeyboardButton("📎 Прикрепить медиа", callback_data=f"attach_media_flow:{draft_id}")],
            *([[InlineKeyboardButton("🗑 Убрать медиа", callback_data=f"remove_media:{draft_id}")]] if has_media else []),
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{draft_id}")],
        ]
    return InlineKeyboardMarkup(rows)


def _set_pending_edit(context: ContextTypes.DEFAULT_TYPE, draft_id: int) -> None:
    context.user_data["pending_edit_draft_id"] = draft_id


def _get_pending_edit(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    draft_id = context.user_data.get("pending_edit_draft_id")
    return int(draft_id) if isinstance(draft_id, int) else None


def _clear_pending_edit(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_edit_draft_id", None)


def _set_pending_media(context: ContextTypes.DEFAULT_TYPE, draft_id: int) -> None:
    context.user_data["pending_media_draft_id"] = draft_id
    context.user_data["pending_media_items"] = []


def _get_pending_media(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    draft_id = context.user_data.get("pending_media_draft_id")
    return int(draft_id) if isinstance(draft_id, int) else None


def _clear_pending_media(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_media_draft_id", None)
    context.user_data.pop("pending_media_items", None)


def _can_publish(status: str | None) -> bool:
    return status in {"draft", "approved", "scheduled"}


def _can_schedule(status: str | None) -> bool:
    return status in {"draft", "approved"}


def _can_edit(status: str | None) -> bool:
    return status in {"draft", "approved"}


def _status_guard_message(action: str, status: str | None) -> str:
    if status == "published":
        if action == "publish":
            return "Этот черновик уже опубликован."
        if action == "schedule":
            return "Опубликованный черновик уже нельзя планировать."
        return "Опубликованный черновик уже нельзя менять."
    if status == "rejected" and action == "schedule":
        return "Отклонённый черновик нельзя планировать."
    if status == "rejected" and action == "publish":
        return "Этот черновик отклонён. Сначала создай новый или восстанови его позже."
    if status == "scheduled" and action == "edit":
        return "Запланированный черновик уже в очереди. Сначала сними его с очереди позже."
    return f"Это действие недоступно для текущего статуса: {status or 'unknown'}."


def _schedule_keyboard(draft_id: int, slots: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(slot, callback_data=f"schedule_slot:{draft_id}:{slot}")] for slot in slots]
    )


def _get_day_range(day_offset: int, timezone_str: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_str)
    now_local = datetime.now(tz)
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
    return start, start + timedelta(days=1)


def _queue_keyboard(day_offset: int, draft_ids: list[int]) -> InlineKeyboardMarkup:
    rows = []
    for draft_id in draft_ids:
        rows.append([InlineKeyboardButton(f"Снять #{draft_id} с очереди", callback_data=f"unschedule:{draft_id}")])
    if day_offset == 0:
        rows.extend([
            [InlineKeyboardButton("🔄 Обновить", callback_data="queue_today:0")],
            [InlineKeyboardButton("📅 Завтра", callback_data="queue_tomorrow:0")],
        ])
    else:
        rows.extend([
            [InlineKeyboardButton("🔄 Обновить", callback_data="queue_tomorrow:0")],
            [InlineKeyboardButton("📅 Сегодня", callback_data="queue_today:0")],
        ])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")])
    return InlineKeyboardMarkup(rows)


def _render_queue_text(db: DraftDatabase, settings, day_offset: int) -> str:
    day_name = "сегодня" if day_offset == 0 else "завтра"
    start_local, end_local = _get_day_range(day_offset, settings.schedule_timezone)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    drafts = db.list_scheduled_drafts_between(start_utc, end_utc)
    slots_map = {slot: None for slot in settings.daily_post_slots}
    extra: list[dict[str, object]] = []
    lines = [f"📅 План публикаций на {day_name}", "", "Слоты:"]
    for draft in drafts:
        scheduled_raw = str(draft.get("scheduled_at") or "")
        scheduled_utc = datetime.strptime(scheduled_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        scheduled_local = scheduled_utc.astimezone(ZoneInfo(settings.schedule_timezone))
        hhmm = scheduled_local.strftime("%H:%M")
        if hhmm in slots_map and slots_map[hhmm] is None:
            slots_map[hhmm] = draft
        else:
            extra.append(draft)
    for slot in settings.daily_post_slots:
        bound = slots_map[slot]
        lines.append(f"{slot} - #{bound['id']} scheduled" if bound else f"{slot} - пусто")
    if not drafts:
        lines.extend(["", "На этот день пока ничего не запланировано."])
        return "\n".join(lines)
    lines.extend(["", "Запланировано:"])
    for draft in drafts:
        scheduled_raw = str(draft.get("scheduled_at") or "")
        scheduled_utc = datetime.strptime(scheduled_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        local_time = scheduled_utc.astimezone(ZoneInfo(settings.schedule_timezone)).strftime("%H:%M")
        snippet = (str(draft.get("content") or "").strip()[:90] or "[пусто]").replace("\n", " ")
        lines.extend([f"#{draft['id']} - {local_time}", f"Текст: {snippet}", ""])
    if extra:
        lines.append("Дополнительно:")
        for draft in extra:
            scheduled_raw = str(draft.get("scheduled_at") or "")
            scheduled_utc = datetime.strptime(scheduled_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
            local_time = scheduled_utc.astimezone(ZoneInfo(settings.schedule_timezone)).strftime("%H:%M")
            lines.append(f"#{draft['id']} - {local_time}")
    return "\n".join(lines).rstrip()


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
) -> str:
    source = source_url or "не указан"
    count = media_count(media_url, media_type)
    media = "нет"
    if count == 1:
        items = decode_media_items(media_url, media_type)
        media = items[0]["type"] if items else "нет"
    elif count > 1:
        media = f"{count} файлов"
    body = strip_quote_markers(content).strip() or "[пусто]"
    return (
        f"📝 Черновик #{draft_id}\n"
        f"Источник: {source}\n"
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
) -> str:
    source = source_url or "не указан"
    media = media_type or "нет"
    body = strip_quote_markers(content).strip() or "[пусто]"
    snippet = body[:500]
    caption = (
        f"📝 Черновик #{draft_id}\n"
        f"Источник: {source}\n"
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
        result = generate_post_draft_from_page(
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
            result = generate_post_draft_from_page(
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




async def _send_moderation_preview(
    context: ContextTypes.DEFAULT_TYPE,
    admin_id: int,
    draft_id: int,
    content: str,
    source_url: str | None = None,
    media_url: str | None = None,
    media_type: str | None = None,
) -> None:
    text = _build_moderation_text(draft_id, content, source_url, media_type, media_url)
    has_media = media_count(media_url, media_type) > 0
    keyboard = _moderation_keyboard(draft_id, has_media=has_media)
    items = decode_media_items(media_url, media_type)
    if len(items) == 1:
        short_caption = _build_media_preview_caption(draft_id, content, source_url, media_type)
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
            "Привет 👋\nБот работает.",
            reply_markup=_main_menu_keyboard(),
            link_preview_options=_disabled_link_preview_options(),
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
            _main_menu_text(),
            reply_markup=_main_menu_keyboard(),
            link_preview_options=_disabled_link_preview_options(),
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
            "Неизвестный статус. Доступные статусы: draft, approved, scheduled, published, rejected"
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

    reply_markup = _moderation_keyboard(draft_id, str(draft.get("status") or ""))
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
            reply_markup=_queue_keyboard(0, _queue_draft_ids_for_day(db, settings, 0)),
        )


async def queue_tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        return
    if update.message:
        await update.message.reply_text(
            _render_queue_text(db, settings, day_offset=1),
            reply_markup=_queue_keyboard(1, _queue_draft_ids_for_day(db, settings, 1)),
        )


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
            title, page_text = fetch_page_content(source_url)
            generation_result, used_fallback, operation = await _generate_url_draft_with_fallback(
                api_key=api_key,
                settings=settings,
                source_url=source_url,
                title=title,
                page_text=page_text,
                base_url=base_url,
                extra_headers=extra_headers,
            )
        else:
            if message:
                await message.reply_text("Генерирую черновик...")
            generation_result = generate_post_draft(
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
    draft_id = db.create_draft(content, source_url=source_url)
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
    await _send_moderation_preview(context, settings.admin_id, draft_id, content, source_url)

    if message:
        await message.reply_text(f"Черновик #{draft_id} создан и отправлен на модерацию.")
        if source_url and used_fallback:
            logger.info(
                "Draft created with fallback model: draft_id=%s source_url=%s",
                draft_id,
                source_url,
            )


async def collect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    if update.message:
        await update.message.reply_text("Собираю свежие AI-темы из источников...")

    items = collect_topics()
    added = 0
    for item in items:
        if db.create_topic_candidate(item.title, item.url, item.source, item.published_at):
            added += 1

    if update.message:
        await update.message.reply_text(f"Готово. Найдено: {len(items)}, добавлено новых: {added}.")


async def topics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    topics = db.list_topic_candidates(limit=10)
    if not topics:
        if update.message:
            await update.message.reply_text("Пока нет тем. Запусти /collect")
        return

    for topic in topics:
        text = (
            f"🧠 Тема #{topic['id']}\n"
            f"Источник: {topic['source']}\n"
            f"Заголовок: {topic['title']}\n"
            f"URL: {topic['url']}"
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✍️ Создать пост", callback_data=f"topic_generate:{topic['id']}")]]
        )
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=_disabled_link_preview_options(),
        )


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
        await update.message.reply_text(_render_usage_text(summary, period_title, costs_enabled))


async def usage_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _usage_command(update, context, days=1, period_title="сегодня")


async def usage_7d_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _usage_command(update, context, days=7, period_title="7 дней")


async def usage_month_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _usage_command(update, context, days=30, period_title="30 дней")


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

    try:
        action, draft_id, slot = _parse_callback_data(data)
    except (AttributeError, ValueError):
        await _edit_callback_message(query, "Некорректное действие.")
        return

    try:
        if action == "topic_generate":
            topic_id = draft_id
            topic = db.get_topic_candidate(topic_id)
            if not topic:
                await _edit_callback_message(query, f"Тема #{topic_id} не найдена.")
                return
            if not settings.has_ai_provider:
                await _edit_callback_message(query, "AI-провайдер не настроен.")
                return

            api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
            logger.info("topic_generate provider=%s model=%s", provider, settings.model_draft)
            title, page_text = fetch_page_content(topic["url"])
            generation_result, used_fallback, operation = await _generate_url_draft_with_fallback(
                api_key=api_key,
                settings=settings,
                source_url=topic["url"],
                title=title,
                page_text=page_text,
                base_url=base_url,
                extra_headers=extra_headers,
            )
            if not generation_result.content.strip():
                await _edit_callback_message(query, EMPTY_AI_REPLY_TEXT)
                return
            new_draft_id = db.create_draft(generation_result.content, source_url=topic["url"])
            estimated_cost = estimate_ai_cost(provider, generation_result.prompt_tokens, generation_result.completion_tokens, settings)
            db.record_ai_usage(
                provider=provider, model=generation_result.model or settings.model_draft, operation="topic_generate",
                prompt_tokens=generation_result.prompt_tokens, completion_tokens=generation_result.completion_tokens,
                total_tokens=generation_result.total_tokens, estimated_cost_usd=estimated_cost, source_url=topic["url"], draft_id=new_draft_id
            )
            logger.info("AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s", provider, generation_result.model or settings.model_draft, "topic_generate", generation_result.prompt_tokens, generation_result.completion_tokens, generation_result.total_tokens, estimated_cost)
            await _send_moderation_preview(context, settings.admin_id, new_draft_id, generation_result.content, topic["url"])
            await _edit_callback_message(query, f"Создан черновик #{new_draft_id} из темы #{topic_id}.")
            if used_fallback:
                logger.info(
                    "Topic draft created with fallback model: topic_id=%s draft_id=%s source_url=%s",
                    topic_id,
                    new_draft_id,
                    topic["url"],
                )
            return

        if action == "queue_today":
            await _edit_callback_message(
                query,
                _render_queue_text(db, settings, day_offset=0),
                reply_markup=_queue_keyboard(0, _queue_draft_ids_for_day(db, settings, 0)),
            )
            return

        if action == "queue_tomorrow":
            await _edit_callback_message(
                query,
                _render_queue_text(db, settings, day_offset=1),
                reply_markup=_queue_keyboard(1, _queue_draft_ids_for_day(db, settings, 1)),
            )
            return

        draft = db.get_draft(draft_id)
        if not draft and action not in {"edit_cancel", "attach_media_cancel"}:
            await _edit_callback_message(query, f"Черновик #{draft_id} не найден.")
            return

        if action == "publish":
            if not _can_publish(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("publish", draft.get("status")))
                return
            await publish_to_channel(
                context.bot,
                settings.channel_id,
                draft["content"],
                draft.get("media_url"),
                draft.get("media_type"),
            )
            db.update_status(draft_id, "published")
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
                reply_markup=_queue_keyboard(0, _queue_draft_ids_for_day(db, settings, 0)),
            )

        elif action == "preview":
            preview_text = strip_quote_markers(str(draft.get("content") or "")).strip() or "[пусто]"
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
                ),
                reply_markup=_moderation_keyboard(
                    draft_id,
                    str(refreshed.get("status") or ""),
                    has_media=media_count(refreshed.get("media_url"), refreshed.get("media_type")) > 0,
                ),
            )

        elif action == "reject":
            if draft.get("status") == "published":
                await _edit_callback_message(query, "Опубликованный черновик нельзя отклонить.")
                return
            db.update_status(draft_id, "rejected")
            await _edit_callback_message(query, f"❌ Черновик #{draft_id} отклонён.")

        elif action == "rewrite":
            if not _can_edit(draft.get("status")):
                await _edit_callback_message(query, _status_guard_message("edit", draft.get("status")))
                return
            rewritten = rewrite_test_draft(draft["content"])
            db.update_draft_content(draft_id, rewritten)
            db.update_status(draft_id, "draft")
            await _edit_callback_message(
                query,
                _build_moderation_text(draft_id, rewritten, draft.get("source_url")),
                reply_markup=_moderation_keyboard(draft_id, "draft"),
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
            await _send_moderation_preview(context, settings.admin_id, draft_id, str(refreshed.get('content') or ''), source_url=refreshed.get("source_url"), media_url=refreshed.get("media_url"), media_type=refreshed.get("media_type"))

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
                ),
                reply_markup=_moderation_keyboard(draft_id, str(draft.get("status") or ""), has_media=media_count(draft.get("media_url"), draft.get("media_type")) > 0),
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
                ),
                reply_markup=_moderation_keyboard(draft_id, str(draft.get("status") or "")),
            )
            if query.message:
                await query.message.reply_text("Редактирование отменено.")

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
            polished = polish_post_draft(
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
                ),
                reply_markup=_moderation_keyboard(draft_id, "draft"),
            )

        elif action == "draft_info":
            reply_markup = _moderation_keyboard(draft_id, str(draft.get("status") or ""))
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
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔎 Собрать свежие темы", callback_data="menu_collect")],
                [InlineKeyboardButton("📋 Показать найденные темы", callback_data="menu_show_topics")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")],
            ]
        )
        await _edit_callback_message(query, "Что сделать с темами?", reply_markup=keyboard)
    elif data == "menu_collect":
        items = collect_topics()
        added = 0
        for item in items:
            if db.create_topic_candidate(item.title, item.url, item.source, item.published_at):
                added += 1
        await _edit_callback_message(
            query,
            f"Готово. Найдено: {len(items)}, добавлено новых: {added}.",
            reply_markup=_back_to_menu_keyboard(),
        )
    elif data == "menu_show_topics":
        topics = db.list_topic_candidates(limit=10)
        if not topics:
            await _edit_callback_message(query, "Пока нет тем. Запусти /collect", reply_markup=_back_to_menu_keyboard())
            return
        await _edit_callback_message(query, "Найденные темы:", reply_markup=_back_to_menu_keyboard())
        for topic in topics:
            text = f"🧠 Тема #{topic['id']}\nИсточник: {topic['source']}\nЗаголовок: {topic['title']}\nURL: {topic['url']}"
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✍️ Создать пост", callback_data=f"topic_generate:{topic['id']}")]]
            )
            await context.bot.send_message(chat_id=settings.admin_id, text=text, reply_markup=keyboard, link_preview_options=_disabled_link_preview_options())
    elif data == "menu_queue":
        await _edit_callback_message(
            query,
            _render_queue_text(db, settings, day_offset=0),
            reply_markup=_queue_keyboard(0, _queue_draft_ids_for_day(db, settings, 0)),
        )
    elif data == "menu_settings":
        await _edit_callback_message(query, _settings_text(settings), reply_markup=_back_to_menu_keyboard())
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
            "/queue_today - план публикаций на сегодня\n"
            "/queue_tomorrow - план публикаций на завтра\n"
            "/unschedule <id> - снять черновик с очереди\n"
            "/usage_today - расходы ИИ за сегодня\n"
            "/usage_7d - расходы ИИ за 7 дней\n"
            "/usage_month - расходы ИИ за 30 дней\n"
            "/collect - собрать темы\n"
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
        title, page_text = fetch_page_content(source_url)
        api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
        logger.info("url_generate provider=%s model=%s", provider, settings.model_draft)
        generation_result, used_fallback, operation = await _generate_url_draft_with_fallback(
            api_key=api_key,
            settings=settings,
            source_url=source_url,
            title=title,
            page_text=page_text,
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

    content = generation_result.content
    if not content.strip():
        await update.message.reply_text(EMPTY_AI_REPLY_TEXT)
        return
    draft_id = db.create_draft(content, source_url=source_url)
    estimated_cost = estimate_ai_cost(provider, generation_result.prompt_tokens, generation_result.completion_tokens, settings)
    db.record_ai_usage(
        provider=provider, model=generation_result.model or settings.model_draft, operation=operation,
        prompt_tokens=generation_result.prompt_tokens, completion_tokens=generation_result.completion_tokens,
        total_tokens=generation_result.total_tokens, estimated_cost_usd=estimated_cost, source_url=source_url, draft_id=draft_id
    )
    logger.info("AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s", provider, generation_result.model or settings.model_draft, operation, generation_result.prompt_tokens, generation_result.completion_tokens, generation_result.total_tokens, estimated_cost)
    await _send_moderation_preview(context, settings.admin_id, draft_id, content, source_url)
    await update.message.reply_text(f"Черновик #{draft_id} создан и отправлен на модерацию.")
    if used_fallback:
        logger.info(
            "Draft created with fallback model: draft_id=%s source_url=%s",
            draft_id,
            source_url,
        )
