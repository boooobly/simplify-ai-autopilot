import asyncio
from types import SimpleNamespace

import main


def test_global_error_handler_is_registered():
    class _Application:
        def __init__(self):
            self.error_handlers = []

        def add_error_handler(self, callback):
            self.error_handlers.append(callback)

    application = _Application()

    main._add_global_error_handler(application)

    assert application.error_handlers == [main.telegram_error_handler]


def test_global_error_handler_notifies_admin_without_leaking_exception():
    class _Bot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)

    bot = _Bot()
    context = SimpleNamespace(
        error=RuntimeError("secret failure details"),
        bot=bot,
        bot_data={"settings": SimpleNamespace(admin_id=42)},
    )

    asyncio.run(main.telegram_error_handler(None, context))

    assert bot.messages[0]["chat_id"] == 42
    assert "secret failure details" not in bot.messages[0]["text"]


def test_global_error_handler_does_not_raise_when_notification_fails():
    class _Bot:
        async def send_message(self, **kwargs):
            raise RuntimeError("Telegram unavailable")

    context = SimpleNamespace(
        error=RuntimeError("handler failure"),
        bot=_Bot(),
        bot_data={"settings": SimpleNamespace(admin_id=42)},
    )

    asyncio.run(main.telegram_error_handler(None, context))
