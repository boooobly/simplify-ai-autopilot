"""Moderation callback handling for draft cards."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.database import DraftDatabase
from bot.media_utils import media_count
from bot.queue_helpers import ACTIONABLE_DRAFT_STATUSES
from bot.telegram_formatting import strip_quote_markers
from bot.writer import EmptyAIResponseError

logger = logging.getLogger(__name__)

REWRITE_DRAFT_ACTIONS = {
    "rewrite_remove_fluff": {
        "mode": "remove_fluff",
        "operation": "rewrite_remove_fluff",
        "progress": "🧹 Убираю воду из черновика #{draft_id}...",
    },
    "rewrite_shorten": {
        "mode": "shorten",
        "operation": "rewrite_shorten",
        "progress": "📉 Сокращаю черновик #{draft_id}...",
    },
    "rewrite_neutralize_ads": {
        "mode": "neutralize_ads",
        "operation": "rewrite_neutralize_ads",
        "progress": "😐 Убираю рекламный тон из черновика #{draft_id}...",
    },
}

DRAFT_MODERATION_ACTIONS = {
    "publish",
    "schedule",
    "schedule_slot",
    "schedule_nearest",
    "regenerate",
    "unschedule",
    "preview",
    "preview_back",
    "reject",
    "restore_draft",
    "rewrite",
    "edit_text",
    "edit_cancel",
    "attach_source_image",
    "attach_media_flow",
    "attach_media_done",
    "attach_media_cancel",
    "remove_media",
    "polish",
    "draft_info",
    *REWRITE_DRAFT_ACTIONS.keys(),
}


def is_draft_moderation_action(action: str) -> bool:
    """Return True when callback action belongs to an existing draft moderation card."""

    return action in DRAFT_MODERATION_ACTIONS


def _rewrite_action_config(action: str) -> dict[str, str]:
    try:
        return REWRITE_DRAFT_ACTIONS[action]
    except KeyError as exc:
        raise ValueError(f"Unsupported rewrite action: {action}") from exc


@dataclass(frozen=True)
class ModerationCallbackDeps:
    edit_callback_message: Callable[..., Awaitable[None]]
    publish_to_channel: Callable[..., Awaitable[Any]]
    schedule_keyboard: Callable[[int, list[str]], InlineKeyboardMarkup]
    queue_keyboard: Callable[[DraftDatabase, Any, int], InlineKeyboardMarkup]
    schedule_draft_to_nearest_slot: Callable[[DraftDatabase, Any, int], str]
    can_publish: Callable[[str | None], bool]
    can_schedule: Callable[[str | None], bool]
    can_edit: Callable[[str | None], bool]
    status_guard_message: Callable[[str, str | None], str]
    regenerate_draft_from_source: Callable[..., Awaitable[tuple[str | None, str | None]]]
    build_moderation_text: Callable[..., str]
    moderation_keyboard: Callable[..., InlineKeyboardMarkup]
    moderation_keyboard_for_draft: Callable[[int, dict[str, object]], InlineKeyboardMarkup]
    preview_keyboard: Callable[[int], InlineKeyboardMarkup]
    full_draft_text: Callable[[dict[str, object]], str]
    clear_pending_edit: Callable[[Any], None]
    set_pending_edit: Callable[[Any, int], None]
    clear_pending_media: Callable[[Any], None]
    get_pending_media: Callable[[Any], int | None]
    set_pending_media: Callable[[Any, int], None]
    send_moderation_preview: Callable[..., Awaitable[None]]
    resolve_ai_provider: Callable[[Any], tuple[str, str, str | None, dict[str, str] | None]]
    run_rewrite_post_draft: Callable[..., Awaitable[Any]]
    run_polish_post_draft: Callable[..., Awaitable[Any]]
    rewrite_test_draft: Callable[[str], str]
    encode_media_group: Callable[[list[dict]], str]
    estimate_ai_cost: Callable[[str, int, int, Any], float]
    empty_ai_reply_text: str


async def handle_draft_moderation_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    draft_id: int,
    slot: str | None,
    deps: ModerationCallbackDeps,
) -> bool:
    """Handle moderation callback actions that operate on a draft card."""

    if not is_draft_moderation_action(action):
        return False

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    query = update.callback_query
    if not query:
        return True

    draft = db.get_draft(draft_id)
    if not draft and action not in {"edit_cancel", "attach_media_cancel"}:
        await deps.edit_callback_message(query, f"Черновик #{draft_id} не найден.")
        return True

    if action == "publish":
        original_status = str(draft.get("status") or "")
        if not deps.can_publish(original_status):
            await deps.edit_callback_message(query, deps.status_guard_message("publish", original_status))
            return True
        if not db.mark_draft_publishing(
            draft_id,
            allowed_statuses=("draft", "approved", "scheduled"),
        ):
            await deps.edit_callback_message(query, "Черновик уже публикуется или его статус изменился.")
            return True
        draft_to_publish = db.get_draft(draft_id)
        if not draft_to_publish:
            db.mark_draft_failed(draft_id, error="DraftMissingAfterPublishClaim")
            await deps.edit_callback_message(query, f"Черновик #{draft_id} не найден после начала публикации.")
            return True
        try:
            publish_result = await deps.publish_to_channel(
                context.bot,
                settings.channel_id,
                draft_to_publish["content"],
                draft_to_publish.get("media_url"),
                draft_to_publish.get("media_type"),
                settings.custom_emoji_map,
                settings.custom_emoji_aliases,
            )
        except Exception as exc:
            db.mark_draft_failed(draft_id, error=type(exc).__name__)
            raise
        db.mark_draft_published(draft_id, channel_id=settings.channel_id, message_ids=publish_result.message_ids)
        await deps.edit_callback_message(query, f"✅ Черновик #{draft_id} опубликован в канал.")

    elif action == "schedule":
        if not deps.can_schedule(draft.get("status")):
            await deps.edit_callback_message(query, deps.status_guard_message("schedule", draft.get("status")))
            return True
        db.update_status(draft_id, "approved")
        schedule_text = f"Выбери слот публикации для черновика #{draft_id} (часовой пояс: {settings.schedule_timezone}):"
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=schedule_text,
            reply_markup=deps.schedule_keyboard(draft_id, settings.daily_post_slots),
        )
        try:
            await query.answer("Меню слотов отправлено отдельным сообщением.")
        except Exception as answer_exc:
            logger.warning("Failed to answer schedule callback for draft #%s: %s", draft_id, answer_exc)

    elif action == "schedule_slot":
        if not slot:
            await deps.edit_callback_message(query, "Некорректный слот времени.")
            return True
        if slot not in settings.daily_post_slots:
            await deps.edit_callback_message(query, "Такого слота нет в настройках расписания.")
            return True
        draft_for_slot = db.get_draft(draft_id)
        if not draft_for_slot:
            await deps.edit_callback_message(query, f"Черновик #{draft_id} не найден.")
            return True
        slot_status = str(draft_for_slot.get("status") or "")
        if slot_status not in {"draft", "approved", "scheduled"}:
            await deps.edit_callback_message(query, deps.status_guard_message("schedule", slot_status))
            return True
        tz = ZoneInfo(settings.schedule_timezone)
        now_local = datetime.now(tz)
        try:
            hour, minute = map(int, slot.split(":"))
        except (TypeError, ValueError):
            await deps.edit_callback_message(query, "Некорректный слот времени.")
            return True
        if hour not in range(24) or minute not in range(60):
            await deps.edit_callback_message(query, "Некорректный слот времени.")
            return True
        scheduled_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled_local <= now_local:
            scheduled_local += timedelta(days=1)

        scheduled_utc = scheduled_local.astimezone(ZoneInfo("UTC"))
        if not db.schedule_draft(draft_id, scheduled_utc.strftime("%Y-%m-%d %H:%M:%S")):
            await deps.edit_callback_message(query, "Этот слот уже занят или статус черновика изменился.")
            return True
        await deps.edit_callback_message(
            query,
            f"🗓️ Черновик #{draft_id} запланирован на {scheduled_local.strftime('%Y-%m-%d %H:%M')}.\nОчередь: /queue_today",
        )

    elif action == "schedule_nearest":
        if not deps.can_schedule(draft.get("status")):
            await deps.edit_callback_message(query, deps.status_guard_message("schedule", draft.get("status")))
            return True
        try:
            scheduled_text = deps.schedule_draft_to_nearest_slot(db, settings, draft_id)
        except ValueError as exc:
            await deps.edit_callback_message(query, str(exc))
            return True
        await deps.edit_callback_message(
            query,
            f"Черновик #{draft_id} поставлен в ближайший слот: {scheduled_text}",
            reply_markup=deps.queue_keyboard(db, settings, 0),
        )

    elif action == "regenerate":
        status = str(draft.get("status") or "")
        if not deps.can_edit(status):
            await deps.edit_callback_message(query, deps.status_guard_message("edit", status))
            return True
        if not str(draft.get("source_url") or "").strip():
            await deps.edit_callback_message(query, "У этого черновика нет source_url, перегенерация недоступна.")
            return True
        await deps.edit_callback_message(query, f"♻️ Перегенерирую черновик #{draft_id} из того же источника...")
        regenerated, error = await deps.regenerate_draft_from_source(db=db, settings=settings, draft=draft)
        if error or regenerated is None:
            await deps.edit_callback_message(query, error or "Не удалось перегенерировать черновик.")
            return True
        refreshed = db.get_draft(draft_id) or draft
        await deps.edit_callback_message(
            query,
            deps.build_moderation_text(
                draft_id,
                regenerated,
                refreshed.get("source_url"),
                refreshed.get("media_type"),
                refreshed.get("media_url"),
                source_image_url=refreshed.get("source_image_url"),
                custom_emoji_aliases=settings.custom_emoji_aliases,
            ),
            reply_markup=deps.moderation_keyboard(
                draft_id,
                "draft",
                has_media=media_count(refreshed.get("media_url"), refreshed.get("media_type")) > 0,
                source_url=refreshed.get("source_url"),
                source_image_url=refreshed.get("source_image_url"),
            ),
        )

    elif action == "unschedule":
        if not draft:
            await deps.edit_callback_message(query, f"Черновик #{draft_id} не найден.")
            return True
        if draft.get("status") != "scheduled":
            await deps.edit_callback_message(query, f"Черновик #{draft_id} сейчас не в очереди.")
            return True
        db.unschedule_draft(draft_id)
        await deps.edit_callback_message(
            query,
            "Черновик #{0} снят с очереди.".format(draft_id),
            reply_markup=deps.queue_keyboard(db, settings, 0),
        )

    elif action == "preview":
        preview_text = strip_quote_markers(str(draft.get("content") or ""), custom_emoji_aliases=settings.custom_emoji_aliases).strip() or "[пусто]"
        await deps.edit_callback_message(query, preview_text, reply_markup=deps.preview_keyboard(draft_id))

    elif action == "preview_back":
        refreshed = db.get_draft(draft_id) or draft
        await deps.edit_callback_message(
            query,
            deps.build_moderation_text(
                draft_id,
                refreshed["content"],
                refreshed.get("source_url"),
                refreshed.get("media_type"),
                refreshed.get("media_url"),
                source_image_url=refreshed.get("source_image_url"),
                custom_emoji_aliases=settings.custom_emoji_aliases,
            ),
            reply_markup=deps.moderation_keyboard(
                draft_id,
                str(refreshed.get("status") or ""),
                has_media=media_count(refreshed.get("media_url"), refreshed.get("media_type")) > 0,
                source_url=refreshed.get("source_url"),
                source_image_url=refreshed.get("source_image_url"),
            ),
        )

    elif action == "reject":
        if draft.get("status") in {"published", "publishing"}:
            await deps.edit_callback_message(query, "Опубликованный черновик нельзя отклонить.")
            return True
        db.update_status(draft_id, "rejected")
        await deps.edit_callback_message(query, f"❌ Черновик #{draft_id} отклонён.")

    elif action == "restore_draft":
        if draft.get("status") != "failed":
            await deps.edit_callback_message(
                query,
                f"Черновик #{draft_id} нельзя восстановить из статуса {draft.get('status')}. "
                "Восстановление доступно только для failed, чтобы не создать дубли публикаций.",
            )
            return True
        if not db.restore_draft(draft_id):
            await deps.edit_callback_message(query, f"Черновик #{draft_id} уже не в статусе failed и не был восстановлен.")
            return True
        refreshed = db.get_draft(draft_id) or draft
        await deps.edit_callback_message(
            query,
            deps.build_moderation_text(
                draft_id,
                str(refreshed.get("content") or ""),
                refreshed.get("source_url"),
                refreshed.get("media_type"),
                refreshed.get("media_url"),
                source_image_url=refreshed.get("source_image_url"),
                custom_emoji_aliases=settings.custom_emoji_aliases,
            ),
            reply_markup=deps.moderation_keyboard(
                draft_id,
                str(refreshed.get("status") or ""),
                has_media=media_count(refreshed.get("media_url"), refreshed.get("media_type")) > 0,
                source_url=refreshed.get("source_url"),
                source_image_url=refreshed.get("source_image_url"),
            ),
        )

    elif action == "rewrite":
        if not deps.can_edit(draft.get("status")):
            await deps.edit_callback_message(query, deps.status_guard_message("edit", draft.get("status")))
            return True
        rewritten = deps.rewrite_test_draft(draft["content"])
        db.update_draft_content(draft_id, rewritten)
        db.update_status(draft_id, "draft")
        await deps.edit_callback_message(
            query,
            deps.build_moderation_text(draft_id, rewritten, draft.get("source_url"), custom_emoji_aliases=settings.custom_emoji_aliases),
            reply_markup=deps.moderation_keyboard(
                draft_id,
                "draft",
                has_media=media_count(draft.get("media_url"), draft.get("media_type")) > 0,
                source_url=draft.get("source_url"),
                source_image_url=draft.get("source_image_url"),
            ),
        )

    elif action == "edit_text":
        if not deps.can_edit(draft.get("status")):
            await deps.edit_callback_message(query, deps.status_guard_message("edit", draft.get("status")))
            return True
        deps.clear_pending_media(context)
        deps.set_pending_edit(context, draft_id)
        await deps.edit_callback_message(
            query,
            f"✏️ Редактирование черновика #{draft_id}\n\n"
            "Пришли новый текст поста одним сообщением.\n\n"
            "Чтобы отменить, нажми кнопку ниже.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отменить редактирование", callback_data=f"edit_cancel:{draft_id}")]]
            ),
        )

    elif action == "attach_source_image":
        status = str(draft.get("status") or "")
        if not deps.can_edit(status):
            await deps.edit_callback_message(query, deps.status_guard_message("edit", status))
            return True
        source_image_url = str(draft.get("source_image_url") or "").strip()
        if not source_image_url:
            await deps.edit_callback_message(query, "У черновика нет сохранённой картинки источника.")
            return True
        if media_count(draft.get("media_url"), draft.get("media_type")) > 0:
            await deps.edit_callback_message(query, "У черновика уже есть медиа. Сначала убери его, потом прикрепи картинку источника.")
            return True
        db.attach_media(draft_id, source_image_url, "photo")
        db.update_status(draft_id, "draft")
        refreshed = db.get_draft(draft_id) or draft
        await deps.edit_callback_message(query, f"Картинка источника прикреплена к черновику #{draft_id}.")
        await deps.send_moderation_preview(
            context,
            settings.admin_id,
            draft_id,
            str(refreshed.get("content") or ""),
            source_url=refreshed.get("source_url"),
            media_url=refreshed.get("media_url"),
            media_type=refreshed.get("media_type"),
            source_image_url=refreshed.get("source_image_url"),
        )

    elif action == "attach_media_flow":
        if not deps.can_edit(draft.get("status")):
            await deps.edit_callback_message(query, deps.status_guard_message("edit", draft.get("status")))
            return True
        deps.clear_pending_edit(context)
        deps.set_pending_media(context, draft_id)
        await deps.edit_callback_message(
            query,
            f"📎 Прикрепление медиа к черновику #{draft_id}\n\n"
            "Пришли одно или несколько фото/видео/GIF.\n"
            "Можно отправлять по одному сообщению.\n"
            "Когда закончишь, нажми «✅ Готово».\n\n"
            "Лимит: до 10 файлов.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Готово", callback_data=f"attach_media_done:{draft_id}")],
                    [InlineKeyboardButton("❌ Отменить прикрепление", callback_data=f"attach_media_cancel:{draft_id}")],
                ]
            ),
        )

    elif action == "attach_media_done":
        if deps.get_pending_media(context) != draft_id:
            await deps.edit_callback_message(query, "Нет активного режима прикрепления для этого черновика.")
            return True
        status = str(draft.get("status") or "")
        if not deps.can_edit(status):
            deps.clear_pending_media(context)
            await deps.edit_callback_message(query, deps.status_guard_message("edit", status))
            return True
        items = context.user_data.get("pending_media_items") or []
        if not items:
            await query.answer("Медиа ещё не добавлено. Пришли фото, видео или GIF/анимацию.", show_alert=True)
            return True
        if len(items) == 1:
            db.attach_media(draft_id, items[0]["file_id"], items[0]["type"])
        else:
            db.attach_media(draft_id, deps.encode_media_group(items[:10]), "media_group")
        db.update_status(draft_id, "draft")
        deps.clear_pending_media(context)
        await deps.edit_callback_message(query, f"Готово. Медиа прикреплено к черновику #{draft_id}: {len(items)} файл(ов).")
        refreshed = db.get_draft(draft_id) or draft
        await deps.send_moderation_preview(
            context,
            settings.admin_id,
            draft_id,
            str(refreshed.get('content') or ''),
            source_url=refreshed.get("source_url"),
            media_url=refreshed.get("media_url"),
            media_type=refreshed.get("media_type"),
            source_image_url=refreshed.get("source_image_url"),
        )

    elif action == "attach_media_cancel":
        deps.clear_pending_media(context)
        if not draft:
            await deps.edit_callback_message(query, "Прикрепление медиа отменено.")
            return True
        await deps.edit_callback_message(
            query,
            deps.build_moderation_text(
                draft_id,
                draft["content"],
                draft.get("source_url"),
                draft.get("media_type"),
                draft.get("media_url"),
                source_image_url=draft.get("source_image_url"),
                custom_emoji_aliases=settings.custom_emoji_aliases,
            ),
            reply_markup=deps.moderation_keyboard(
                draft_id,
                str(draft.get("status") or ""),
                has_media=media_count(draft.get("media_url"), draft.get("media_type")) > 0,
                source_url=draft.get("source_url"),
                source_image_url=draft.get("source_image_url"),
            ),
        )
        if query.message:
            await query.message.reply_text("Прикрепление медиа отменено.")

    elif action == "remove_media":
        if not deps.can_edit(draft.get("status")):
            await deps.edit_callback_message(query, deps.status_guard_message("edit", draft.get("status")))
            return True
        db.clear_media(draft_id)
        db.update_status(draft_id, "draft")
        await deps.edit_callback_message(query, f"Медиа удалено из черновика #{draft_id}.")
        refreshed = db.get_draft(draft_id) or draft
        await deps.send_moderation_preview(
            context,
            settings.admin_id,
            draft_id,
            str(refreshed.get("content") or ""),
            source_url=refreshed.get("source_url"),
            source_image_url=refreshed.get("source_image_url"),
        )

    elif action == "edit_cancel":
        deps.clear_pending_edit(context)
        if not draft:
            await deps.edit_callback_message(query, "Редактирование отменено.")
            return True
        await deps.edit_callback_message(
            query,
            deps.build_moderation_text(
                draft_id,
                draft["content"],
                draft.get("source_url"),
                draft.get("media_type"),
                draft.get("media_url"),
                source_image_url=draft.get("source_image_url"),
                custom_emoji_aliases=settings.custom_emoji_aliases,
            ),
            reply_markup=deps.moderation_keyboard_for_draft(draft_id, draft),
        )
        if query.message:
            await query.message.reply_text("Редактирование отменено.")

    elif action in REWRITE_DRAFT_ACTIONS:
        status = str(draft.get("status") or "")
        if status not in ACTIONABLE_DRAFT_STATUSES:
            await deps.edit_callback_message(query, deps.status_guard_message("edit", status))
            return True
        if not settings.has_ai_provider:
            await deps.edit_callback_message(query, "AI-провайдер не настроен.")
            return True
        config = _rewrite_action_config(action)
        await deps.edit_callback_message(query, config["progress"].format(draft_id=draft_id))
        api_key, provider, base_url, extra_headers = deps.resolve_ai_provider(settings)
        logger.info("rewrite provider=%s model=%s mode=%s", provider, settings.model_polish, config["mode"])
        try:
            rewritten = await deps.run_rewrite_post_draft(
                api_key,
                model=settings.model_polish,
                draft_text=draft["content"],
                source_url=draft.get("source_url"),
                mode=config["mode"],
                max_chars=settings.post_max_chars,
                soft_chars=settings.post_soft_chars,
                base_url=base_url,
                extra_headers=extra_headers,
            )
        except EmptyAIResponseError:
            await deps.edit_callback_message(query, deps.empty_ai_reply_text.replace("Черновик не создан", "Черновик не обновлён"))
            return True
        estimated_cost = deps.estimate_ai_cost(provider, rewritten.prompt_tokens, rewritten.completion_tokens, settings)
        db.record_ai_usage(
            provider=provider,
            model=rewritten.model or settings.model_polish,
            operation=config["operation"],
            prompt_tokens=rewritten.prompt_tokens,
            completion_tokens=rewritten.completion_tokens,
            total_tokens=rewritten.total_tokens,
            estimated_cost_usd=estimated_cost,
            source_url=draft.get("source_url"),
            draft_id=draft_id,
        )
        logger.info(
            "AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s",
            provider,
            rewritten.model or settings.model_polish,
            config["operation"],
            rewritten.prompt_tokens,
            rewritten.completion_tokens,
            rewritten.total_tokens,
            estimated_cost,
        )
        db.update_draft_content(draft_id, rewritten.content)
        db.update_status(draft_id, "draft")
        refreshed = db.get_draft(draft_id) or draft
        await deps.edit_callback_message(
            query,
            deps.build_moderation_text(
                draft_id,
                rewritten.content,
                refreshed.get("source_url"),
                refreshed.get("media_type"),
                refreshed.get("media_url"),
                source_image_url=refreshed.get("source_image_url"),
                custom_emoji_aliases=settings.custom_emoji_aliases,
            ),
            reply_markup=deps.moderation_keyboard(
                draft_id,
                "draft",
                has_media=media_count(refreshed.get("media_url"), refreshed.get("media_type")) > 0,
                source_url=refreshed.get("source_url"),
                source_image_url=refreshed.get("source_image_url"),
            ),
        )

    elif action == "polish":
        if not deps.can_edit(draft.get("status")):
            await deps.edit_callback_message(query, deps.status_guard_message("edit", draft.get("status")))
            return True
        if not settings.has_ai_provider:
            await deps.edit_callback_message(query, "AI-провайдер не настроен.")
            return True
        await deps.edit_callback_message(query, "Улучшаю текст через Claude...")
        api_key, provider, base_url, extra_headers = deps.resolve_ai_provider(settings)
        logger.info("polish provider=%s model=%s", provider, settings.model_polish)
        polished = await deps.run_polish_post_draft(
            api_key,
            model=settings.model_polish,
            draft_text=draft["content"],
            source_url=draft.get("source_url"),
            max_chars=settings.post_max_chars,
            soft_chars=settings.post_soft_chars,
            base_url=base_url,
            extra_headers=extra_headers,
        )
        estimated_cost = deps.estimate_ai_cost(provider, polished.prompt_tokens, polished.completion_tokens, settings)
        db.record_ai_usage(
            provider=provider, model=polished.model or settings.model_polish, operation="polish",
            prompt_tokens=polished.prompt_tokens, completion_tokens=polished.completion_tokens,
            total_tokens=polished.total_tokens, estimated_cost_usd=estimated_cost, source_url=draft.get("source_url"), draft_id=draft_id
        )
        logger.info("AI usage provider=%s model=%s operation=%s prompt=%s completion=%s total=%s cost=%s", provider, polished.model or settings.model_polish, "polish", polished.prompt_tokens, polished.completion_tokens, polished.total_tokens, estimated_cost)
        db.update_draft_content(draft_id, polished.content)
        db.update_status(draft_id, "draft")
        await deps.edit_callback_message(
            query,
            deps.build_moderation_text(
                draft_id,
                polished.content,
                draft.get("source_url"),
                draft.get("media_type"),
                draft.get("media_url"),
                source_image_url=draft.get("source_image_url"),
                custom_emoji_aliases=settings.custom_emoji_aliases,
            ),
            reply_markup=deps.moderation_keyboard(
                draft_id,
                "draft",
                has_media=media_count(draft.get("media_url"), draft.get("media_type")) > 0,
                source_url=draft.get("source_url"),
                source_image_url=draft.get("source_image_url"),
            ),
        )

    elif action == "draft_info":
        reply_markup = deps.moderation_keyboard_for_draft(draft_id, draft)
        await deps.edit_callback_message(query, deps.full_draft_text(draft), reply_markup=reply_markup)

    else:
        await deps.edit_callback_message(query, "Неизвестное действие.")

    return True
