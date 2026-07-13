"""Publishing helper for sending approved drafts to channel."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class PublishResult:
    message_ids: list[int]


class PartialPublishError(RuntimeError):
    """Telegram accepted some messages before a later send failed."""

    def __init__(self, message_ids: list[int], cause: Exception) -> None:
        super().__init__(f"partial publish after {len(message_ids)} message(s): {type(cause).__name__}")
        self.message_ids = list(message_ids)
        self.cause = cause


def _raise_send_failure(message_ids: list[int], exc: Exception) -> None:
    if message_ids:
        raise PartialPublishError(message_ids, exc) from exc
    raise exc


def _message_id(message: Any) -> int | None:
    value = getattr(message, "message_id", None)
    return int(value) if value is not None else None


def _message_ids(messages: Any) -> list[int]:
    if messages is None:
        return []
    if isinstance(messages, (list, tuple)):
        return [message_id for message in messages if (message_id := _message_id(message)) is not None]
    message_id = _message_id(messages)
    return [message_id] if message_id is not None else []


def _render_or_plain(
    text: str,
    custom_emoji_map: dict[str, str] | None = None,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None = None,
    *,
    strict_custom_emoji: bool = True,
) -> tuple[str, str | None]:
    try:
        rendered = render_post_html(
            text,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
            strict_custom_emoji=strict_custom_emoji,
        )
        if rendered.strip():
            return rendered, "HTML"
        logger.warning("Telegram HTML rendering produced empty output; using plain text fallback")
    except Exception as exc:
        logger.warning("Telegram HTML rendering failed; using plain text fallback error=%s", type(exc).__name__)

    plain_text = strip_quote_markers(
        text,
        custom_emoji_aliases=custom_emoji_aliases,
        strict_custom_emoji=strict_custom_emoji,
    )
    if plain_text.strip():
        return plain_text, None

    safe_fallback = strip_quote_markers(
        text,
        custom_emoji_aliases=custom_emoji_aliases,
        strict_custom_emoji=False,
    )
    return (safe_fallback if safe_fallback.strip() else text.strip()), None


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
) -> PublishResult:
    """Publish text or media post to the configured Telegram channel."""

    rendered_text, parse_mode = _render_or_plain(content, custom_emoji_map=custom_emoji_map, custom_emoji_aliases=custom_emoji_aliases)
    media_items = decode_media_items(media_url, media_type)

    if len(media_items) > 10:
        logger.warning("Draft media group too large (%s), using first 10 items", len(media_items))
        media_items = media_items[:10]

    if len(media_items) > 1:
        has_animation = any(item["type"] == "animation" for item in media_items)
        if has_animation:
            sent_messages: list[int] = []
            try:
                sent_messages.extend(_message_ids(await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))))
                for item in media_items:
                    if item["type"] == "photo":
                        sent_messages.extend(_message_ids(await bot.send_photo(chat_id=channel_id, photo=item["file_id"])))
                    elif item["type"] == "video":
                        sent_messages.extend(_message_ids(await bot.send_video(chat_id=channel_id, video=item["file_id"])))
                    else:
                        sent_messages.extend(_message_ids(await bot.send_animation(chat_id=channel_id, animation=item["file_id"])))
            except Exception as exc:
                _raise_send_failure(sent_messages, exc)
            return PublishResult(message_ids=sent_messages)
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
            sent_messages = []
            try:
                sent_messages.extend(_message_ids(await bot.send_media_group(chat_id=channel_id, media=group)))
                if safe_caption.send_full_text_after:
                    sent_messages.extend(_message_ids(await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))))
            except Exception as exc:
                _raise_send_failure(sent_messages, exc)
            return PublishResult(message_ids=sent_messages)

    if media_items and media_items[0]["type"] == "photo":
        media_url = media_items[0]["file_id"]
        safe_caption = _prepare_media_caption(
            content,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
        )
        sent_messages = []
        try:
            sent_messages.extend(_message_ids(await bot.send_photo(
                chat_id=channel_id,
                photo=media_url,
                caption=safe_caption.text,
                parse_mode=safe_caption.parse_mode,
            )))
            if safe_caption.send_full_text_after:
                sent_messages.extend(_message_ids(await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))))
        except Exception as exc:
            _raise_send_failure(sent_messages, exc)
        return PublishResult(message_ids=sent_messages)
    if media_items and media_items[0]["type"] == "video":
        media_url = media_items[0]["file_id"]
        safe_caption = _prepare_media_caption(
            content,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
        )
        sent_messages = []
        try:
            sent_messages.extend(_message_ids(await bot.send_video(
                chat_id=channel_id,
                video=media_url,
                caption=safe_caption.text,
                parse_mode=safe_caption.parse_mode,
            )))
            if safe_caption.send_full_text_after:
                sent_messages.extend(_message_ids(await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))))
        except Exception as exc:
            _raise_send_failure(sent_messages, exc)
        return PublishResult(message_ids=sent_messages)
    if media_items and media_items[0]["type"] == "animation":
        media_url = media_items[0]["file_id"]
        safe_caption = _prepare_media_caption(
            content,
            custom_emoji_map=custom_emoji_map,
            custom_emoji_aliases=custom_emoji_aliases,
        )
        sent_messages = []
        try:
            sent_messages.extend(_message_ids(await bot.send_animation(
                chat_id=channel_id,
                animation=media_url,
                caption=safe_caption.text,
                parse_mode=safe_caption.parse_mode,
            )))
            if safe_caption.send_full_text_after:
                sent_messages.extend(_message_ids(await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))))
        except Exception as exc:
            _raise_send_failure(sent_messages, exc)
        return PublishResult(message_ids=sent_messages)

    return PublishResult(message_ids=_message_ids(await bot.send_message(chat_id=channel_id, text=rendered_text, parse_mode=parse_mode, link_preview_options=LinkPreviewOptions(is_disabled=True))))


async def run_scheduled_publishing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Publish due scheduled drafts every minute."""

    settings = context.application.bot_data["settings"]
    db: DraftDatabase = context.application.bot_data["db"]
    recovered_count = db.recover_stuck_publishing_drafts()
    if recovered_count:
        logger.warning(
            "scheduled_publish recovered_stale_publishing count=%s status_transition=publishing->failed",
            recovered_count,
        )
    due_drafts = db.get_due_scheduled_drafts()
    logger.info("scheduled_publish scan due_count=%s", len(due_drafts))

    for draft in due_drafts:
        draft_id = int(draft["id"])
        logger.info("scheduled_publish claim_attempt draft_id=%s status_transition=scheduled->publishing", draft_id)
        if not db.mark_draft_publishing(draft_id):
            logger.info("scheduled_publish claim_skip draft_id=%s reason=status_changed", draft_id)
            continue
        logger.info("scheduled_publish claim_success draft_id=%s status_transition=scheduled->publishing", draft_id)
        refreshed = db.get_draft(draft_id)
        if not refreshed:
            logger.warning("scheduled_publish draft_missing_after_claim draft_id=%s", draft_id)
            continue
        try:
            result = await publish_to_channel(
                context.bot,
                settings.channel_id,
                refreshed["content"],
                refreshed.get("media_url"),
                refreshed.get("media_type"),
                settings.custom_emoji_map,
                settings.custom_emoji_aliases,
            )
        except Exception as exc:
            partial_message_ids = list(getattr(exc, "message_ids", []) or [])
            if partial_message_ids:
                db.mark_draft_published(
                    draft_id,
                    channel_id=settings.channel_id,
                    message_ids=partial_message_ids,
                    error=f"PartialPublish:{type(getattr(exc, 'cause', exc)).__name__}",
                )
                logger.error(
                    "scheduled_publish partial draft_id=%s message_count=%s retry_blocked=true",
                    draft_id,
                    len(partial_message_ids),
                )
                try:
                    await context.bot.send_message(
                        chat_id=settings.admin_id,
                        text=(
                            f"⚠️ Черновик #{draft_id} опубликован частично ({len(partial_message_ids)} сообщений). "
                            "Автоповтор заблокирован, чтобы не создать дубли. Проверь канал вручную."
                        ),
                    )
                except Exception:
                    logger.exception("scheduled_publish failed to notify admin about partial publish draft_id=%s", draft_id)
                continue
            logger.exception("scheduled_publish failure draft_id=%s status_transition=publishing->failed", draft_id)
            db.mark_draft_failed(draft_id, error=type(exc).__name__)
            continue
        db.mark_draft_published(draft_id, channel_id=settings.channel_id, message_ids=result.message_ids)
        logger.info(
            "scheduled_publish success draft_id=%s status_transition=publishing->published message_count=%s",
            draft_id,
            len(result.message_ids),
        )
