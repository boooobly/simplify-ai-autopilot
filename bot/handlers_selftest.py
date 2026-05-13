import asyncio
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from bot.database import DraftDatabase
from bot.sources import TopicItem, _with_scoring
import bot.handlers as handlers
from bot.handlers import (
    _build_media_preview_caption,
    _build_moderation_text,
    _failed_drafts_keyboard,
    _render_failed_drafts_text,
    _send_moderation_preview,
    _collect_topics_with_stats,
    _find_nearest_available_slot,
    _moderation_keyboard,
    _render_collect_text,
    _schedule_draft_to_nearest_slot,
    _topic_actions_keyboard,
)


class _FixedDateTime(datetime):
    fixed_now: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        value = cls.fixed_now or datetime.now(timezone.utc)
        if tz is None:
            return value.replace(tzinfo=None)
        return value.astimezone(tz)


def _keyboard_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _keyboard_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _settings(slots: list[str], timezone_name: str = "UTC") -> SimpleNamespace:
    return SimpleNamespace(daily_post_slots=slots, schedule_timezone=timezone_name)


def _with_fixed_now(now: datetime):
    class _Patch:
        def __enter__(self):
            self.original = handlers.datetime
            _FixedDateTime.fixed_now = now
            handlers.datetime = _FixedDateTime

        def __exit__(self, exc_type, exc, tb):
            handlers.datetime = self.original
            _FixedDateTime.fixed_now = None
    return _Patch()


def _run_nearest_slot_selftest() -> None:
    fixed_now = datetime(2026, 5, 13, 10, 30, tzinfo=timezone.utc)
    with _with_fixed_now(fixed_now):
        tmp = TemporaryDirectory()
        db = DraftDatabase(f"{tmp.name}/slots.db")
        settings = _settings(["09:00", "11:00", "18:00"])
        later_today = _find_nearest_available_slot(db, settings)
        assert later_today.strftime("%Y-%m-%d %H:%M") == "2026-05-13 11:00"
        draft_id = db.create_draft("slot test")
        scheduled_text = _schedule_draft_to_nearest_slot(db, settings, draft_id)
        stored = db.get_draft(draft_id)
        assert scheduled_text == "13.05 11:00"
        assert stored["status"] == "scheduled"
        assert stored["scheduled_at"] == "2026-05-13 11:00:00"
        datetime.strptime(stored["scheduled_at"], "%Y-%m-%d %H:%M:%S")
        tmp.cleanup()

    with _with_fixed_now(fixed_now):
        tmp = TemporaryDirectory()
        db = DraftDatabase(f"{tmp.name}/full-today.db")
        settings = _settings(["09:00", "11:00"])
        booked_id = db.create_draft("booked")
        db.schedule_draft(booked_id, "2026-05-13 11:00:00")
        tomorrow = _find_nearest_available_slot(db, settings)
        assert tomorrow.strftime("%Y-%m-%d %H:%M") == "2026-05-14 09:00"
        tmp.cleanup()

    with _with_fixed_now(fixed_now):
        tmp = TemporaryDirectory()
        db = DraftDatabase(f"{tmp.name}/double-book.db")
        settings = _settings(["11:00", "18:00"])
        booked_id = db.create_draft("booked")
        db.schedule_draft(booked_id, "2026-05-13 11:00:00")
        next_free = _find_nearest_available_slot(db, settings)
        assert next_free.strftime("%Y-%m-%d %H:%M") == "2026-05-13 18:00"
        tmp.cleanup()


def _run_keyboard_selftest() -> None:
    topic_keyboard = _topic_actions_keyboard(7, "https://example.com/topic")
    topic_buttons = _keyboard_buttons(topic_keyboard)
    assert any(button.text == "🔗 Открыть источник" and button.url == "https://example.com/topic" for button in topic_buttons)
    assert "✍️ Создать черновик" in _keyboard_texts(topic_keyboard)
    assert "❌ Отклонить тему" in _keyboard_texts(topic_keyboard)
    assert not any("Shorts" in text or "Reels" in text or "TikTok" in text or "Видео" in text for text in _keyboard_texts(topic_keyboard))

    draft_keyboard = _moderation_keyboard(5, "draft", source_url="https://example.com/source")
    draft_buttons = _keyboard_buttons(draft_keyboard)
    draft_texts = _keyboard_texts(draft_keyboard)
    assert "📅 В ближайший слот" in draft_texts
    assert "♻️ Перегенерировать" in draft_texts
    assert any(button.text == "🔗 Открыть источник" and button.url == "https://example.com/source" for button in draft_buttons)

    approved_texts = _keyboard_texts(_moderation_keyboard(5, "approved", source_url="https://example.com/source"))
    assert "📅 В ближайший слот" in approved_texts
    assert "♻️ Перегенерировать" in approved_texts

    for status in ["scheduled", "published", "rejected", "failed"]:
        texts = _keyboard_texts(_moderation_keyboard(5, status, source_url="https://example.com/source"))
        assert "📅 В ближайший слот" not in texts
        assert "♻️ Перегенерировать" not in texts

    no_source_texts = _keyboard_texts(_moderation_keyboard(5, "draft"))
    assert "♻️ Перегенерировать" not in no_source_texts


async def _run_collect_stats_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topics.db")
    fresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    items = [
        _with_scoring(TopicItem("Fresh AI tool app for video captions", "https://example.com/fresh", "Test", fresh_date, source_group="tools")),
        _with_scoring(TopicItem("Old AI tool app for video captions", "https://example.com/old", "Test", old_date, source_group="tech_media")),
        _with_scoring(TopicItem("AI tool without date for prompts", "https://example.com/no-date", "Test", None, source_group="tech_media")),
        _with_scoring(TopicItem("OpenAI launches GPT-5.1 for ChatGPT", "https://example.com/model-a", "OpenAI blog", fresh_date, source_group="official_ai")),
        _with_scoring(TopicItem("OpenAI unveils GPT-5.1 with ChatGPT update", "https://example.com/model-b", "The Verge AI", fresh_date, source_group="tech_media")),
    ]
    stats, all_items, inserted = await _collect_topics_with_stats(
        db,
        items=items,
        settings=SimpleNamespace(max_topic_age_days=14, has_ai_provider=False),
    )
    assert stats.total == 5
    assert stats.stale == 1
    assert stats.missing_date == 1
    assert stats.new >= 1
    assert stats.merged_story == 1
    assert any(item.url == "https://example.com/fresh" for item in inserted)
    assert all_items == items
    summary = _render_collect_text(stats, all_items, inserted)
    tmp.cleanup()
    assert "Старые: 1" in summary
    assert "Без даты: 1" in summary
    assert "Объединено с похожими: 1" in summary


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

    _run_nearest_slot_selftest()
    _run_keyboard_selftest()
    asyncio.run(_run_collect_stats_selftest())

    print("OK")


if __name__ == "__main__":
    run()
