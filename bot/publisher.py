"""Publishing helper for sending approved drafts to channel."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from telegram import Bot, InputMediaPhoto, InputMediaVideo, LinkPreviewOptions
from telegram.ext import ContextTypes

from bot.database import DraftDatabase
from bot.media_utils import decode_media_items
from bot.telegram_formatting import render_post_html, strip_quote_markers


MEDIA_CAPTION_LIMIT = 1024
MEDIA_PREVIEW_CAPTION_LIMIT = 300
INTERNAL_MARKER_PATTERN = re.compile(
    r"\[\[LINK:.+?\|.+?\]\]|\[\[EMOJI:[a-zA-Z0-9_-]+\]\]|\[\[/?QUOTE\]\]",
    re.DOTALL,
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SafeCaption:
    text: str
    parse_mode: str | None
    send_full_text_after: bool


def _render_or_plain(
    text: str,
    custom_emoji_map: dict[str, str] | None = None,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None = None,
    *,
    strict_custom_emoji: bool = True,
) -> tuple[str, str | None]:
    try:
        return render_post_html(
            text,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
            strict_custom_emoji=strict_custom_emoji,
        ), "HTML"
    except Exception:
        return strip_quote_markers(
            text,
            custom_emoji_aliases=custom_emoji_aliases,
            strict_custom_emoji=strict_custom_emoji,
        ), None


def _remove_incomplete_trailing_marker(text: str) -> str:
    """Drop an incomplete internal marker fragment at the end of a preview."""

    marker_start = text.rfind("[[")
    marker_end = text.rfind("]]", marker_start) if marker_start != -1 else -1
    if marker_start != -1 and marker_end == -1:
        return text[:marker_start].rstrip()
    return text


def _shorten_internal_text(text: str, limit: int = MEDIA_PREVIEW_CAPTION_LIMIT) -> str:
    """Shorten source post text without splitting internal Telegram markers."""

    if len(text) <= limit:
        return _remove_incomplete_trailing_marker(text)

    cut = max(0, limit - 1)
    for match in INTERNAL_MARKER_PATTERN.finditer(text):
        if match.start() < cut < match.end():
            cut = match.start()
            break

    shortened = _remove_incomplete_trailing_marker(text[:cut]).rstrip()
    return f"{shortened}…" if shortened else "…"


def _prepare_media_caption(
    text: str,
    custom_emoji_map: dict[str, str] | None = None,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None = None,
) -> SafeCaption:
    """Build a Telegram caption by shortening internal text before HTML rendering."""

    send_full_text_after = len(text) > MEDIA_CAPTION_LIMIT
    caption_source = _shorten_internal_text(text) if send_full_text_after else text
    caption_text, caption_mode = _render_or_plain(
        caption_source,
        custom_emoji_map=custom_emoji_map,
        custom_emoji_aliases=custom_emoji_aliases,
    )
    return SafeCaption(caption_text, caption_mode, send_full_text_after)


async def publish_to_channel(
    bot: Bot,
    channel_id: str,
    content: str,
    media_url: str | None = None,
    media_type: str | None = None,
    custom_emoji_map: dict[str, str] | None = None,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Publish text or media post to the configured Telegram channel."""

    rendered_text, parse_mode = _render_or_plain(content, custom_emoji_map=custom_emoji_map, custom_emoji_aliases=custom_emoji_aliases)
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
        safe_caption = _prepare_media_caption(
            content,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
        )
        caption = safe_caption.text
        caption_mode = safe_caption.parse_mode
        group = []
        for idx, item in enumerate(media_items):
            if item["type"] == "photo":
                group.append(InputMediaPhoto(media=item["file_id"], caption=caption if idx == 0 else None, parse_mode=caption_mode if idx == 0 else None))
            elif item["type"] == "video":
                group.append(InputMediaVideo(media=item["file_id"], caption=caption if idx == 0 else None, parse_mode=caption_mode if idx == 0 else None))
        if group:
            await bot.send_media_group(chat_id=channel_id, media=group)
            if safe_caption.send_full_text_after:
                await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))
            return

    if media_items and media_items[0]["type"] == "photo":
        media_url = media_items[0]["file_id"]
        safe_caption = _prepare_media_caption(
            content,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
        )
        await bot.send_photo(
            chat_id=channel_id,
            photo=media_url,
            caption=safe_caption.text,
            parse_mode=safe_caption.parse_mode,
        )
        if safe_caption.send_full_text_after:
            await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))
        return
    if media_items and media_items[0]["type"] == "video":
        media_url = media_items[0]["file_id"]
        safe_caption = _prepare_media_caption(
            content,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
        )
        await bot.send_video(
            chat_id=channel_id,
            video=media_url,
            caption=safe_caption.text,
            parse_mode=safe_caption.parse_mode,
        )
        if safe_caption.send_full_text_after:
            await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))
        return
    if media_items and media_items[0]["type"] == "animation":
        media_url = media_items[0]["file_id"]
        safe_caption = _prepare_media_caption(
            content,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
        )
        await bot.send_animation(
            chat_id=channel_id,
            animation=media_url,
            caption=safe_caption.text,
            parse_mode=safe_caption.parse_mode,
        )
        if safe_caption.send_full_text_after:
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
                settings.custom_emoji_aliases,
            )
        except Exception:
            logger.exception("Scheduled publishing failed for draft %s", draft_id)
            db.mark_draft_failed(draft_id)
            continue
        db.mark_draft_published(draft_id)
