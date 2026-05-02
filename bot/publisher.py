"""Publishing helper for sending approved drafts to channel."""

from __future__ import annotations

from telegram import Bot


async def publish_to_channel(bot: Bot, channel_id: str, content: str) -> None:
    """Publish a plain text post to the configured Telegram channel."""

    await bot.send_message(chat_id=channel_id, text=content)
