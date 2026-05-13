import asyncio
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from bot.database import DraftDatabase
from bot.sources import TopicItem, _with_scoring
from bot.handlers import (
    _build_media_preview_caption,
    _build_moderation_text,
    _failed_drafts_keyboard,
    _render_failed_drafts_text,
    _send_moderation_preview,
    _collect_topics_with_stats,
    _render_collect_text,
)


async def _run_collect_stats_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topics.db")
    fresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    items = [
        _with_scoring(TopicItem("Fresh AI tool app for video captions", "https://example.com/fresh", "Test", fresh_date, source_group="tools")),
        _with_scoring(TopicItem("Old AI tool app for video captions", "https://example.com/old", "Test", old_date, source_group="tech_media")),
        _with_scoring(TopicItem("AI tool without date for prompts", "https://example.com/no-date", "Test", None, source_group="tech_media")),
    ]
    stats, all_items, inserted = await _collect_topics_with_stats(
        db,
        items=items,
        settings=SimpleNamespace(max_topic_age_days=14, has_ai_provider=False),
    )
    assert stats.total == 3
    assert stats.stale == 1
    assert stats.missing_date == 1
    assert stats.new >= 1
    assert any(item.url == "https://example.com/fresh" for item in inserted)
    assert all_items == items
    summary = _render_collect_text(stats, all_items, inserted)
    tmp.cleanup()
    assert "Старые: 1" in summary
    assert "Без даты: 1" in summary


def run() -> None:
    aliases = {"claude": ("🤖", "5208880957280522189")}
    text = "Тест [[EMOJI:claude]]"

    moderation = _build_moderation_text(
        draft_id=1,
        content=text,
        source_url="https://example.com",
        custom_emoji_aliases=aliases,
    )
    assert "🤖" in moderation

    caption = _build_media_preview_caption(
        draft_id=2,
        content=text,
        source_url="https://example.com",
        media_type="photo",
        custom_emoji_aliases=aliases,
    )
    assert "🤖" in caption
    assert "settings" not in _send_moderation_preview.__code__.co_names

    failed_drafts = [
        {
            "id": 42,
            "source_url": "https://example.com/news",
            "media_url": "abc",
            "media_type": "photo",
            "updated_at": "2026-05-05 10:00:00",
            "content": "Тестовый упавший черновик\nсо второй строкой",
        },
        {
            "id": 43,
            "source_url": "https://example.com/other",
            "media_url": "",
            "media_type": "",
            "updated_at": "2026-05-05 11:00:00",
            "content": "Второй упавший черновик",
        },
    ]
    failed_text = _render_failed_drafts_text(failed_drafts)
    assert "#42" in failed_text
    assert "#43" in failed_text
    assert "Можно восстановить: /restore_draft ID" in failed_text

    keyboard = _failed_drafts_keyboard(failed_drafts)
    assert len(keyboard.inline_keyboard) == 2
    first_row = keyboard.inline_keyboard[0]
    assert first_row[0].text == "Открыть #42"
    assert first_row[0].callback_data == "draft_info:42"
    assert first_row[1].text == "🔁 Восстановить #42"
    assert first_row[1].callback_data == "restore_draft:42"
    second_row = keyboard.inline_keyboard[1]
    assert second_row[0].text == "Открыть #43"
    assert second_row[0].callback_data == "draft_info:43"
    assert second_row[1].text == "🔁 Восстановить #43"
    assert second_row[1].callback_data == "restore_draft:43"

    asyncio.run(_run_collect_stats_selftest())

    print("OK")


if __name__ == "__main__":
    run()
