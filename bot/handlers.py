"""Telegram handlers for admin commands and moderation callbacks."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.database import DraftDatabase
from bot.drafts import create_test_draft, rewrite_test_draft
from bot.publisher import publish_to_channel
from bot.writer import (
    fetch_page_content,
    find_first_url,
    generate_post_draft,
    generate_post_draft_from_page,
    normalize_url,
)

logger = logging.getLogger(__name__)


def _is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


def _moderation_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{draft_id}")],
            [InlineKeyboardButton("✍️ Переписать", callback_data=f"rewrite:{draft_id}")],
        ]
    )


def _build_moderation_text(draft_id: int, content: str, source_url: str | None = None) -> str:
    source = source_url or "не указан"
    return f"📝 Черновик #{draft_id}\nИсточник: {source}\n\n{content}\n\nВыбери действие:"


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
            "/generate - создать черновик через ИИ\n"
            "/generate <ссылка> - создать черновик по ссылке"
        )


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

    await context.bot.send_message(
        chat_id=settings.admin_id,
        text=_build_moderation_text(draft_id, content),
        reply_markup=_moderation_keyboard(draft_id),
    )

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

    if not settings.openai_api_key:
        if update.message:
            await update.message.reply_text("OpenAI API ключ не настроен. Добавь OPENAI_API_KEY в переменные окружения и перезапусти бота.")
        return

    if update.message:
        await update.message.reply_text("Генерирую черновик...")

    try:
        content = generate_post_draft(settings.openai_api_key, source_url=source_url)
    except Exception as exc:
        logger.exception("Error during generation: %s", exc)
        if update.message:
            await update.message.reply_text("Не удалось сгенерировать черновик. Попробуй ещё раз.")
        return

    draft_id = db.create_draft(content, source_url=source_url)
    await context.bot.send_message(
        chat_id=settings.admin_id,
        text=_build_moderation_text(draft_id, content, source_url),
        reply_markup=_moderation_keyboard(draft_id),
    )

    if update.message:
        await update.message.reply_text(f"Черновик #{draft_id} создан и отправлен на модерацию.")


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
        action, draft_id_raw = query.data.split(":", maxsplit=1)
        draft_id = int(draft_id_raw)
    except (AttributeError, ValueError):
        await query.edit_message_text("Некорректное действие.")
        return

    draft = db.get_draft(draft_id)
    if not draft:
        await query.edit_message_text(f"Черновик #{draft_id} не найден.")
        return

    try:
        if action == "publish":
            await publish_to_channel(context.bot, settings.channel_id, draft["content"])
            db.update_status(draft_id, "published")
            await query.edit_message_text(f"✅ Черновик #{draft_id} опубликован в канал.")

        elif action == "reject":
            db.update_status(draft_id, "rejected")
            await query.edit_message_text(f"❌ Черновик #{draft_id} отклонён.")

        elif action == "rewrite":
            rewritten = rewrite_test_draft(draft["content"])
            db.update_draft_content(draft_id, rewritten)
            db.update_status(draft_id, "pending")
            await query.edit_message_text(
                _build_moderation_text(draft_id, rewritten, draft.get("source_url")),
                reply_markup=_moderation_keyboard(draft_id),
            )

        else:
            await query.edit_message_text("Неизвестное действие.")

    except Exception as exc:  # Keep user-facing flow stable on runtime errors.
        logger.exception("Error while handling moderation callback: %s", exc)
        await query.edit_message_text("Что-то пошло не так. Попробуй ещё раз.")


async def admin_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a draft from any URL sent by admin in a regular message."""

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    message_text = (update.message.text or "").strip() if update.message else ""

    if not _is_admin(user_id, settings.admin_id) or not message_text:
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

    if not settings.openai_api_key:
        await update.message.reply_text("OPENAI_API_KEY не настроен. Добавь ключ и перезапусти бота.")
        return

    await update.message.reply_text("Нашёл ссылку. Читаю страницу и готовлю черновик...")

    try:
        title, page_text = fetch_page_content(source_url)
        content = generate_post_draft_from_page(
            settings.openai_api_key, source_url=source_url, title=title, page_text=page_text
        )
    except Exception as exc:
        logger.exception("Failed to process URL %s: %s", source_url, exc)
        await update.message.reply_text(
            "Не удалось получить страницу или подготовить черновик. "
            "Проверь ссылку и попробуй ещё раз."
        )
        return

    draft_id = db.create_draft(content, source_url=source_url)
    await context.bot.send_message(
        chat_id=settings.admin_id,
        text=_build_moderation_text(draft_id, content, source_url),
        reply_markup=_moderation_keyboard(draft_id),
    )
    await update.message.reply_text(f"Черновик #{draft_id} готов и отправлен на модерацию.")
