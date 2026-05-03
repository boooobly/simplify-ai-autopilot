"""Publishing helper for sending approved drafts to channel."""

from __future__ import annotations

import logging

from telegram import Bot, InputMediaPhoto, InputMediaVideo, LinkPreviewOptions
from telegram.ext import ContextTypes

from bot.database import DraftDatabase
from bot.media_utils import decode_media_items
from bot.telegram_formatting import render_post_html, strip_quote_markers


MEDIA_CAPTION_LIMIT = 1024
logger = logging.getLogger(__name__)


def _fit_caption(text: str) -> str:
    return text if len(text) <= MEDIA_CAPTION_LIMIT else text[: MEDIA_CAPTION_LIMIT - 1].rstrip() + "…"


def _render_or_plain(text: str, custom_emoji_map: dict[str, str] | None = None) -> tuple[str, str | None]:
    try:
        return render_post_html(text, custom_emoji_map=custom_emoji_map), "HTML"
    except Exception:
        return strip_quote_markers(text), None


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
    custom_emoji_map: dict[str, str] | None = None,
) -> None:
    """Publish text or media post to the configured Telegram channel."""

    rendered_text, parse_mode = _render_or_plain(content, custom_emoji_map=custom_emoji_map)
    media_items = decode_media_items(media_url, media_type)

    if len(media_items) > 10:
        logger.warning("Draft media group too large (%s), using first 10 items", len(media_items))
        media_items = media_items[:10]

    if len(media_items) > 1:
        has_animation = any(item["type"] == "animation" for item in media_items)
        if has_animation:
            await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))
            for item in media_items:
                if item["type"] == "photo":
                    await bot.send_photo(chat_id=channel_id, photo=item["file_id"])
                elif item["type"] == "video":
                    await bot.send_video(chat_id=channel_id, video=item["file_id"])
                else:
                    await bot.send_animation(chat_id=channel_id, animation=item["file_id"])
            return
        caption = None
        caption_mode = None
        send_full_text_after = False
        if len(content) <= MEDIA_CAPTION_LIMIT:
            caption = _fit_caption(rendered_text)
            caption_mode = parse_mode
        else:
            short_caption, short_mode = _render_or_plain(_short_media_caption(content), custom_emoji_map=custom_emoji_map)
            caption = _fit_caption(short_caption)
            caption_mode = short_mode
            send_full_text_after = True
        group = []
        for idx, item in enumerate(media_items):
            if item["type"] == "photo":
                group.append(InputMediaPhoto(media=item["file_id"], caption=caption if idx == 0 else None, parse_mode=caption_mode if idx == 0 else None))
            elif item["type"] == "video":
                group.append(InputMediaVideo(media=item["file_id"], caption=caption if idx == 0 else None, parse_mode=caption_mode if idx == 0 else None))
        if group:
            await bot.send_media_group(chat_id=channel_id, media=group)
            if send_full_text_after:
                await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))
            return

    if media_items and media_items[0]["type"] == "photo":
        media_url = media_items[0]["file_id"]
        if len(content) <= MEDIA_CAPTION_LIMIT:
            await bot.send_photo(chat_id=channel_id, photo=media_url, caption=_fit_caption(rendered_text), parse_mode=parse_mode)
        else:
            short_caption, short_mode = _render_or_plain(_short_media_caption(content), custom_emoji_map=custom_emoji_map)
            await bot.send_photo(chat_id=channel_id, photo=media_url, caption=_fit_caption(short_caption), parse_mode=short_mode)
            await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))
        return
    if media_items and media_items[0]["type"] == "video":
        media_url = media_items[0]["file_id"]
        if len(content) <= MEDIA_CAPTION_LIMIT:
            await bot.send_video(chat_id=channel_id, video=media_url, caption=_fit_caption(rendered_text), parse_mode=parse_mode)
        else:
            short_caption, short_mode = _render_or_plain(_short_media_caption(content), custom_emoji_map=custom_emoji_map)
            await bot.send_video(chat_id=channel_id, video=media_url, caption=_fit_caption(short_caption), parse_mode=short_mode)
            await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))
        return
    if media_items and media_items[0]["type"] == "animation":
        media_url = media_items[0]["file_id"]
        if len(content) <= MEDIA_CAPTION_LIMIT:
            await bot.send_animation(chat_id=channel_id, animation=media_url, caption=_fit_caption(rendered_text), parse_mode=parse_mode)
        else:
            short_caption, short_mode = _render_or_plain(_short_media_caption(content), custom_emoji_map=custom_emoji_map)
            await bot.send_animation(chat_id=channel_id, animation=media_url, caption=_fit_caption(short_caption), parse_mode=short_mode)
            await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))
        return

    await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))


async def run_scheduled_publishing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Publish due scheduled drafts every minute."""

    settings = context.application.bot_data["settings"]
    db: DraftDatabase = context.application.bot_data["db"]
    due_drafts = db.get_due_scheduled_drafts()

    for draft in due_drafts:
        draft_id = int(draft["id"])
        if not db.mark_draft_publishing(draft_id):
            logger.info("Skipping draft %s because it is no longer scheduled", draft_id)
            continue
        refreshed = db.get_draft(draft_id)
        if not refreshed:
            logger.warning("Draft %s disappeared after publishing lock", draft_id)
            continue
        try:
            await publish_to_channel(
                context.bot,
                settings.channel_id,
                refreshed["content"],
                refreshed.get("media_url"),
                refreshed.get("media_type"),
                settings.custom_emoji_map,
            )
        except Exception:
            logger.exception("Scheduled publishing failed for draft %s", draft_id)
            db.mark_draft_failed(draft_id)
            continue
        db.mark_draft_published(draft_id)
