"""Telegram channel topic source integration via Telethon."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.config import Settings
    from bot.sources import SourceReport, TopicItem

_MIN_USEFUL_CHARS = 40

_AD_PATTERNS = [
    re.compile(r"\b(реклама|advertis|sponsored|партн[её]рск|promo\s*code|промокод)\b", re.IGNORECASE),
    re.compile(r"\b(giveaway|розыгрыш|airdrops?|casino|беттинг|ставк|ваканси|webinar|вебинар)\b", re.IGNORECASE),
]


def _useful_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _looks_like_ad_or_bait(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    return any(p.search(lowered) for p in _AD_PATTERNS)


def _short_title_from_text(text: str, limit: int = 140) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    title = lines[0] if lines else "Telegram update"
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


def _message_to_topic(message, channel_username: str, lookback_since: datetime):
    if getattr(message, "service", False):
        return None
    msg_date = getattr(message, "date", None)
    if not isinstance(msg_date, datetime):
        return None
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)
    else:
        msg_date = msg_date.astimezone(timezone.utc)
    if msg_date < lookback_since:
        return None

    raw_text = (getattr(message, "raw_text", None) or getattr(message, "text", None) or "").strip()
    if not raw_text or _useful_char_count(raw_text) < _MIN_USEFUL_CHARS:
        return None
    if _looks_like_ad_or_bait(raw_text):
        return None

    message_id = getattr(message, "id", None)
    if not message_id:
        return None
    clean_channel = channel_username.strip().lstrip("@")
    title = _short_title_from_text(raw_text)
    from bot.sources import TopicItem, _with_scoring
    return _with_scoring(
        TopicItem(
            title=title,
            url=f"https://t.me/{clean_channel}/{message_id}",
            source=f"Telegram @{clean_channel}",
            source_group="telegram",
            published_at=msg_date.strftime("%Y-%m-%d %H:%M:%S"),
            original_description=raw_text,
        )
    )


def _source_report(name: str, url: str, status: str, item_count: int = 0, error: str = ""):
    from bot.sources import SourceReport

    return SourceReport(name=name, url=url, source_group="telegram", status=status, item_count=item_count, error=error)


async def fetch_telegram_channel_topics(settings: "Settings", extra_channels: list[str] | None = None):
    if not getattr(settings, "enable_telegram_channel_sources", False):
        return [], [
            _source_report(
                name="Telegram channels",
                url="https://t.me",
                status="skipped",
                error="Telegram channel sources disabled (ENABLE_TELEGRAM_CHANNEL_SOURCES=false).",
            )
        ]

    channels = list(getattr(settings, "telegram_source_channels", []) or [])
    for ch in extra_channels or []:
        if ch and ch not in channels:
            channels.append(ch)
    api_id = getattr(settings, "telegram_api_id", None)
    api_hash = (getattr(settings, "telegram_api_hash", "") or "").strip()
    session_string = (getattr(settings, "telegram_session_string", "") or "").strip()
    if not api_id or not api_hash or not session_string:
        return [], [
            _source_report(
                name="Telegram channels",
                url="https://t.me",
                status="skipped",
                error="Telegram channels skipped: missing TELEGRAM_API_ID/TELEGRAM_API_HASH/TELEGRAM_SESSION_STRING. Пропуск: не хватает настроек.",
            )
        ]
    if not channels:
        return [], [_source_report(name="Telegram channels", url="https://t.me", status="skipped", error="Нет настроенных Telegram-каналов.")]

    lookback_hours = int(getattr(settings, "telegram_source_lookback_hours", 24) or 24)
    max_posts = int(getattr(settings, "telegram_source_max_posts_per_channel", 20) or 20)
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    topics: list[TopicItem] = []
    reports: list[SourceReport] = []

    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        for channel in channels:
            clean_channel = channel.strip().lstrip("@")
            if not clean_channel:
                continue
            source_name = f"Telegram @{clean_channel}"
            try:
                entity = await client.get_entity(clean_channel)
                username = getattr(entity, "username", None) or clean_channel
                channel_topics: list[TopicItem] = []
                async for message in client.iter_messages(entity, limit=max_posts):
                    item = _message_to_topic(message, username, since)
                    if item is not None:
                        channel_topics.append(item)
                topics.extend(channel_topics)
                reports.append(_source_report(name=source_name, url=f"https://t.me/{username}", status="ok" if channel_topics else "empty", item_count=len(channel_topics)))
            except Exception as exc:
                reports.append(_source_report(name=source_name, url=f"https://t.me/{clean_channel}", status="error", error=str(exc)[:160]))
    finally:
        await client.disconnect()

    return topics, reports
