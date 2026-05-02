"""Publishing helper for sending approved drafts to channel."""

from __future__ import annotations

from telegram import Bot
from telegram.ext import ContextTypes

from bot.database import DraftDatabase


async def publish_to_channel(bot: Bot, channel_id: str, content: str) -> None:
    """Publish a plain text post to the configured Telegram channel."""

    await bot.send_message(chat_id=channel_id, text=content)


async def run_scheduled_publishing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Publish due scheduled drafts every minute."""

    settings = context.application.bot_data["settings"]
    db: DraftDatabase = context.application.bot_data["db"]
    due_drafts = db.get_due_scheduled_drafts()

    for draft in due_drafts:
        await publish_to_channel(context.bot, settings.channel_id, draft["content"])
        db.update_status(int(draft["id"]), "published")
