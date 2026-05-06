"""Entry point for Telegram moderation bot MVP."""

from __future__ import annotations

import logging
import os

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.config import load_settings, startup_diagnostics
from bot.database import DraftDatabase
from bot.handlers import (
    admin_url_message,
    draft_command,
    generate_command,
    collect_command,
    collect_debug_command,
    attach_media_command,
    moderation_callback,
    start_command,
    topics_command,
    topics_all_command,
    topics_tools_command,
    topics_news_command,
    topics_fun_command,
    topics_hot_command,
    sources_status_command,
    usage_7d_command,
    usage_month_command,
    style_guide_command,
    usage_today_command,
    drafts_command,
    draft_info_command,
    delete_draft_command,
    menu_command,
    queue_today_command,
    queue_tomorrow_command,
    plan_day_command,
    plan_tomorrow_command,
    generate_plan_day_command,
    generate_plan_tomorrow_command,
    schedule_generated_plan_day_command,
    schedule_generated_plan_tomorrow_command,
    unschedule_command,
    restore_draft_command,
    failed_drafts_command,
    emoji_ids_command,
    health_command,
)
from bot.publisher import run_scheduled_publishing


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )




async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Запустить бота"),
            BotCommand("menu", "Главное меню"),
            BotCommand("plan_day", "План тем на сегодня"),
            BotCommand("generate_plan_day", "Создать черновики из плана"),
            BotCommand("schedule_generated_plan_day", "Поставить черновики в очередь"),
            BotCommand("queue_today", "Очередь на сегодня"),
            BotCommand("drafts", "Последние черновики"),
            BotCommand("collect_debug", "Сбор тем с диагностикой"),
            BotCommand("topics_hot", "Горячие темы"),
            BotCommand("sources_status", "Статус источников"),
            BotCommand("usage_today", "Расходы ИИ сегодня"),
            BotCommand("style_guide", "Сводка по стилю"),
            BotCommand("emoji_ids", "ID кастомных emoji"),
            BotCommand("health", "Статус бота"),
        ]
    )

def main() -> None:
    setup_logging()

    settings = load_settings()
    for line in startup_diagnostics(settings):
        logging.getLogger(__name__).info("startup: %s", line)
    db = DraftDatabase(settings.db_path)

    application = Application.builder().token(settings.bot_token).post_init(_post_init).build()
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("draft", draft_command))
    application.add_handler(CommandHandler("generate", generate_command))
    application.add_handler(CommandHandler("collect", collect_command))
    application.add_handler(CommandHandler("collect_debug", collect_debug_command))
    application.add_handler(CommandHandler("sources_status", sources_status_command))
    application.add_handler(CommandHandler("attach_media", attach_media_command))
    application.add_handler(CommandHandler("topics", topics_command))
    application.add_handler(CommandHandler("topics_all", topics_all_command))
    application.add_handler(CommandHandler("topics_tools", topics_tools_command))
    application.add_handler(CommandHandler("topics_news", topics_news_command))
    application.add_handler(CommandHandler("topics_fun", topics_fun_command))
    application.add_handler(CommandHandler("topics_hot", topics_hot_command))
    application.add_handler(CommandHandler("usage_today", usage_today_command))
    application.add_handler(CommandHandler("usage_7d", usage_7d_command))
    application.add_handler(CommandHandler("usage_month", usage_month_command))
    application.add_handler(CommandHandler("style_guide", style_guide_command))
    application.add_handler(CommandHandler("drafts", drafts_command))
    application.add_handler(CommandHandler("draft_info", draft_info_command))
    application.add_handler(CommandHandler("delete_draft", delete_draft_command))
    application.add_handler(CommandHandler("queue_today", queue_today_command))
    application.add_handler(CommandHandler("queue_tomorrow", queue_tomorrow_command))
    application.add_handler(CommandHandler("plan_day", plan_day_command))
    application.add_handler(CommandHandler("plan_tomorrow", plan_tomorrow_command))
    application.add_handler(CommandHandler("generate_plan_day", generate_plan_day_command))
    application.add_handler(CommandHandler("generate_plan_tomorrow", generate_plan_tomorrow_command))
    application.add_handler(CommandHandler("schedule_generated_plan_day", schedule_generated_plan_day_command))
    application.add_handler(CommandHandler("schedule_generated_plan_tomorrow", schedule_generated_plan_tomorrow_command))
    application.add_handler(CommandHandler("unschedule", unschedule_command))
    application.add_handler(CommandHandler("restore_draft", restore_draft_command))
    application.add_handler(CommandHandler("failed_drafts", failed_drafts_command))
    application.add_handler(CommandHandler("emoji_ids", emoji_ids_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(MessageHandler(~filters.COMMAND, admin_url_message))
    application.add_handler(CallbackQueryHandler(moderation_callback))

    if application.job_queue is None:
        raise RuntimeError(
            "JobQueue недоступен. Установи зависимость python-telegram-bot[job-queue]."
        )

    application.job_queue.run_repeating(run_scheduled_publishing, interval=60, first=10)

    # Railway sets PORT by default for web services. This bot uses long polling,
    # so it should run as a worker process. PORT is ignored safely.
    _ = os.getenv("PORT")

    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
