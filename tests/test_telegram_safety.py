import asyncio
from types import SimpleNamespace

from telegram.error import BadRequest

from bot import handlers
from bot.telegram_safety import TELEGRAM_SAFE_TEXT_LIMIT, split_telegram_text, truncate_telegram_text
from bot.topic_handlers import collect_debug_command


def _long_topic(index: int):
    return SimpleNamespace(
        id=index,
        title=f"Topic {index}",
        title_ru="Очень длинный заголовок " + "з" * 400,
        summary_ru="Подробное описание " + "о" * 700,
        angle_ru="Редакционная идея " + "и" * 500,
        reason_ru="",
        url=f"https://example.com/{index}",
        canonical_key=f"topic-{index}",
        normalized_title=f"topic {index}",
        source="Test",
        source_group="tools",
        category="tool",
        score=90 - index,
        content_format="tool",
    )


def test_collect_summary_and_topic_preview_stay_under_telegram_limit():
    topics = [_long_topic(index) for index in range(10)]
    handlers._collect_preview_candidates(topics, topics)
    stats = handlers.TopicCollectStats(total=10, new=10, ai_enrichment_attempted=3, ai_enriched=2)

    text = handlers._render_collect_text(stats, topics, topics)

    assert len(text) <= TELEGRAM_SAFE_TEXT_LIMIT
    assert text.count("  О чем:") == 6
    assert "о" * 700 not in text
    assert "и" * 500 not in text


def test_split_and_truncate_telegram_text_are_safe():
    text = ("section\n" + "x" * 1000 + "\n\n") * 10

    parts = split_telegram_text(text)

    assert len(parts) > 1
    assert all(len(part) <= TELEGRAM_SAFE_TEXT_LIMIT for part in parts)
    assert len(truncate_telegram_text(text)) <= TELEGRAM_SAFE_TEXT_LIMIT


def test_edit_callback_message_recovers_from_message_too_long():
    class _Query:
        def __init__(self):
            self.calls = []
            self.message = SimpleNamespace(photo=None, video=None, animation=None, document=None, caption=None)

        async def edit_message_text(self, text, **kwargs):
            self.calls.append(text)
            if len(self.calls) == 1:
                raise BadRequest("Message_too_long")

        async def answer(self, text=None, show_alert=False):
            self.answer = (text, show_alert)

    query = _Query()

    asyncio.run(handlers._edit_callback_message(query, "x" * 6000))

    assert len(query.calls) == 2
    assert len(query.calls[0]) <= TELEGRAM_SAFE_TEXT_LIMIT
    assert "слишком длинный" in query.calls[1]


def test_collect_debug_splits_long_output(monkeypatch):
    async def _fake_collect(**kwargs):
        return [], []

    async def _fake_stats(*args, **kwargs):
        return SimpleNamespace(source_seconds=0.0, store_seconds=0.0, ai_seconds=0.0, total_seconds=0.0, stale=0), [], []

    class _Progress:
        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append(text)

    class _Message:
        def __init__(self):
            self.progress = _Progress()
            self.replies = []

        async def reply_text(self, text, **kwargs):
            self.replies.append(text)
            return self.progress

    monkeypatch.setattr(handlers, "_run_collect_topics_with_diagnostics", _fake_collect)
    monkeypatch.setattr(handlers, "_collect_topics_with_stats", _fake_stats)
    monkeypatch.setattr(handlers, "_render_collect_text", lambda *args, **kwargs: "debug\n" + "x" * 9000)
    monkeypatch.setattr(handlers, "_collect_result_keyboard", lambda: object())
    message = _Message()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)
    context = SimpleNamespace(bot_data={"settings": SimpleNamespace(admin_id=1, max_topic_age_days=14), "db": object()})

    asyncio.run(collect_debug_command(update, context))

    delivered = [*message.progress.edits, *message.replies[1:]]
    assert len(delivered) > 1
    assert all(len(part) <= TELEGRAM_SAFE_TEXT_LIMIT for part in delivered)
