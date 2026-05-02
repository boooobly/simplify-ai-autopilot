"""Entry point for Telegram moderation bot MVP."""

from __future__ import annotations

import logging
import os

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.config import load_settings
from bot.database import DraftDatabase
from bot.handlers import draft_command, generate_command, moderation_callback, start_command


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )


def main() -> None:
    setup_logging()

    settings = load_settings()
    db = DraftDatabase(settings.db_path)

    application = Application.builder().token(settings.bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("draft", draft_command))
    application.add_handler(CommandHandler("generate", generate_command))
    application.add_handler(CallbackQueryHandler(moderation_callback))

    # Railway sets PORT by default for web services. This bot uses long polling,
    # so it should run as a worker process. PORT is ignored safely.
    _ = os.getenv("PORT")

    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
