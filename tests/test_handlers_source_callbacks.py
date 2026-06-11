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
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


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


def test_source_test_duplicate_click_blocked(monkeypatch):
    row = {"id": 7, "source_type": "telegram", "name": "tg", "value": "openai", "enabled": 1, "last_status": None, "last_error": None}

    class _FakeDB:
        def get_managed_source(self, sid):
            return row if sid == 7 else None

    calls: list[str] = []

    async def _fake_edit(_query, text, reply_markup=None):
        calls.append(text)

    monkeypatch.setattr(handlers, "_edit_callback_message", _fake_edit)

    class _FakeApp:
        bot_data = {"source_test_running": {7}}

        def create_task(self, coro):
            calls.append("scheduled")
            coro.close()

    context = SimpleNamespace(bot_data={"settings": SimpleNamespace(admin_id=1), "db": _FakeDB()}, application=_FakeApp())
    update = SimpleNamespace(callback_query=_FakeCallbackQuery("source_test:7"))
    asyncio.run(handlers._handle_sources_callback(update, context, "source_test:7"))
    assert "scheduled" not in calls
    assert any("уже идёт" in text for text in calls)


def test_sources_status_duplicate_click_blocked(monkeypatch):
    calls: list[str] = []

    async def _fake_edit(_query, text, reply_markup=None):
        calls.append(text)

    monkeypatch.setattr(handlers, "_edit_callback_message", _fake_edit)

    context = SimpleNamespace(
        bot_data={"settings": SimpleNamespace(admin_id=1), "db": object()},
        application=SimpleNamespace(bot_data={"sources_check_running": True}, create_task=lambda coro: coro.close()),
    )
    query = _FakeCallbackQuery("menu_sources_status")
    asyncio.run(handlers._handle_menu_callback(SimpleNamespace(callback_query=query), context, "menu_sources_status"))
    assert any("уже идёт" in text for text in calls)


def test_sources_inventory_sends_every_part_and_returns(monkeypatch):
    calls: list[str] = []

    async def _fake_edit(_query, text, reply_markup=None):
        calls.append(text)

    class _FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None):
            calls.append(text)

    context = SimpleNamespace(
        bot_data={"settings": SimpleNamespace(admin_id=1), "db": object()},
        bot=_FakeBot(),
        application=SimpleNamespace(bot_data={}),
        user_data={},
    )
    update = SimpleNamespace(callback_query=_FakeCallbackQuery("sources_inventory"))

    asyncio.run(
        handlers.source_handlers.handle_sources_callback(
            update=update,
            context=context,
            data="sources_inventory",
            edit_callback_message=_fake_edit,
            sources_hub_keyboard=lambda: None,
            source_card_keyboard=lambda *_args: None,
            run_source_test_background=lambda **_kwargs: None,
            render_sources_inventory=lambda _settings, _db: ["part 1", "part 2", "part 3"],
        )
    )

    assert calls == ["part 1", "part 2", "part 3"]


def test_moderation_callback_dispatches_sources_inventory(monkeypatch):
    calls = []

    async def _fake_sources_handler(update, context, data):
        calls.append((update, context, data))

    monkeypatch.setattr(handlers, "_handle_sources_callback", _fake_sources_handler)

    query = _FakeCallbackQuery("sources_inventory")
    query.from_user = SimpleNamespace(id=1)
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(bot_data={"settings": SimpleNamespace(admin_id=1), "db": object()}, user_data={})

    asyncio.run(handlers.moderation_callback(update, context))

    assert calls == [(update, context, "sources_inventory")]


def test_moderation_callback_dispatches_sources_health(monkeypatch):
    calls = []

    async def _fake_menu_handler(update, context, data):
        calls.append((update, context, data))

    monkeypatch.setattr(handlers, "_handle_menu_callback", _fake_menu_handler)

    query = _FakeCallbackQuery("sources_health")
    query.from_user = SimpleNamespace(id=1)
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(bot_data={"settings": SimpleNamespace(admin_id=1), "db": object()}, user_data={})

    asyncio.run(handlers.moderation_callback(update, context))

    assert calls == [(update, context, "sources_health")]
