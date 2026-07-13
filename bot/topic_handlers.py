"""Topic collection, navigation, and topic callback handlers."""

from __future__ import annotations

import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from bot.database import DraftDatabase
from bot.telegram_safety import (
    safe_edit_message_text,
    safe_reply_text,
    safe_send_message,
    split_telegram_text,
    truncate_telegram_text,
)

logger = logging.getLogger(__name__)


def _legacy_handlers():
    # Imported lazily to keep the extraction mechanical and avoid circular imports
    # while shared topic-generation/collection helpers still live in handlers.py.
    from bot import handlers

    return handlers


async def collect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not handlers._is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    if context.bot_data.get("topics_collect_running"):
        if update.message:
            await update.message.reply_text("Сбор тем уже идёт. Я пришлю результат, когда закончу.")
        return

    progress_message = None
    if update.message:
        progress_message = await update.message.reply_text("🔄 Начал сбор тем. Можно продолжать пользоваться ботом — результат пришлю сюда.")

    context.bot_data["topic_detail_debug"] = False
    context.bot_data["topics_collect_running"] = True

    async def _work() -> None:
        try:
            stats, items, inserted = await handlers._collect_topics_with_stats(db, settings=settings)
            if update.message:
                text = handlers._render_collect_text(stats, items, inserted)
                if progress_message:
                    try:
                        await safe_edit_message_text(progress_message, text, reply_markup=handlers._collect_result_keyboard())
                        return
                    except Exception as exc:
                        logger.warning("Failed to edit collect progress message: %s", exc)
                await safe_reply_text(update.message, text, reply_markup=handlers._collect_result_keyboard())
        except Exception:
            logger.exception("Background topic collection failed")
            if update.message:
                await safe_reply_text(update.message, "Не удалось собрать темы. Открой /sources_status и попробуй ещё раз.")
        finally:
            context.bot_data["topics_collect_running"] = False

    application = getattr(context, "application", None)
    if application is not None:
        application.create_task(_work())
        return
    await _work()


async def collect_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not handlers._is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    if context.bot_data.get("topics_collect_running"):
        if update.message:
            await update.message.reply_text("Сбор тем уже идёт. Дождись результата перед диагностическим запуском.")
        return

    progress_message = None
    if update.message:
        progress_message = await update.message.reply_text("🔄 Начал диагностический сбор. Можно продолжать пользоваться ботом.")
    context.bot_data["topic_detail_debug"] = True
    context.bot_data["topics_collect_running"] = True

    async def _work() -> None:
        try:
            debug_started = time.monotonic()
            items, reports = await handlers._run_collect_topics_with_diagnostics(settings=settings, db=db)
            source_seconds = time.monotonic() - debug_started
            stats, _all_items, inserted = await handlers._collect_topics_with_stats(db, items=items, settings=settings)
            stats.source_seconds = source_seconds
            stats.total_seconds = source_seconds + stats.store_seconds + stats.ai_seconds
            text = handlers._render_collect_text(stats, items, inserted, debug=True)
            ok = sum(1 for r in reports if r.status == "ok")
            empty = sum(1 for r in reports if r.status == "empty")
            skipped = sum(1 for r in reports if r.status == "skipped")
            errors = sum(1 for r in reports if r.status == "error")
            problems = [r for r in reports if r.status in {"error", "empty"}][:12]
            problem_lines = [f"- {r.name}: {r.error or 'empty'}" if r.status == "error" else f"- {r.name}: empty" for r in problems]
            skipped_lines = [f"- {r.name}: {r.error or 'пропущено'}" for r in reports if r.status == "skipped"][:8]
            combined = text.replace("🧠 Темы собраны", "🧠 Темы собраны с диагностикой")
            combined += f"\n\nСвежесть: пропущено старых тем: {stats.stale} (лимит {getattr(settings, 'max_topic_age_days', 14)} дн.)"
            combined += f"\n\nИсточники:\nРаботают: {ok}\nПустые: {empty}\nОтключены/пропущены: {skipped}\nОшибки: {errors}"
            if skipped_lines:
                combined += "\n\nОтключено/пропущено:\n" + "\n".join(skipped_lines)
            if problem_lines:
                combined += "\n\nПроблемы:\n" + "\n".join(problem_lines)
            if len([r for r in reports if r.status in {'error', 'empty'}]) > 12:
                combined += "\nПоказаны первые 12 проблем."
            if update.message:
                parts = split_telegram_text(combined)
                if progress_message:
                    try:
                        await safe_edit_message_text(
                            progress_message,
                            parts[0],
                            reply_markup=handlers._collect_result_keyboard() if len(parts) == 1 else None,
                        )
                        for index, part in enumerate(parts[1:], start=1):
                            await safe_reply_text(
                                update.message,
                                part,
                                reply_markup=handlers._collect_result_keyboard() if index == len(parts) - 1 else None,
                            )
                        return
                    except Exception as exc:
                        logger.warning("Failed to edit collect debug progress message: %s", exc)
                await safe_reply_text(update.message, combined, reply_markup=handlers._collect_result_keyboard())
        except Exception:
            logger.exception("Background diagnostic topic collection failed")
            if update.message:
                await safe_reply_text(update.message, "Диагностический сбор завершился ошибкой. Попробуй /sources_status.")
        finally:
            context.bot_data["topics_collect_running"] = False

    application = getattr(context, "application", None)
    if application is not None:
        application.create_task(_work())
        return
    await _work()


async def topics_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not handlers._is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    hot_topics = handlers._topics_for_kind(db, "hot", limit=5)
    new_topics = handlers._topics_for_kind(db, "new", limit=5)
    if not hot_topics and not new_topics:
        text = handlers._render_topics_hub_text(db) + "\n\nТем пока нет. Запусти /collect или /collect_debug."
        if update.message:
            await update.message.reply_text(text, reply_markup=handlers._topics_hub_keyboard())
        return
    if update.message:
        await update.message.reply_text(handlers._render_topics_hub_text(db), reply_markup=handlers._topics_hub_keyboard())
    if hot_topics:
        if update.message:
            await safe_reply_text(
                update.message,
                handlers._render_topic_preview_list("🔥 Лучшие горячие", hot_topics),
                reply_markup=handlers._topics_hub_keyboard(),
            )
    else:
        if update.message:
            await update.message.reply_text(
                "Горячих тем пока нет, но есть свежие темы. Показываю лучшие новые.",
                reply_markup=handlers._topics_hub_keyboard(),
            )
            await safe_reply_text(
                update.message,
                handlers._render_topic_preview_list("🆕 Лучшие новые", new_topics),
                reply_markup=handlers._topics_hub_keyboard(),
            )


async def _send_topic_cards(context: ContextTypes.DEFAULT_TYPE, settings, topics: list[dict]) -> None:
    handlers = _legacy_handlers()
    db: DraftDatabase | None = context.bot_data.get("db")
    for topic in topics:
        if db is not None:
            topic = await handlers._ensure_topic_candidate_display_metadata(
                int(topic["id"]), settings, db, debug=bool(context.bot_data.get("topic_detail_debug")),
            ) or topic
        await safe_send_message(
            context.bot,
            chat_id=settings.admin_id,
            text=truncate_telegram_text(handlers._topic_card_text(topic)),
            reply_markup=handlers._topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or "")),
            link_preview_options=handlers._disabled_link_preview_options(),
        )


async def topics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not handlers._is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    limit = handlers._parse_topic_limit(context, default=5)
    topics = db.list_topic_candidates(limit=limit, status="new", order_by_score=True)
    if not topics:
        if update.message:
            await update.message.reply_text("Пока нет тем. Запусти /collect")
        return

    await _send_topic_cards(context, settings, topics)
    if update.message:
        next_limit = min(30, max(limit + 10, 20))
        await update.message.reply_text(f"Показал {len(topics)} тем. Можно открыть больше: /topics {next_limit}")


async def topics_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not handlers._is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    topics = db.list_topic_candidates(limit=10, status=None, order_by_score=True)
    if not topics:
        if update.message:
            await update.message.reply_text("Пока нет тем. Запусти /collect")
        return
    for topic in topics:
        status = topic.get("status") or "new"
        topic = await handlers._ensure_topic_candidate_display_metadata(
            int(topic["id"]), settings, db, debug=bool(context.bot_data.get("topic_detail_debug")),
        ) or topic
        await safe_send_message(
            context.bot,
            chat_id=settings.admin_id,
            text=truncate_telegram_text(f"{handlers._topic_card_text(topic)}\nСтатус: {status}"),
            reply_markup=handlers._topic_actions_keyboard(int(topic["id"]), str(topic.get("url") or "")),
            link_preview_options=handlers._disabled_link_preview_options(),
        )


async def _topics_fun_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not handlers._is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return

    topics_by_category = db.list_topic_candidates_filtered(limit=20, status="new", categories=["drama", "meme"])
    topics_by_group = db.list_topic_candidates_filtered(limit=20, status="new", source_groups=["community", "github", "x", "custom"])

    merged: dict[int, dict] = {}
    for topic in topics_by_category + topics_by_group:
        topic_id = int(topic["id"])
        merged[topic_id] = topic

    limit = handlers._parse_topic_limit(context, default=5)
    topics = sorted(merged.values(), key=lambda t: (int(t.get("score") or 0), str(t.get("created_at") or "")), reverse=True)[:limit]
    if not topics:
        if update.message:
            await update.message.reply_text("По фильтру пока нет тем. Запусти /collect")
        return

    await _send_topic_cards(context, settings, topics)
    if update.message:
        next_limit = min(30, max(limit + 10, 20))
        await update.message.reply_text(f"Показал {len(topics)} тем. Можно открыть больше: /topics_fun {next_limit}")


async def _topics_filtered_editorial_command(update: Update, context: ContextTypes.DEFAULT_TYPE, lanes=None, formats=None, categories=None, min_score: int = 0, command_name: str = "topics") -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not handlers._is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    topics = db.list_topic_candidates_by_editorial(limit=handlers._parse_topic_limit(context, default=5), lanes=lanes, formats=formats, categories=categories, min_score=min_score)
    if not topics:
        if update.message:
            await update.message.reply_text("По фильтру пока нет тем. Запусти /collect")
        return
    await _send_topic_cards(context, settings, topics)


async def _topics_filtered_command(update: Update, context: ContextTypes.DEFAULT_TYPE, categories=None, source_groups=None, command_name: str = "topics") -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not handlers._is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    limit = handlers._parse_topic_limit(context, default=5)
    topics = db.list_topic_candidates_filtered(limit=limit, status="new", categories=categories, source_groups=source_groups)
    if not topics:
        if update.message:
            await update.message.reply_text("По фильтру пока нет тем. Запусти /collect")
        return
    await _send_topic_cards(context, settings, topics)
    if update.message:
        next_limit = min(30, max(limit + 10, 20))
        await update.message.reply_text(f"Показал {len(topics)} тем. Можно открыть больше: /{command_name} {next_limit}")


async def topics_tools_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _topics_filtered_command(update, context, categories=["tool", "creator", "guide", "dev", "mobile"], command_name="topics_tools")


async def topics_news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _topics_filtered_command(update, context, categories=["news", "model", "agent", "research", "business", "privacy"], command_name="topics_news")


async def topics_fun_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _topics_fun_command(update, context)


async def topics_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _topics_filtered_editorial_command(update, context, lanes=["short_video", "creator", "tool", "meme"], formats=["short_video", "tool_review", "meme"], min_score=62, command_name="topics_video")


async def topics_guides_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _topics_filtered_editorial_command(update, context, lanes=["guide"], categories=["guide"], command_name="topics_guides")


async def topics_best_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    if not handlers._is_admin(update.effective_user.id if update.effective_user else None, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    topics = db.get_balanced_topic_shortlist(limit=handlers._parse_topic_limit(context, default=12), hours=48, min_score=60)
    if update.message:
        await update.message.reply_text("⭐ Лучшие темы на сегодня")
    await _send_topic_cards(context, settings, topics)


async def topics_hot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not handlers._is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    limit = handlers._parse_topic_limit(context, default=15)
    topics = db.list_topic_candidates_min_score(limit=limit, status="new", min_score=75)
    if not topics:
        fallback_topics = db.list_topic_candidates(limit=limit, status="new", order_by_score=True)
        if fallback_topics:
            if update.message:
                await update.message.reply_text("Горячих тем пока нет, но есть свежие темы. Показываю лучшие новые.", reply_markup=handlers._topics_hub_keyboard())
            topics = fallback_topics
        else:
            if update.message:
                await update.message.reply_text("Тем пока нет. Запусти /collect или /collect_debug.", reply_markup=handlers._topics_hub_keyboard())
            return
    await _send_topic_cards(context, settings, topics)
    if update.message:
        await update.message.reply_text(f"Показал {len(topics)} тем. Можно открыть больше: /topics_hot 30")


async def handle_topics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    query = update.callback_query
    if not query:
        return
    kind = data.split(":", 1)[0].removeprefix("topics_")
    titles = {
        "hot": "🔥 Горячие темы",
        "new": "🆕 Лучшие новые темы",
        "tools": "🛠 Инструменты",
        "news": "📰 Новости",
        "fun": "😄 Живые темы",
    }
    topics = handlers._topics_for_kind(db, kind, limit=10)
    if kind == "hot" and not topics:
        topics = handlers._topics_for_kind(db, "new", limit=10)
        text = "Горячих тем пока нет, но есть свежие темы. Показываю лучшие новые."
    else:
        text = handlers._render_topic_preview_list(titles.get(kind, "🧠 Темы"), topics)
    if not topics:
        text = "Тем пока нет. Запусти /collect или /collect_debug."
    await handlers._edit_callback_message(query, text, reply_markup=handlers._topics_hub_keyboard())
    await _send_topic_cards(context, settings, topics[:5])


async def handle_topic_moderation_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, topic_id: int, query) -> bool:
    handlers = _legacy_handlers()
    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]

    if action == "topic_generate":
        new_draft_id, error = await handlers._create_draft_from_topic(
            context=context, settings=settings, db=db, topic_id=topic_id
        )
        if new_draft_id is None:
            await handlers._edit_callback_message(query, error or "Не удалось создать черновик.")
            return True
        success_text = f"Создан черновик #{new_draft_id} из темы #{topic_id}."
        if error:
            success_text = f"{success_text}\n\n⚠️ {error}"
        await handlers._edit_callback_message(query, success_text)
        return True

    if action == "topic_reenrich":
        await handlers._edit_callback_message(query, f"🔁 Перевожу тему #{topic_id} заново...")
        topic, error = await handlers._reenrich_topic_candidate_display_metadata(topic_id, settings, db)
        if not topic:
            await handlers._edit_callback_message(query, error or "Не удалось заново перевести тему.")
            return True
        text = handlers._topic_card_text(topic)
        if error:
            text += f"\n\n⚠️ {error}"
        await handlers._edit_callback_message(
            query,
            text,
            reply_markup=handlers._topic_actions_keyboard(topic_id, str(topic.get("url") or "")),
        )
        return True

    if action == "reject_topic":
        topic = db.get_topic_candidate(topic_id)
        if not topic:
            await handlers._edit_callback_message(query, f"Тема #{topic_id} не найдена.")
            return True
        db.update_topic_status(topic_id, "rejected")
        await handlers._edit_callback_message(query, f"Тема #{topic_id} отклонена.")
        return True

    return False


# Backward-compatible private name used by existing tests/selftests.
_handle_topics_callback = handle_topics_callback
