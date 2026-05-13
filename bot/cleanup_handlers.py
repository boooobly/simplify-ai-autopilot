"""Cleanup admin command and callback handlers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.database import DraftDatabase

CLEANUP_CONFIRM_TTL_SECONDS = 10 * 60
CLEANUP_PREVIEW_GENERATED_AT_KEY = "cleanup_preview_generated_at"
CLEANUP_PREVIEW_COUNTS_KEY = "cleanup_preview_counts"


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _cleanup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧹 Очистить", callback_data="cleanup_confirm:0")],
            [InlineKeyboardButton("Отмена", callback_data="cleanup_cancel:0")],
        ]
    )


def _cleanup_total(counts: dict[str, int]) -> int:
    return int(counts.get("total") or sum(int(value) for key, value in counts.items() if key != "total"))


def _render_cleanup_preview_text(counts: dict[str, int]) -> str:
    return "\n".join(
        [
            "🧹 Предпросмотр очистки базы",
            "",
            f"Старые отклонённые темы: {_fmt_int(int(counts.get('old_rejected_topics', 0)))}",
            f"Старые использованные темы: {_fmt_int(int(counts.get('old_used_topics', 0)))}",
            f"Старые новые темы: {_fmt_int(int(counts.get('old_new_topics', 0)))}",
            f"Старые отклонённые черновики: {_fmt_int(int(counts.get('old_rejected_drafts', 0)))}",
            f"Старые failed-черновики: {_fmt_int(int(counts.get('old_failed_drafts', 0)))}",
            f"Залежавшиеся нетронутые draft-черновики: {_fmt_int(int(counts.get('stale_draft_drafts', 0)))}",
            "",
            f"Всего строк к удалению: {_fmt_int(_cleanup_total(counts))}",
            "",
            "Защита: scheduled/publishing/published/approved, черновики с медиа и ai_usage не удаляются.",
            "Для применения нажми кнопку или отправь /cleanup_confirm в течение 10 минут.",
        ]
    )


def _render_cleanup_applied_text(counts: dict[str, int]) -> str:
    return _render_cleanup_preview_text(counts).replace(
        "🧹 Предпросмотр очистки базы", "✅ Очистка базы выполнена"
    ).replace(
        "Всего строк к удалению:", "Всего строк удалено:"
    ).replace(
        "Для применения нажми кнопку или отправь /cleanup_confirm в течение 10 минут.",
        "Автоматическая очистка не включалась.",
    )


def _store_cleanup_preview(context: ContextTypes.DEFAULT_TYPE, counts: dict[str, int]) -> None:
    context.user_data[CLEANUP_PREVIEW_GENERATED_AT_KEY] = datetime.now(timezone.utc).timestamp()
    context.user_data[CLEANUP_PREVIEW_COUNTS_KEY] = dict(counts)


def _clear_cleanup_preview(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(CLEANUP_PREVIEW_GENERATED_AT_KEY, None)
    context.user_data.pop(CLEANUP_PREVIEW_COUNTS_KEY, None)


def _cleanup_preview_is_fresh(context: ContextTypes.DEFAULT_TYPE) -> bool:
    generated_at = context.user_data.get(CLEANUP_PREVIEW_GENERATED_AT_KEY)
    counts = context.user_data.get(CLEANUP_PREVIEW_COUNTS_KEY)
    if not isinstance(counts, dict):
        return False
    try:
        generated_ts = float(generated_at)
    except (TypeError, ValueError):
        return False
    age = datetime.now(timezone.utc).timestamp() - generated_ts
    return 0 <= age <= CLEANUP_CONFIRM_TTL_SECONDS


def _is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


def _admin_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🗓 План"), KeyboardButton("🧩 Черновики из плана")],
            [KeyboardButton("📅 Очередь"), KeyboardButton("📝 Черновики")],
            [KeyboardButton("🧠 Темы"), KeyboardButton("📡 Источники")],
            [KeyboardButton("📊 Расходы"), KeyboardButton("✍️ Стиль")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("❓ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие или пришли ссылку",
    )


async def cleanup_preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    db: DraftDatabase = context.bot_data["db"]
    counts = db.cleanup_preview()
    _store_cleanup_preview(context, counts)
    if update.message:
        await update.message.reply_text(_render_cleanup_preview_text(counts), reply_markup=_cleanup_keyboard())


async def cleanup_confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    if not _is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    if not _cleanup_preview_is_fresh(context):
        _clear_cleanup_preview(context)
        if update.message:
            await update.message.reply_text("Сначала запусти /cleanup_preview. Предпросмотр действует 10 минут.")
        return
    db: DraftDatabase = context.bot_data["db"]
    counts = db.cleanup_apply()
    _clear_cleanup_preview(context)
    if update.message:
        await update.message.reply_text(_render_cleanup_applied_text(counts), reply_markup=_admin_reply_keyboard())


async def handle_cleanup_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    db: DraftDatabase,
    settings,
    edit_message_func: Callable[..., Awaitable[None]],
    back_keyboard_func: Callable[[], InlineKeyboardMarkup],
) -> bool:
    _ = settings
    data = query.data or ""
    action = data.split(":", 1)[0]
    if action == "cleanup_cancel":
        _clear_cleanup_preview(context)
        await edit_message_func(query, "Очистка отменена.", reply_markup=back_keyboard_func())
        return True

    if action == "cleanup_confirm":
        if not _cleanup_preview_is_fresh(context):
            _clear_cleanup_preview(context)
            await edit_message_func(query, "Сначала запусти /cleanup_preview. Предпросмотр действует 10 минут.")
            return True
        counts = db.cleanup_apply()
        _clear_cleanup_preview(context)
        await edit_message_func(query, _render_cleanup_applied_text(counts), reply_markup=back_keyboard_func())
        return True

    return False
