import asyncio
from types import SimpleNamespace

from bot.handlers import emoji_test_command


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeBot:
    def __init__(self, valid_ids=()):
        self.valid_ids = set(valid_ids)
        self.messages = []
        self.validated_ids = []

    async def get_custom_emoji_stickers(self, custom_emoji_ids):
        self.validated_ids.extend(custom_emoji_ids)
        return tuple(
            SimpleNamespace(custom_emoji_id=emoji_id)
            for emoji_id in custom_emoji_ids
            if emoji_id in self.valid_ids
        )

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


def _context(settings, bot, args=None):
    return SimpleNamespace(bot_data={"settings": settings}, bot=bot, args=args or [])


def _update(user_id=42):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=42),
        message=FakeMessage(),
    )


def test_emoji_test_is_admin_only():
    settings = SimpleNamespace(
        admin_id=42,
        channel_id="@channel",
        custom_emoji_map={"🔥": "111"},
        custom_emoji_aliases={},
    )
    update = _update(user_id=7)
    bot = FakeBot(valid_ids={"111"})

    asyncio.run(emoji_test_command(update, _context(settings, bot)))

    assert bot.messages == []
    assert update.message.replies[0][0] == "Нет доступа."


def test_emoji_test_explains_empty_configuration():
    settings = SimpleNamespace(
        admin_id=42,
        channel_id="@channel",
        custom_emoji_map={},
        custom_emoji_aliases={},
    )
    update = _update()

    asyncio.run(emoji_test_command(update, _context(settings, FakeBot())))

    reply = update.message.replies[0][0]
    assert "CUSTOM_EMOJI_MAP" in reply
    assert "CUSTOM_EMOJI_ALIASES" in reply


def test_emoji_test_validates_ids_and_sends_html_to_admin_and_channel():
    settings = SimpleNamespace(
        admin_id=42,
        channel_id="@channel",
        custom_emoji_map={"🔥": "111"},
        custom_emoji_aliases={"thought": ("💭", "222")},
    )
    update = _update()
    bot = FakeBot(valid_ids={"111"})

    asyncio.run(emoji_test_command(update, _context(settings, bot, args=["channel"])))

    assert bot.validated_ids == ["111", "222"]
    assert [message["chat_id"] for message in bot.messages] == [42, "@channel"]
    assert all(message["parse_mode"] == "HTML" for message in bot.messages)
    assert all("<tg-emoji" in message["text"] for message in bot.messages)
    assert "Post-style raw emoji sample" in bot.messages[0]["text"]
    assert "Заголовок" in bot.messages[0]["text"]
    assert "пункт один" in bot.messages[0]["text"]
    assert "финальная мысль" in bot.messages[0]["text"]
    assert "Bot API invalid ids: 222" in bot.messages[0]["text"]
    assert '<tg-emoji emoji-id="222">' not in bot.messages[0]["text"]
    assert "alias=thought id=222 INVALID" in bot.messages[0]["text"]
    assert update.message.replies[-1][0] == "Тест custom emoji отправлен в CHANNEL_ID."


def test_emoji_test_debug_returns_literal_rendered_html_to_admin():
    settings = SimpleNamespace(
        admin_id=42,
        channel_id="@channel",
        custom_emoji_map={"🔥": "111", "➖": "222", "💭": "333"},
        custom_emoji_aliases={},
    )
    update = _update()
    bot = FakeBot(valid_ids={"111", "222", "333"})

    asyncio.run(emoji_test_command(update, _context(settings, bot, args=["debug"])))

    assert [message["chat_id"] for message in bot.messages] == [42]
    assert bot.messages[0]["parse_mode"] == "HTML"
    assert bot.messages[0]["text"].count("<tg-emoji") >= 3
    debug_text, debug_kwargs = update.message.replies[-1]
    assert "Rendered HTML #1" in debug_text
    assert '<tg-emoji emoji-id="111">🔥</tg-emoji>' in debug_text
    assert '<tg-emoji emoji-id="222">➖</tg-emoji>' in debug_text
    assert '<tg-emoji emoji-id="333">💭</tg-emoji>' in debug_text
    assert debug_kwargs["parse_mode"] is None


def test_emoji_test_survives_api_validation_failure():
    class FailingBot(FakeBot):
        async def get_custom_emoji_stickers(self, custom_emoji_ids):
            raise RuntimeError("telegram unavailable")

    settings = SimpleNamespace(
        admin_id=42,
        channel_id="@channel",
        custom_emoji_map={"🔥": "111"},
        custom_emoji_aliases={},
    )
    bot = FailingBot()

    asyncio.run(emoji_test_command(_update(), _context(settings, bot)))

    assert len(bot.messages) == 1
    assert "Bot API validation: unavailable (RuntimeError)" in bot.messages[0]["text"]
