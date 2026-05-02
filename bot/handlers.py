"""Telegram handlers for admin commands and moderation callbacks."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.database import DraftDatabase
from bot.drafts import create_test_draft, rewrite_test_draft
from bot.publisher import publish_to_channel
from bot.writer import generate_post_draft

logger = logging.getLogger(__name__)


def _is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


def _moderation_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Publish now", callback_data=f"publish:{draft_id}")],
            [InlineKeyboardButton("Reject", callback_data=f"reject:{draft_id}")],
            [InlineKeyboardButton("Rewrite", callback_data=f"rewrite:{draft_id}")],
        ]
    )


def _build_moderation_text(draft_id: int, content: str, source_url: str | None = None) -> str:
    source = source_url or "(not provided)"
    return f"📝 Draft #{draft_id}\nSource: {source}\n\n{content}\n\nChoose an action:"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow /start only for admin user."""

    settings = context.bot_data["settings"]
    user_id = update.effective_user.id if update.effective_user else None

    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Access denied. This bot is for admin only.")
        return

    if update.message:
        await update.message.reply_text(
            "Hello admin 👋\nUse /draft to create a test draft or /generate to create an AI draft."
        )


async def draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a test draft and send moderation message to admin."""

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None

    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Access denied.")
        return

    content = create_test_draft()
    draft_id = db.create_draft(content)

    await context.bot.send_message(
        chat_id=settings.admin_id,
        text=_build_moderation_text(draft_id, content),
        reply_markup=_moderation_keyboard(draft_id),
    )

    if update.message:
        await update.message.reply_text(f"Draft #{draft_id} created and sent for moderation.")


async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate an OpenAI-powered draft and send moderation message to admin."""

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None

    if not _is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Access denied.")
        return

    source_url = " ".join(context.args).strip() if context.args else None
    if update.message:
        await update.message.reply_text("Generating draft...")

    try:
        content = generate_post_draft(settings.openai_api_key, source_url=source_url)
    except Exception as exc:
        logger.exception("Error during generation: %s", exc)
        if update.message:
            await update.message.reply_text("Failed to generate draft. Please try again.")
        return

    draft_id = db.create_draft(content, source_url=source_url)
    await context.bot.send_message(
        chat_id=settings.admin_id,
        text=_build_moderation_text(draft_id, content, source_url),
        reply_markup=_moderation_keyboard(draft_id),
    )

    if update.message:
        await update.message.reply_text(f"Generated draft #{draft_id} sent for moderation.")


async def moderation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Publish/Reject/Rewrite button clicks."""

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    query = update.callback_query

    if not query:
        return

    user_id = query.from_user.id if query.from_user else None
    if not _is_admin(user_id, settings.admin_id):
        await query.answer("Only admin can moderate.", show_alert=True)
        return

    await query.answer()

    try:
        action, draft_id_raw = query.data.split(":", maxsplit=1)
        draft_id = int(draft_id_raw)
    except (AttributeError, ValueError):
        await query.edit_message_text("Invalid action payload.")
        return

    draft = db.get_draft(draft_id)
    if not draft:
        await query.edit_message_text(f"Draft #{draft_id} not found.")
        return

    try:
        if action == "publish":
            await publish_to_channel(context.bot, settings.channel_id, draft["content"])
            db.update_status(draft_id, "published")
            await query.edit_message_text(f"✅ Draft #{draft_id} published to channel.")

        elif action == "reject":
            db.update_status(draft_id, "rejected")
            await query.edit_message_text(f"❌ Draft #{draft_id} rejected.")

        elif action == "rewrite":
            rewritten = rewrite_test_draft(draft["content"])
            db.update_draft_content(draft_id, rewritten)
            db.update_status(draft_id, "pending")
            await query.edit_message_text(
                _build_moderation_text(draft_id, rewritten, draft.get("source_url")),
                reply_markup=_moderation_keyboard(draft_id),
            )

        else:
            await query.edit_message_text("Unknown action.")

    except Exception as exc:  # Keep user-facing flow stable on runtime errors.
        logger.exception("Error while handling moderation callback: %s", exc)
        await query.edit_message_text("Something went wrong. Please try again.")
