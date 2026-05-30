"""Reusable Telegram text-length safety helpers."""

from __future__ import annotations

import logging

from telegram.error import BadRequest

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_SAFE_TEXT_LIMIT = 3900


def telegram_text_len(text: str) -> int:
    return len(text or "")


def truncate_telegram_text(text: str, limit: int = TELEGRAM_SAFE_TEXT_LIMIT) -> str:
    text = str(text or "")
    if telegram_text_len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def split_telegram_text(text: str, limit: int = TELEGRAM_SAFE_TEXT_LIMIT) -> list[str]:
    remaining = str(text or "")
    if not remaining:
        return [""]
    parts: list[str] = []
    while telegram_text_len(remaining) > limit:
        window = remaining[:limit]
        split_at = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if split_at < limit // 2:
            split_at = limit
        part = remaining[:split_at].rstrip()
        parts.append(part or remaining[:limit])
        remaining = remaining[split_at:].lstrip()
    if remaining or not parts:
        parts.append(remaining)
    return parts


def is_message_too_long_error(exc: Exception) -> bool:
    return "message_too_long" in str(exc).casefold() or "message is too long" in str(exc).casefold()


async def safe_send_message(bot, *, chat_id, text: str, reply_markup=None, limit: int = TELEGRAM_SAFE_TEXT_LIMIT, **kwargs) -> list:
    parts = split_telegram_text(text, limit=limit)
    sent = []
    for index, part in enumerate(parts):
        sent.append(
            await bot.send_message(
                chat_id=chat_id,
                text=part,
                reply_markup=reply_markup if index == len(parts) - 1 else None,
                **kwargs,
            )
        )
    return sent


async def safe_reply_text(message, text: str, reply_markup=None, limit: int = TELEGRAM_SAFE_TEXT_LIMIT, **kwargs) -> list:
    parts = split_telegram_text(text, limit=limit)
    sent = []
    for index, part in enumerate(parts):
        sent.append(
            await message.reply_text(
                part,
                reply_markup=reply_markup if index == len(parts) - 1 else None,
                **kwargs,
            )
        )
    return sent


async def safe_edit_message_text(message, text: str, reply_markup=None, limit: int = TELEGRAM_SAFE_TEXT_LIMIT, **kwargs):
    return await message.edit_text(
        truncate_telegram_text(text, limit=limit),
        reply_markup=reply_markup,
        **kwargs,
    )


async def safe_edit_or_send_callback_message(
    query,
    text: str,
    reply_markup=None,
    *,
    limit: int = TELEGRAM_SAFE_TEXT_LIMIT,
    fallback_summary: str = "Готово. Полный ответ слишком длинный для одного сообщения. Открой /collect_debug для подробностей.",
    logger: logging.Logger | None = None,
    **kwargs,
) -> bool:
    original_length = telegram_text_len(text)
    safe_text = truncate_telegram_text(text, limit=limit)
    try:
        await query.edit_message_text(safe_text, reply_markup=reply_markup, **kwargs)
        return True
    except BadRequest as exc:
        if not is_message_too_long_error(exc):
            raise
        if logger:
            logger.warning(
                "Telegram callback edit rejected as too long: original_length=%s safe_length=%s error=%s",
                original_length,
                telegram_text_len(safe_text),
                exc,
            )

    summary = truncate_telegram_text(fallback_summary, limit=min(limit, 500))
    try:
        await query.edit_message_text(summary, reply_markup=reply_markup, **kwargs)
        return True
    except Exception as summary_exc:
        if logger:
            logger.warning("Failed to edit callback with safe summary: %s", summary_exc)

    message = getattr(query, "message", None)
    if message and hasattr(message, "reply_text"):
        try:
            await safe_reply_text(message, summary, reply_markup=reply_markup, limit=min(limit, 500), **kwargs)
            return True
        except Exception as send_exc:
            if logger:
                logger.warning("Failed to send callback safe summary: %s", send_exc)

    try:
        await query.answer(text=summary, show_alert=True)
    except Exception as answer_exc:
        if logger:
            logger.warning("Failed to answer callback with safe summary alert: %s", answer_exc)
    return False
