import asyncio
from types import SimpleNamespace

from bot import handlers


class _FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = None
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_topics_navigation_callback_dispatches_to_topic_handler(monkeypatch):
    calls = []

    async def _fake_topics_handler(update, context, data):
        calls.append((update, context, data))

    monkeypatch.setattr(handlers, "_handle_topics_callback", _fake_topics_handler)

    query = _FakeQuery("topics_hot:0")
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(
        bot_data={"settings": SimpleNamespace(admin_id=1), "db": object()},
        user_data={},
    )

    asyncio.run(handlers.moderation_callback(update, context))

    assert calls == [(update, context, "topics_hot:0")]
    assert query.answers == [(None, False)]


def test_topic_action_callback_dispatches_to_topic_action_handler(monkeypatch):
    calls = []

    async def _fake_topic_action(update, context, action, topic_id, query):
        calls.append((update, context, action, topic_id, query))
        return True

    async def _fake_cleanup(*args, **kwargs):
        return False

    monkeypatch.setattr(handlers, "handle_topic_moderation_action", _fake_topic_action)
    monkeypatch.setattr(handlers, "handle_cleanup_callback", _fake_cleanup)

    query = _FakeQuery("topic_generate:42")
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(
        bot_data={"settings": SimpleNamespace(admin_id=1), "db": object()},
        user_data={},
    )

    asyncio.run(handlers.moderation_callback(update, context))

    assert calls == [(update, context, "topic_generate", 42, query)]
    assert query.answers == [(None, False)]
