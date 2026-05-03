"""Publishing helper for sending approved drafts to channel."""

from __future__ import annotations

from telegram import Bot, LinkPreviewOptions
from telegram.ext import ContextTypes

from bot.database import DraftDatabase


MEDIA_CAPTION_LIMIT = 1024


def _fit_caption(text: str) -> str:
    return text if len(text) <= MEDIA_CAPTION_LIMIT else text[: MEDIA_CAPTION_LIMIT - 1].rstrip() + "…"


def _short_media_caption(text: str) -> str:
    if len(text) <= 300:
        return text
    return text[:299].rstrip() + "…"


async def publish_to_channel(
    bot: Bot,
    channel_id: str,
    content: str,
    media_url: str | None = None,
    media_type: str | None = None,
) -> None:
    """Publish text or media post to the configured Telegram channel."""

    if media_url and media_type == "photo":
        if len(content) <= MEDIA_CAPTION_LIMIT:
            await bot.send_photo(chat_id=channel_id, photo=media_url, caption=content)
        else:
            await bot.send_photo(chat_id=channel_id, photo=media_url, caption=_short_media_caption(content))
            await bot.send_message(
                chat_id=channel_id,
                text=content,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        return
    if media_url and media_type == "video":
        if len(content) <= MEDIA_CAPTION_LIMIT:
            await bot.send_video(chat_id=channel_id, video=media_url, caption=content)
        else:
            await bot.send_video(chat_id=channel_id, video=media_url, caption=_short_media_caption(content))
            await bot.send_message(
                chat_id=channel_id,
                text=content,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        return
    if media_url and media_type == "animation":
        if len(content) <= MEDIA_CAPTION_LIMIT:
            await bot.send_animation(chat_id=channel_id, animation=media_url, caption=content)
        else:
            await bot.send_animation(chat_id=channel_id, animation=media_url, caption=_short_media_caption(content))
            await bot.send_message(
                chat_id=channel_id,
                text=content,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        return

    await bot.send_message(
        chat_id=channel_id,
        text=content,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def run_scheduled_publishing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Publish due scheduled drafts every minute."""

    settings = context.application.bot_data["settings"]
    db: DraftDatabase = context.application.bot_data["db"]
    due_drafts = db.get_due_scheduled_drafts()

    for draft in due_drafts:
        await publish_to_channel(
            context.bot,
            settings.channel_id,
            draft["content"],
            draft.get("media_url"),
            draft.get("media_type"),
        )
        db.update_status(int(draft["id"]), "published")
