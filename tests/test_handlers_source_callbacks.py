import asyncio
from types import SimpleNamespace
from telegram.error import BadRequest

from bot import handlers


class _FakeQuery:
    def __init__(self, exc: Exception | None = None):
        self._exc = exc
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        if self._exc:
            raise self._exc
        self.answers.append((text, show_alert))


def test_safe_answer_callback_ignores_old_query_badrequest():
    query = _FakeQuery(BadRequest("Query is too old and response timeout expired or query id is invalid"))
    asyncio.run(handlers._safe_answer_callback(query, "ok"))


class _FakeCallbackQuery:
    def __init__(self, data):
        self.data = data
        self.message = None


def test_source_test_telegram_returns_after_schedule(monkeypatch):
    row = {"id": 7, "source_type": "telegram", "name": "tg", "value": "openai", "enabled": 1, "last_status": None, "last_error": None}

    class _FakeDB:
        def get_managed_source(self, sid):
            return row if sid == 7 else None

    calls: list[str] = []

    async def _fake_edit(_query, text, reply_markup=None):
        calls.append(text)

    monkeypatch.setattr(handlers, "_edit_callback_message", _fake_edit)

    class _FakeApp:
        bot_data = {}

        def create_task(self, coro):
            calls.append("scheduled")
            coro.close()

    context = SimpleNamespace(
        bot_data={"settings": SimpleNamespace(admin_id=1), "db": _FakeDB()},
        application=_FakeApp(),
    )
    update = SimpleNamespace(callback_query=_FakeCallbackQuery("source_test:7"))

    asyncio.run(handlers._handle_sources_callback(update, context, "source_test:7"))

    assert "scheduled" in calls
    assert "Неизвестное действие источников." not in calls
