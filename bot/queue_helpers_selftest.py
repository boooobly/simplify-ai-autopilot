from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from bot.database import DraftDatabase
import bot.queue_helpers as queue_helpers
from bot.queue_helpers import (
    _find_nearest_available_slot,
    _latest_actionable_drafts,
    _queue_day_slots,
    _queue_keyboard,
    _render_queue_day_text,
    _schedule_draft_to_local_slot,
    _schedule_draft_to_nearest_slot,
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
            self.original = queue_helpers.datetime
            _FixedDateTime.fixed_now = now
            queue_helpers.datetime = _FixedDateTime

        def __exit__(self, exc_type, exc, tb):
            queue_helpers.datetime = self.original
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
        keyboard = _queue_keyboard(db, settings, 0)
        texts = _keyboard_texts(keyboard)
        callbacks = [button.callback_data or "" for button in _keyboard_buttons(keyboard)]
        assert f"👀 Открыть #{occupied_id}" in texts
        assert f"↩️ Снять с очереди #{occupied_id}" in texts
        assert "➕ Поставить черновик 18:00" in texts
        assert f"unschedule:{occupied_id}" in callbacks
        assert "queue_pick_slot:0:1800" in callbacks
        forbidden = ("Shorts", "Reels", "TikTok", "video", "видео")
        assert not any(word in text for text in texts for word in forbidden)
        assert not any(word in callback for callback in callbacks for word in forbidden)
        tmp.cleanup()


def run() -> None:
    _run_nearest_slot_selftest()
    _run_queue_day_selftest()
    _run_queue_schedule_specific_slot_selftest()
    _run_latest_actionable_drafts_selftest()
    _run_queue_keyboard_selftest()
    print("OK")


if __name__ == "__main__":
    run()
