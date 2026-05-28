import asyncio
from types import SimpleNamespace

from bot import handlers
from bot.moderation_handlers import is_draft_moderation_action


class _FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = None
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_moderation_action_registry_includes_draft_card_callbacks():
    for action in [
        "publish",
        "schedule",
        "schedule_slot",
        "schedule_nearest",
        "reject",
        "edit_text",
        "polish",
        "rewrite_remove_fluff",
        "rewrite_shorten",
        "rewrite_neutralize_ads",
        "regenerate",
        "attach_source_image",
        "remove_media",
        "restore_draft",
    ]:
        assert is_draft_moderation_action(action)


def test_moderation_callback_dispatches_draft_card_callbacks(monkeypatch):
    calls = []

    async def _fake_handle(update, context, action, draft_id, slot, deps):
        calls.append((action, draft_id, slot, deps))
        return True

    monkeypatch.setattr(handlers, "handle_draft_moderation_callback", _fake_handle)
    async def _fake_cleanup(*args, **kwargs):
        return False

    monkeypatch.setattr(handlers, "handle_cleanup_callback", _fake_cleanup)

    query = _FakeQuery("schedule_slot:42:09:30")
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(
        bot_data={"settings": SimpleNamespace(admin_id=1), "db": object()},
        user_data={},
    )

    asyncio.run(handlers.moderation_callback(update, context))

    assert calls
    action, draft_id, slot, deps = calls[0]
    assert action == "schedule_slot"
    assert draft_id == 42
    assert slot == "09:30"
    assert deps.edit_callback_message is handlers._edit_callback_message
    assert deps.publish_to_channel is handlers.publish_to_channel
    assert deps.queue_keyboard is handlers._queue_keyboard
    assert deps.schedule_draft_to_nearest_slot is handlers._schedule_draft_to_nearest_slot
    assert deps.rewrite_test_draft is handlers.rewrite_test_draft
    assert deps.encode_media_group is handlers.encode_media_group
    assert deps.empty_ai_reply_text is handlers.EMPTY_AI_REPLY_TEXT
