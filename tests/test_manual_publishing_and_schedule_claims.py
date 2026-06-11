from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.database import DraftDatabase
from bot.moderation_handlers import handle_draft_moderation_callback


def test_manual_publish_claim_is_atomic_for_actionable_draft(tmp_path):
    db = DraftDatabase(str(tmp_path / "drafts.db"))
    draft_id = db.create_draft("publish once")

    allowed = ("draft", "approved", "scheduled")
    assert db.mark_draft_publishing(draft_id, allowed_statuses=allowed) is True
    assert db.mark_draft_publishing(draft_id, allowed_statuses=allowed) is False
    assert db.get_draft(draft_id)["status"] == "publishing"


def test_schedule_claim_rejects_an_occupied_slot(tmp_path):
    db = DraftDatabase(str(tmp_path / "drafts.db"))
    first_id = db.create_draft("first")
    second_id = db.create_draft("second")
    scheduled_at = "2030-01-01 10:00:00"

    assert db.schedule_draft(first_id, scheduled_at) is True
    assert db.schedule_draft(second_id, scheduled_at) is False
    assert db.get_draft(second_id)["status"] == "draft"
    assert db.get_draft(second_id)["scheduled_at"] is None


def test_manual_publish_failure_moves_claimed_draft_to_failed(tmp_path):
    db = DraftDatabase(str(tmp_path / "drafts.db"))
    draft_id = db.create_draft("will fail")
    messages: list[str] = []

    async def edit_callback_message(_query, text, **_kwargs):
        messages.append(text)

    async def fail_publish(*_args, **_kwargs):
        raise RuntimeError("telegram unavailable")

    deps = SimpleNamespace(
        can_publish=lambda status: status in {"draft", "approved", "scheduled"},
        status_guard_message=lambda _action, status: f"blocked: {status}",
        edit_callback_message=edit_callback_message,
        publish_to_channel=fail_publish,
    )
    context = SimpleNamespace(
        bot_data={
            "settings": SimpleNamespace(
                channel_id="-100123",
                custom_emoji_map={},
                custom_emoji_aliases={},
            ),
            "db": db,
        },
        bot=object(),
    )
    update = SimpleNamespace(callback_query=object())

    with pytest.raises(RuntimeError, match="telegram unavailable"):
        asyncio.run(
            handle_draft_moderation_callback(
                update,
                context,
                "publish",
                draft_id,
                None,
                deps,
            )
        )

    draft = db.get_draft(draft_id)
    assert draft["status"] == "failed"
    assert draft["publish_error"] == "RuntimeError"
    assert messages == []
