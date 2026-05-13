"""Queue and schedule helper functions for Telegram handlers."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.database import DraftDatabase

ACTIONABLE_DRAFT_STATUSES = {"draft", "approved"}


def _status_guard_message(action: str, status: str | None) -> str:
    if status == "published":
        if action == "schedule":
            return "Опубликованный черновик уже нельзя планировать."
        if action == "publish":
            return "Этот черновик уже опубликован."
        if action == "edit":
            return "Опубликованный черновик нельзя редактировать."
    if status == "rejected":
        if action == "schedule":
            return "Отклонённый черновик нельзя планировать."
        if action == "publish":
            return "Этот черновик отклонён. Сначала создай новый или восстанови его позже."
        if action == "edit":
            return "Отклонённый черновик нельзя редактировать."
    if status == "publishing":
        return "Черновик сейчас публикуется. Подожди немного."
    if status == "failed":
        return "Черновик в статусе failed. Сначала верни его в черновики."
    if status == "scheduled" and action == "edit":
        return "Запланированный черновик уже в очереди. Сначала сними его с очереди позже."
    if status == "scheduled" and action == "schedule":
        return "Черновик уже запланирован."
    return f"Это действие недоступно для текущего статуса: {status or 'unknown'}."


def _get_day_range(day_offset: int, timezone_str: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_str)
    now_local = datetime.now(tz)
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
    return start, start + timedelta(days=1)


def _queue_draft_ids_for_day(db: DraftDatabase, settings, day_offset: int) -> list[int]:
    return [int(slot["draft"]["id"]) for slot in _queue_day_slots(db, settings, day_offset) if slot.get("draft")]


def _normalize_slot_hhmm(slot_hhmm: str) -> str:
    raw = str(slot_hhmm or "").strip()
    if len(raw) == 4 and raw.isdigit():
        return f"{raw[:2]}:{raw[2:]}"
    return raw


def _slot_callback_hhmm(slot: str) -> str:
    return _normalize_slot_hhmm(slot).replace(":", "")


def _short_post_preview(content: str | None, limit: int = 90) -> str:
    preview = " ".join(str(content or "").strip().split())
    if not preview:
        return "[пусто]"
    if len(preview) <= limit:
        return preview
    return preview[: max(0, limit - 1)].rstrip() + "…"


def _queue_day_slots(db: DraftDatabase, settings, day_offset: int) -> list[dict]:
    tz = ZoneInfo(settings.schedule_timezone)
    start_local, end_local = _get_day_range(day_offset, settings.schedule_timezone)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    drafts = db.list_scheduled_drafts_between(start_utc, end_utc)
    by_slot: dict[str, dict] = {}
    extras_by_slot: dict[str, list[dict]] = {}
    for draft in drafts:
        scheduled_raw = str(draft.get("scheduled_at") or "")
        if not scheduled_raw:
            continue
        scheduled_utc = datetime.strptime(scheduled_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        hhmm = scheduled_utc.astimezone(tz).strftime("%H:%M")
        if hhmm in by_slot:
            extras_by_slot.setdefault(hhmm, []).append(draft)
        else:
            by_slot[hhmm] = draft
    result: list[dict] = []
    for slot in settings.daily_post_slots:
        normalized = _normalize_slot_hhmm(slot)
        draft = by_slot.get(normalized)
        result.append(
            {
                "slot": normalized,
                "status": "occupied" if draft else "free",
                "draft": draft,
                "preview": _short_post_preview(draft.get("content") if draft else None),
                "extra_drafts": extras_by_slot.get(normalized, []),
            }
        )
    return result


def _empty_slots_for_day(db: DraftDatabase, settings, day_offset: int) -> list[str]:
    return [str(slot["slot"]) for slot in _queue_day_slots(db, settings, day_offset) if slot.get("status") == "free"]


def _parse_slot_hhmm(slot: str) -> tuple[int, int]:
    normalized = _normalize_slot_hhmm(slot)
    hour, minute = map(int, normalized.split(":", 1))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Некорректный слот времени: {slot}")
    return hour, minute


def _busy_slots_for_local_day(db: DraftDatabase, settings, day_start_local: datetime) -> set[str]:
    tz = ZoneInfo(settings.schedule_timezone)
    start_local = day_start_local.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    busy: set[str] = set()
    for draft in db.list_scheduled_drafts_between(start_utc, end_utc):
        scheduled_raw = str(draft.get("scheduled_at") or "")
        if not scheduled_raw:
            continue
        scheduled_utc = datetime.strptime(scheduled_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        busy.add(scheduled_utc.astimezone(tz).strftime("%H:%M"))
    return busy


def _find_nearest_available_slot(db: DraftDatabase, settings) -> datetime:
    tz = ZoneInfo(settings.schedule_timezone)
    now_local = datetime.now(tz)
    slots = list(settings.daily_post_slots)
    for day_offset in range(8):
        day_start = (now_local + timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        busy_slots = _busy_slots_for_local_day(db, settings, day_start)
        for slot in slots:
            hour, minute = _parse_slot_hhmm(slot)
            normalized_slot = _normalize_slot_hhmm(slot)
            candidate = day_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now_local:
                continue
            if normalized_slot in busy_slots:
                continue
            return candidate
    raise ValueError("Нет свободных слотов на ближайшие 7 дней.")


def _schedule_draft_to_nearest_slot(db: DraftDatabase, settings, draft_id: int) -> str:
    scheduled_local = _find_nearest_available_slot(db, settings)
    scheduled_utc = scheduled_local.astimezone(ZoneInfo("UTC"))
    db.schedule_draft(draft_id, scheduled_utc.strftime("%Y-%m-%d %H:%M:%S"))
    return scheduled_local.strftime("%d.%m %H:%M")


def _latest_actionable_drafts(db: DraftDatabase, limit: int = 10) -> list[dict]:
    rows = db.list_drafts(limit=max(limit * 5, 50))
    result: list[dict] = []
    for draft in rows:
        if str(draft.get("status") or "") not in ACTIONABLE_DRAFT_STATUSES:
            continue
        if draft.get("scheduled_at"):
            continue
        result.append(draft)
        if len(result) >= limit:
            break
    return result


def _render_queue_day_text(db: DraftDatabase, settings, day_offset: int) -> str:
    day_name = "сегодня" if day_offset == 0 else "завтра" if day_offset == 1 else f"через {day_offset} дн."
    slots = _queue_day_slots(db, settings, day_offset)
    free_count = sum(1 for slot in slots if slot.get("status") == "free")
    occupied_count = len(slots) - free_count
    lines = [f"📅 Очередь на {day_name}", f"Таймзона: {settings.schedule_timezone}", ""]
    for slot in slots:
        if slot.get("draft"):
            draft = slot["draft"]
            lines.extend([f"{slot['slot']} - #{draft['id']} - запланирован", str(slot.get("preview") or "[пусто]"), ""])
        else:
            lines.append(f"{slot['slot']} - свободно")
    if lines and lines[-1] != "":
        lines.append("")
    lines.extend([f"Свободных слотов: {free_count}", f"Занятых слотов: {occupied_count}"])
    return "\n".join(lines).rstrip()


def _render_queue_text(db: DraftDatabase, settings, day_offset: int) -> str:
    return _render_queue_day_text(db, settings, day_offset)


def _is_local_slot_free(db: DraftDatabase, settings, day_offset: int, slot_hhmm: str) -> bool:
    normalized = _normalize_slot_hhmm(slot_hhmm)
    return any(slot.get("slot") == normalized and slot.get("status") == "free" for slot in _queue_day_slots(db, settings, day_offset))


def _schedule_draft_to_local_slot(db: DraftDatabase, settings, draft_id: int, day_offset: int, slot_hhmm: str) -> str:
    normalized_slot = _normalize_slot_hhmm(slot_hhmm)
    configured_slots = [_normalize_slot_hhmm(slot) for slot in settings.daily_post_slots]
    if normalized_slot not in configured_slots:
        raise ValueError("Такого слота нет в настройках расписания.")
    draft = db.get_draft(draft_id)
    if not draft:
        raise ValueError(f"Черновик #{draft_id} не найден.")
    status = str(draft.get("status") or "")
    if status not in ACTIONABLE_DRAFT_STATUSES:
        raise ValueError(_status_guard_message("schedule", status))
    if draft.get("scheduled_at"):
        raise ValueError(f"Черновик #{draft_id} уже запланирован.")
    tz = ZoneInfo(settings.schedule_timezone)
    now_local = datetime.now(tz)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
    hour, minute = _parse_slot_hhmm(normalized_slot)
    scheduled_local = day_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if scheduled_local <= now_local:
        raise ValueError("Этот слот уже в прошлом. Выбери другой слот.")
    if not _is_local_slot_free(db, settings, day_offset, normalized_slot):
        raise ValueError("Этот слот уже занят. Обнови очередь и выбери свободный слот.")
    scheduled_utc = scheduled_local.astimezone(ZoneInfo("UTC"))
    db.schedule_draft(draft_id, scheduled_utc.strftime("%Y-%m-%d %H:%M:%S"))
    return scheduled_local.strftime("%d.%m %H:%M")


def _queue_keyboard(db: DraftDatabase, settings, day_offset: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    has_actionable = bool(_latest_actionable_drafts(db, limit=1))
    for slot in _queue_day_slots(db, settings, day_offset):
        draft = slot.get("draft")
        if draft:
            draft_id = int(draft["id"])
            rows.append([
                InlineKeyboardButton(f"👀 Открыть #{draft_id}", callback_data=f"draft_info:{draft_id}"),
                InlineKeyboardButton(f"↩️ Снять с очереди #{draft_id}", callback_data=f"unschedule:{draft_id}"),
            ])
        elif has_actionable:
            callback_slot = _slot_callback_hhmm(str(slot["slot"]))
            rows.append([InlineKeyboardButton(f"➕ Поставить черновик {slot['slot']}", callback_data=f"queue_pick_slot:{day_offset}:{callback_slot}")])
    if day_offset == 0:
        rows.extend([
            [InlineKeyboardButton("🔄 Обновить", callback_data="queue_today:0")],
            [InlineKeyboardButton("📅 Завтра", callback_data="queue_tomorrow:0")],
        ])
    else:
        rows.extend([
            [InlineKeyboardButton("🔄 Обновить", callback_data="queue_tomorrow:0")],
            [InlineKeyboardButton("📅 Сегодня", callback_data="queue_today:0")],
        ])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")])
    return InlineKeyboardMarkup(rows)


def _queue_draft_pick_keyboard(db: DraftDatabase, day_offset: int, slot_hhmm: str, limit: int = 10) -> InlineKeyboardMarkup:
    normalized_slot = _normalize_slot_hhmm(slot_hhmm)
    callback_slot = _slot_callback_hhmm(normalized_slot)
    rows: list[list[InlineKeyboardButton]] = []
    for draft in _latest_actionable_drafts(db, limit=limit):
        draft_id = int(draft["id"])
        rows.append([InlineKeyboardButton(f"#{draft_id} — {_short_post_preview(draft.get('content'), 45)}", callback_data=f"queue_schedule_draft:{draft_id}:{day_offset}:{callback_slot}")])
    rows.append([InlineKeyboardButton("⬅️ Назад к очереди", callback_data="queue_today:0" if day_offset == 0 else "queue_tomorrow:0")])
    return InlineKeyboardMarkup(rows)
