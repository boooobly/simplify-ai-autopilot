from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from bot import handlers
from bot.database import DraftDatabase
from bot.publisher import run_scheduled_publishing
from bot.writer import GenerationResult, PageContent


class FakeTelegramBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=9000 + len(self.sent_messages))


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        admin_id=42,
        channel_id="-1004242",
        has_ai_provider=True,
        openrouter_api_key="openrouter-test-key",
        openai_api_key="openai-test-key",
        openrouter_app_name="Simplify AI Autopilot Test",
        openrouter_site_url="",
        model_draft="openrouter/draft-model",
        model_topic_enrich="openrouter/topic-model",
        model_polish="openrouter/polish-model",
        openai_model_draft="openai-draft-model",
        openai_model_topic_enrich="openai-topic-model",
        openai_model_polish="openai-polish-model",
        post_max_chars=1400,
        post_soft_chars=1100,
        openrouter_input_cost_per_1m=1.0,
        openrouter_output_cost_per_1m=1.0,
        openai_input_cost_per_1m=2.0,
        openai_output_cost_per_1m=3.0,
        custom_emoji_map={},
        custom_emoji_aliases={},
    )


def _insert_publishable_topic(db: DraftDatabase) -> int:
    source_url = "https://example.com/useful-ai-tool"
    db.upsert_topic_candidate_with_reason(
        title="Useful AI tool adds a practical daily workflow",
        url=source_url,
        source="Example AI",
        published_at="2026-07-14 08:00:00",
        category="tool",
        score=84,
        reason="Практичный AI-инструмент для широкой аудитории.",
        normalized_title="useful ai tool practical daily workflow",
        source_group="tools",
        title_ru="Полезный AI-инструмент для ежедневной работы",
        summary_ru="Инструмент упрощает повторяющуюся задачу и подходит новичкам.",
        angle_ru="Показать один понятный сценарий применения.",
        reason_ru="Практическая тема без корпоративного PR.",
        original_description="A practical AI tool for a repeatable daily workflow.",
        canonical_key="useful ai tool practical daily workflow",
    )
    topic = db.find_topic_candidate_by_url(source_url)
    assert topic is not None
    return int(topic["id"])


def test_topic_to_scheduled_publication_is_one_idempotent_daily_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = DraftDatabase(str(tmp_path / "daily-flow.sqlite3"))
    settings = _settings()
    bot = FakeTelegramBot()
    topic_id = _insert_publishable_topic(db)
    previewed_drafts: list[int] = []

    async def fake_fetch(source_url: str) -> PageContent:
        assert source_url == "https://example.com/useful-ai-tool"
        return PageContent(
            title="Useful AI tool",
            text=" ".join(["Verified article text about a practical AI workflow."] * 100),
            preview_image_url="https://example.com/preview.jpg",
        )

    async def fake_generate(*args, **kwargs) -> GenerationResult:
        assert kwargs["provider"] == "openrouter"
        assert kwargs["fallback"].provider == "openai"
        return GenerationResult(
            content=(
                "[[EMOJI:screen_card]] AI-инструмент упрощает ежедневную задачу\n\n"
                "Он помогает быстрее пройти повторяющийся рабочий сценарий без сложной настройки."
            ),
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="openai-draft-model",
            provider="openai",
        )

    async def fake_preview(_context, _admin_id, draft_id, *_args, **_kwargs) -> None:
        previewed_drafts.append(draft_id)

    monkeypatch.setattr(handlers, "_run_fetch_page_content_details", fake_fetch)
    monkeypatch.setattr(handlers, "_run_generate_post_draft_from_page", fake_generate)
    monkeypatch.setattr(handlers, "_send_moderation_preview", fake_preview)

    handler_context = SimpleNamespace(bot_data={"settings": settings, "db": db}, bot=bot)
    draft_id, error = asyncio.run(
        handlers._create_draft_from_topic(
            context=handler_context,
            settings=settings,
            db=db,
            topic_id=topic_id,
        )
    )

    assert error is None
    assert draft_id is not None
    assert previewed_drafts == [draft_id]
    assert db.get_topic_candidate(topic_id)["status"] == "used"
    draft = db.get_draft(draft_id)
    assert draft["status"] == "draft"
    assert draft["source_image_url"] == "https://example.com/preview.jpg"
    with db._connect() as conn:
        usage = dict(conn.execute("SELECT * FROM ai_usage WHERE draft_id = ?", (draft_id,)).fetchone())
    assert usage["provider"] == "openai"
    assert usage["model"] == "openai-draft-model"

    assert db.schedule_draft(draft_id, "2000-01-01 00:00:00") is True
    scheduler_context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"settings": settings, "db": db}),
        bot=bot,
    )
    asyncio.run(run_scheduled_publishing(scheduler_context))
    asyncio.run(run_scheduled_publishing(scheduler_context))

    published = db.get_draft(draft_id)
    assert published["status"] == "published"
    assert published["scheduled_at"] is None
    assert published["published_channel_id"] == settings.channel_id
    assert published["published_message_ids"] == "9001"
    assert db.get_due_scheduled_drafts() == []
    assert len(bot.sent_messages) == 1
    assert bot.sent_messages[0]["chat_id"] == settings.channel_id
    assert bot.sent_messages[0]["parse_mode"] == "HTML"
    assert "AI-инструмент упрощает ежедневную задачу" in bot.sent_messages[0]["text"]
