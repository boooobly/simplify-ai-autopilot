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
    _latest_actionable_drafts,
    _moderation_keyboard,
    _parse_callback_data,
    _queue_day_slots,
    _queue_keyboard,
    _render_collect_text,
    _render_queue_day_text,
    _schedule_draft_to_local_slot,
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


def _run_queue_day_selftest() -> None:
    fixed_now = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)  # 11:00 Europe/Moscow
    with _with_fixed_now(fixed_now):
        tmp = TemporaryDirectory()
        db = DraftDatabase(f"{tmp.name}/queue-day.db")
        settings = _settings(["10:00", "14:00", "18:00"], "Europe/Moscow")
        occupied_id = db.create_draft("Первый абзац поста\nвторая строка для превью")
        db.schedule_draft(occupied_id, "2026-05-13 11:00:00")  # 14:00 Moscow

        slots = _queue_day_slots(db, settings, 0)
        assert [slot["slot"] for slot in slots] == ["10:00", "14:00", "18:00"]
        assert slots[0]["status"] == "free"
        assert slots[1]["status"] == "occupied"
        assert slots[1]["draft"]["id"] == occupied_id
        assert slots[1]["preview"] == "Первый абзац поста вторая строка для превью"
        assert slots[2]["status"] == "free"

        text = _render_queue_day_text(db, settings, 0)
        assert "📅 Очередь на сегодня" in text
        assert "Таймзона: Europe/Moscow" in text
        assert "10:00 - свободно" in text
        assert f"14:00 - #{occupied_id} - запланирован" in text
        assert "18:00 - свободно" in text
        assert "Свободных слотов: 2" in text
        assert "Занятых слотов: 1" in text
        tmp.cleanup()


def _run_queue_schedule_specific_slot_selftest() -> None:
    fixed_now = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)  # 11:00 Europe/Moscow
    with _with_fixed_now(fixed_now):
        tmp = TemporaryDirectory()
        db = DraftDatabase(f"{tmp.name}/queue-schedule.db")
        settings = _settings(["10:00", "14:30", "18:00"], "Europe/Moscow")
        draft_id = db.create_draft("schedule me")
        scheduled_text = _schedule_draft_to_local_slot(db, settings, draft_id, 0, "1430")
        stored = db.get_draft(draft_id)
        assert scheduled_text == "13.05 14:30"
        assert stored["status"] == "scheduled"
        assert stored["scheduled_at"] == "2026-05-13 11:30:00"
        datetime.strptime(stored["scheduled_at"], "%Y-%m-%d %H:%M:%S")
        tmp.cleanup()

    with _with_fixed_now(fixed_now):
        tmp = TemporaryDirectory()
        db = DraftDatabase(f"{tmp.name}/queue-past.db")
        settings = _settings(["10:00", "14:00"], "Europe/Moscow")
        draft_id = db.create_draft("past")
        try:
            _schedule_draft_to_local_slot(db, settings, draft_id, 0, "1000")
            raise AssertionError("past slot must be rejected")
        except ValueError as exc:
            assert "прошлом" in str(exc)
        tmp.cleanup()

    with _with_fixed_now(fixed_now):
        tmp = TemporaryDirectory()
        db = DraftDatabase(f"{tmp.name}/queue-occupied.db")
        settings = _settings(["14:00"], "Europe/Moscow")
        booked_id = db.create_draft("booked")
        db.schedule_draft(booked_id, "2026-05-13 11:00:00")
        draft_id = db.create_draft("second")
        try:
            _schedule_draft_to_local_slot(db, settings, draft_id, 0, "1400")
            raise AssertionError("occupied slot must be rejected")
        except ValueError as exc:
            assert "занят" in str(exc)
        tmp.cleanup()

    with _with_fixed_now(fixed_now):
        tmp = TemporaryDirectory()
        db = DraftDatabase(f"{tmp.name}/queue-invalid.db")
        settings = _settings(["14:00"], "Europe/Moscow")
        draft_id = db.create_draft("invalid")
        try:
            _schedule_draft_to_local_slot(db, settings, draft_id, 0, "1500")
            raise AssertionError("invalid slot must be rejected")
        except ValueError as exc:
            assert "нет в настройках" in str(exc)
        tmp.cleanup()


def _run_latest_actionable_drafts_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/actionable.db")
    draft_id = db.create_draft("draft ok")
    approved_id = db.create_draft("approved ok")
    db.update_status(approved_id, "approved")
    scheduled_id = db.create_draft("scheduled no")
    db.schedule_draft(scheduled_id, "2030-01-01 00:00:00")
    scheduled_draft_status_id = db.create_draft("stale scheduled_at no")
    db.schedule_draft(scheduled_draft_status_id, "2030-01-01 01:00:00")
    db.update_status(scheduled_draft_status_id, "draft")
    for status in ["published", "rejected", "failed"]:
        item_id = db.create_draft(f"{status} no")
        db.update_status(item_id, status)

    ids = [int(item["id"]) for item in _latest_actionable_drafts(db, limit=10)]
    assert approved_id in ids
    assert draft_id in ids
    assert scheduled_id not in ids
    assert scheduled_draft_status_id not in ids
    tmp.cleanup()


def _run_queue_keyboard_selftest() -> None:
    fixed_now = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)
    with _with_fixed_now(fixed_now):
        tmp = TemporaryDirectory()
        db = DraftDatabase(f"{tmp.name}/queue-keyboard.db")
        settings = _settings(["14:00", "18:00"], "Europe/Moscow")
        occupied_id = db.create_draft("occupied")
        db.schedule_draft(occupied_id, "2026-05-13 11:00:00")
        db.create_draft("actionable")
        texts = _keyboard_texts(_queue_keyboard(db, settings, 0))
        assert f"👀 Открыть #{occupied_id}" in texts
        assert f"↩️ Снять с очереди #{occupied_id}" in texts
        assert "➕ Поставить черновик 18:00" in texts
        forbidden = ("Shorts", "Reels", "TikTok", "video", "видео")
        assert not any(word in text for text in texts for word in forbidden)
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
    assert "🖼 Прикрепить картинку источника" not in draft_texts
    assert any(button.text == "🔗 Открыть источник" and button.url == "https://example.com/source" for button in draft_buttons)
    manual_media_buttons = [button for button in draft_buttons if button.text == "📎 Прикрепить медиа"]
    assert len(manual_media_buttons) == 1
    manual_media_callback = manual_media_buttons[0].callback_data
    assert manual_media_callback == "attach_media_flow:5"
    manual_media_action, manual_media_draft_id, manual_media_slot = _parse_callback_data(manual_media_callback)
    assert manual_media_draft_id == 5
    assert manual_media_slot is None
    assert manual_media_action == "attach_media_flow"

    source_image_texts = _keyboard_texts(
        _moderation_keyboard(
            5,
            "draft",
            source_url="https://example.com/source",
            source_image_url="https://example.com/preview.jpg",
        )
    )
    assert "🖼 Прикрепить картинку источника" in source_image_texts

    media_texts = _keyboard_texts(
        _moderation_keyboard(
            5,
            "draft",
            has_media=True,
            source_url="https://example.com/source",
            source_image_url="https://example.com/preview.jpg",
        )
    )
    assert "🖼 Прикрепить картинку источника" not in media_texts

    approved_texts = _keyboard_texts(
        _moderation_keyboard(
            5,
            "approved",
            source_url="https://example.com/source",
            source_image_url="https://example.com/preview.jpg",
        )
    )
    assert "📅 В ближайший слот" in approved_texts
    assert "♻️ Перегенерировать" in approved_texts
    assert "🖼 Прикрепить картинку источника" in approved_texts

    for status in ["scheduled", "published", "rejected", "failed"]:
        texts = _keyboard_texts(
            _moderation_keyboard(
                5,
                status,
                source_url="https://example.com/source",
                source_image_url="https://example.com/preview.jpg",
            )
        )
        assert "📅 В ближайший слот" not in texts
        assert "♻️ Перегенерировать" not in texts
        assert "🖼 Прикрепить картинку источника" not in texts

    no_source_texts = _keyboard_texts(_moderation_keyboard(5, "draft"))
    assert "♻️ Перегенерировать" not in no_source_texts


class _FakeCallbackQuery:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(photo=None, video=None, animation=None, document=None, caption=None)
        self.answers: list[tuple[str | None, bool]] = []
        self.edited_text: str | None = None
        self.edited_reply_markup = None

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, reply_markup=None, link_preview_options=None) -> None:
        self.edited_text = text
        self.edited_reply_markup = reply_markup

    async def edit_message_caption(self, caption: str, reply_markup=None) -> None:
        self.edited_text = caption
        self.edited_reply_markup = reply_markup


async def _run_moderation_media_attach_callback_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/moderation-media.db")
    draft_id = db.create_draft("Draft ready for manual media attach")
    keyboard = _moderation_keyboard(draft_id, "draft")
    media_buttons = [button for button in _keyboard_buttons(keyboard) if button.text == "📎 Прикрепить медиа"]
    assert len(media_buttons) == 1
    callback_data = media_buttons[0].callback_data
    action, parsed_draft_id, slot = _parse_callback_data(callback_data)
    assert action == "attach_media_flow"
    assert parsed_draft_id == draft_id
    assert slot is None

    settings = SimpleNamespace(admin_id=123, custom_emoji_aliases={})
    context = SimpleNamespace(bot_data={"settings": settings, "db": db}, user_data={"pending_edit_draft_id": draft_id})
    query = _FakeCallbackQuery(callback_data, settings.admin_id)
    update = SimpleNamespace(callback_query=query)

    await handlers.moderation_callback(update, context)

    assert handlers._get_pending_media(context) == draft_id
    assert "pending_edit_draft_id" not in context.user_data
    assert query.edited_text is not None
    assert f"Прикрепление медиа к черновику #{draft_id}" in query.edited_text
    reply_callbacks = [button.callback_data for button in _keyboard_buttons(query.edited_reply_markup)]
    assert f"attach_media_done:{draft_id}" in reply_callbacks
    assert f"attach_media_cancel:{draft_id}" in reply_callbacks
    tmp.cleanup()


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
    assert "Картинка источника: нет" in moderation
    moderation_with_image = _build_moderation_text(
        draft_id=1,
        content=text,
        source_url="https://example.com",
        source_image_url="https://example.com/preview.jpg",
        custom_emoji_aliases=aliases,
    )
    assert "Картинка источника: есть" in moderation_with_image

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
    _run_queue_day_selftest()
    _run_queue_schedule_specific_slot_selftest()
    _run_latest_actionable_drafts_selftest()
    _run_queue_keyboard_selftest()
    _run_keyboard_selftest()
    asyncio.run(_run_moderation_media_attach_callback_selftest())
    asyncio.run(_run_collect_stats_selftest())

    print("OK")


if __name__ == "__main__":
    run()
