"""Telegram handlers for admin commands and moderation callbacks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.database import DraftDatabase
from bot.drafts import create_test_draft, rewrite_test_draft
from bot.publisher import publish_to_channel
from bot.sources import collect_topics
from bot.writer import (
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


def _is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


def _resolve_ai_provider(settings) -> tuple[str, str, str | None, dict[str, str] | None]:
    if settings.openrouter_api_key:
        headers = {"X-Title": settings.openrouter_app_name}
        if settings.openrouter_site_url:
            headers["HTTP-Referer"] = settings.openrouter_site_url
        return settings.openrouter_api_key, "openrouter", "https://openrouter.ai/api/v1", headers
    return settings.openai_api_key, "openai", None, None


def _moderation_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}")],
            [InlineKeyboardButton("🗓️ Запланировать", callback_data=f"schedule:{draft_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{draft_id}")],
            [InlineKeyboardButton("✍️ Переписать", callback_data=f"rewrite:{draft_id}")],
            [InlineKeyboardButton("✨ Улучшить Claude", callback_data=f"polish:{draft_id}")],
        ]
    )


def _schedule_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    slots = ["10:00", "14:00", "18:00", "21:00"]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(slot, callback_data=f"schedule_slot:{draft_id}:{slot}")] for slot in slots]
    )


def _build_moderation_text(
    draft_id: int,
    content: str,
    source_url: str | None = None,
    media_type: str | None = None,
    media_url: str | None = None,
) -> str:
    source = source_url or "не указан"
    media_line = f"\nМедиа: {media_type}" if media_type and media_url else "\nМедиа: нет"
    return f"📝 Черновик #{draft_id}\nИсточник: {source}{media_line}\n\n{content}\n\nВыбери действие:"


def _is_media_callback_message(query) -> bool:
    message = query.message if query else None
    if not message:
        return False
    return bool(message.photo or message.video or message.animation or message.document or message.caption)


async def _edit_callback_message(
    query, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    if _is_media_callback_message(query):
        if len(text) <= TELEGRAM_CAPTION_LIMIT:
            await query.edit_message_caption(caption=text, reply_markup=reply_markup)
            return
        await query.edit_message_caption(
            caption="Готово. Полный текст отправил отдельным сообщением ниже.",
            reply_markup=reply_markup,
        )
        if query.message:
            await query.message.reply_text(text)
        return
    await query.edit_message_text(text, reply_markup=reply_markup)


def _build_media_preview_caption(
    draft_id: int,
    content: str,
    source_url: str | None = None,
    media_type: str | None = None,
) -> str:
    source = source_url or "не указан"
    media = media_type or "неизвестно"
    snippet = content[:500]
    caption = (
        f"📝 Черновик #{draft_id}\n"
        f"Источник: {source}\n"
        f"Медиа: {media}\n\n"
        f"{snippet}"
    )
    if len(content) > 500:
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
    keyboard = _moderation_keyboard(draft_id)
    if media_url and media_type in {"photo", "video", "animation"}:
        short_caption = _build_media_preview_caption(draft_id, content, source_url, media_type)
        if media_type == "photo":
            await context.bot.send_photo(chat_id=admin_id, photo=media_url, caption=short_caption, reply_markup=keyboard)
            return
        if media_type == "video":
            await context.bot.send_video(chat_id=admin_id, video=media_url, caption=short_caption, reply_markup=keyboard)
            return
        if media_type == "animation":
            await context.bot.send_animation(chat_id=admin_id, animation=media_url, caption=short_caption, reply_markup=keyboard)
            return
    await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
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
            "Привет 👋\n"
            "Бот работает.\n\n"
            "Команды:\n"
            "/draft - создать тестовый черновик\n"
            "/generate - создать черновик через draft-модель\n"
            "/generate <ссылка> - создать черновик по ссылке\n"
            "После генерации можно нажать кнопку «✨ Улучшить Claude» для полировки текста.\n"
            "/collect - собрать свежие темы\n"
            "/topics - показать найденные темы\n"
            "/attach_media <id> <photo|video|animation> <url> - прикрепить медиа\n"
            "/drafts - показать последние черновики\n"
            "/drafts <status> - показать черновики по статусу\n"
            "/draft_info <id> - открыть черновик\n"
            "/delete_draft <id> - удалить черновик"
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

    reply_markup = _moderation_keyboard(draft_id) if draft.get("status") in ACTIONABLE_DRAFT_STATUSES else None
    await update.message.reply_text(_full_draft_text(draft), reply_markup=reply_markup)


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

    source_url = " ".join(context.args).strip() if context.args else None

    if not settings.has_ai_provider:
        if update.message:
            await update.message.reply_text("AI-провайдер не настроен. Добавь OPENROUTER_API_KEY или OPENAI_API_KEY и перезапусти бота.")
        return

    if update.message:
        await update.message.reply_text("Генерирую черновик...")

    try:
        api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
        logger.info("/generate provider=%s model=%s", provider, settings.model_draft)
        content = generate_post_draft(api_key, model=settings.model_draft, source_url=source_url, base_url=base_url, extra_headers=extra_headers)
    except Exception as exc:
        logger.exception("Error during generation: %s", exc)
        if update.message:
            await update.message.reply_text("Не удалось сгенерировать черновик. Попробуй ещё раз.")
        return

    draft_id = db.create_draft(content, source_url=source_url)
    await _send_moderation_preview(context, settings.admin_id, draft_id, content, source_url)

    if update.message:
        await update.message.reply_text(f"Черновик #{draft_id} создан и отправлен на модерацию.")


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
        await context.bot.send_message(chat_id=settings.admin_id, text=text, reply_markup=keyboard)


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

    try:
        parts = (query.data or "").split(":")
        action = parts[0]
        if action == "schedule_slot":
            draft_id = int(parts[1])
            slot = parts[2]
        else:
            draft_id = int(parts[1])
            slot = None
    except (AttributeError, ValueError, IndexError):
        await _edit_callback_message(query, "Некорректное действие.")
        return

    draft = db.get_draft(draft_id)
    if not draft:
        await _edit_callback_message(query, f"Черновик #{draft_id} не найден.")
        return

    try:
        if action == "publish":
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
            db.update_status(draft_id, "approved")
            await _edit_callback_message(
                query,
                f"Выбери слот публикации для черновика #{draft_id} (часовой пояс: {settings.schedule_timezone}):",
                reply_markup=_schedule_keyboard(draft_id),
            )

        elif action == "schedule_slot":
            if slot is None:
                await _edit_callback_message(query, "Некорректный слот времени.")
                return
            tz = ZoneInfo(settings.schedule_timezone)
            now_local = datetime.now(tz)
            hour, minute = map(int, slot.split(":"))
            scheduled_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled_local <= now_local:
                scheduled_local += timedelta(days=1)

            scheduled_utc = scheduled_local.astimezone(ZoneInfo("UTC"))
            db.schedule_draft(draft_id, scheduled_utc.strftime("%Y-%m-%d %H:%M:%S"))
            await _edit_callback_message(
                query,
                f"🗓️ Черновик #{draft_id} запланирован на {scheduled_local.strftime('%Y-%m-%d %H:%M')} ({settings.schedule_timezone})."
            )

        elif action == "reject":
            db.update_status(draft_id, "rejected")
            await _edit_callback_message(query, f"❌ Черновик #{draft_id} отклонён.")

        elif action == "rewrite":
            rewritten = rewrite_test_draft(draft["content"])
            db.update_draft_content(draft_id, rewritten)
            db.update_status(draft_id, "draft")
            await _edit_callback_message(
                query,
                _build_moderation_text(draft_id, rewritten, draft.get("source_url")),
                reply_markup=_moderation_keyboard(draft_id),
            )

        elif action == "polish":
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
                base_url=base_url,
                extra_headers=extra_headers,
            )
            db.update_draft_content(draft_id, polished)
            db.update_status(draft_id, "draft")
            await _edit_callback_message(query, _build_moderation_text(draft_id, polished, draft.get("source_url")), reply_markup=_moderation_keyboard(draft_id))

        elif action == "draft_info":
            reply_markup = _moderation_keyboard(draft_id) if draft.get("status") in ACTIONABLE_DRAFT_STATUSES else None
            await _edit_callback_message(
                query,
                _full_draft_text(draft),
                reply_markup=reply_markup,
            )

        elif action == "topic_generate":
            topic = db.get_topic_candidate(draft_id)
            if not topic:
                await _edit_callback_message(query, "Тема не найдена.")
                return
            if not settings.has_ai_provider:
                await _edit_callback_message(query, "AI-провайдер не настроен.")
                return

            api_key, provider, base_url, extra_headers = _resolve_ai_provider(settings)
            logger.info("topic_generate provider=%s model=%s", provider, settings.model_draft)
            title, page_text = fetch_page_content(topic["url"])
            content = generate_post_draft_from_page(
                api_key,
                model=settings.model_draft,
                source_url=topic["url"],
                title=title,
                page_text=page_text,
                base_url=base_url,
                extra_headers=extra_headers,
            )
            new_draft_id = db.create_draft(content, source_url=topic["url"])
            await _send_moderation_preview(context, settings.admin_id, new_draft_id, content, topic["url"])
            await _edit_callback_message(query, f"Создан черновик #{new_draft_id} из темы #{topic['id']}.")

        else:
            await _edit_callback_message(query, "Неизвестное действие.")

    except Exception as exc:  # Keep user-facing flow stable on runtime errors.
        logger.exception("Error while handling moderation callback: %s", exc)
        try:
            await _edit_callback_message(query, "Что-то пошло не так. Попробуй ещё раз.")
        except Exception as edit_exc:
            logger.exception("Failed to edit callback message after error: %s", edit_exc)
            if query.message:
                await query.message.reply_text("Что-то пошло не так. Посмотри логи.")


async def admin_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a draft from any URL sent by admin in a regular message."""

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    message_text = (update.message.text or "").strip() if update.message else ""

    if not _is_admin(user_id, settings.admin_id):
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
            f"Похоже, эта ссылка уже обрабатывалась: черновик #{duplicate['id']} (статус: {duplicate['status']})."
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
        content = generate_post_draft_from_page(
            api_key, model=settings.model_draft, source_url=source_url, title=title, page_text=page_text, base_url=base_url, extra_headers=extra_headers
        )
    except Exception as exc:
        logger.exception("Failed to process URL %s: %s", source_url, exc)
        await update.message.reply_text(
            "Не удалось нормально прочитать страницу. Возможно, там мало текста, сайт закрыл доступ или страница требует JavaScript. Попробуй другую ссылку или пришли текст новости вручную."
        )
        return

    draft_id = db.create_draft(content, source_url=source_url)
    await _send_moderation_preview(context, settings.admin_id, draft_id, content, source_url)
    await update.message.reply_text(f"Черновик #{draft_id} готов и отправлен на модерацию.")
