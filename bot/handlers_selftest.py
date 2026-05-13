import ast
import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from bot.database import DraftDatabase
from bot.sources import TopicItem, _with_scoring
import bot.handlers as handlers
from bot.handlers import (
    _build_media_preview_caption,
    _build_moderation_text,
    _failed_drafts_keyboard,
    _render_failed_drafts_text,
    _send_moderation_preview,
    _collect_topics_with_stats,
    _moderation_keyboard,
    _parse_callback_data,
    _render_cleanup_preview_text,
    _render_collect_text,
    _rewrite_action_config,
    _topic_actions_keyboard,
)


def _keyboard_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _keyboard_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _moderation_callback_actions() -> set[str]:
    tree = ast.parse(inspect.getsource(handlers.moderation_callback))
    actions: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "action":
            continue
        if not any(isinstance(op, ast.Eq) for op in node.ops):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                actions.add(comparator.value)
    return actions


def _run_keyboard_selftest() -> None:
    topic_keyboard = _topic_actions_keyboard(7, "https://example.com/topic")
    topic_buttons = _keyboard_buttons(topic_keyboard)
    assert any(button.text == "🔗 Открыть источник" and button.url == "https://example.com/topic" for button in topic_buttons)
    assert "✍️ Создать черновик" in _keyboard_texts(topic_keyboard)
    assert "❌ Отклонить тему" in _keyboard_texts(topic_keyboard)
    assert not any("Shorts" in text or "Reels" in text or "TikTok" in text or "Видео" in text for text in _keyboard_texts(topic_keyboard))

    draft_keyboard = _moderation_keyboard(5, "draft", source_url="https://example.com/source")
    draft_buttons = _keyboard_buttons(draft_keyboard)
    draft_texts = _keyboard_texts(draft_keyboard)
    assert "📅 В ближайший слот" in draft_texts
    assert "🧹 Убрать воду" in draft_texts
    assert "📉 Сделать короче" in draft_texts
    assert "😐 Без рекламного тона" in draft_texts
    assert "♻️ Перегенерировать" in draft_texts
    assert "🖼 Прикрепить картинку источника" not in draft_texts
    assert any(button.text == "🔗 Открыть источник" and button.url == "https://example.com/source" for button in draft_buttons)
    manual_media_buttons = [button for button in draft_buttons if button.text == "📎 Прикрепить медиа"]
    assert len(manual_media_buttons) == 1
    manual_media_callback = manual_media_buttons[0].callback_data
    assert manual_media_callback == "attach_media_flow:5"
    manual_media_action, manual_media_draft_id, manual_media_slot = _parse_callback_data(manual_media_callback)
    assert manual_media_draft_id == 5
    assert manual_media_slot is None
    assert manual_media_action == "attach_media_flow"

    source_image_texts = _keyboard_texts(
        _moderation_keyboard(
            5,
            "draft",
            source_url="https://example.com/source",
            source_image_url="https://example.com/preview.jpg",
        )
    )
    assert "🖼 Прикрепить картинку источника" in source_image_texts

    media_texts = _keyboard_texts(
        _moderation_keyboard(
            5,
            "draft",
            has_media=True,
            source_url="https://example.com/source",
            source_image_url="https://example.com/preview.jpg",
        )
    )
    assert "🖼 Прикрепить картинку источника" not in media_texts

    approved_texts = _keyboard_texts(
        _moderation_keyboard(
            5,
            "approved",
            source_url="https://example.com/source",
            source_image_url="https://example.com/preview.jpg",
        )
    )
    assert "📅 В ближайший слот" in approved_texts
    assert "🧹 Убрать воду" in approved_texts
    assert "📉 Сделать короче" in approved_texts
    assert "😐 Без рекламного тона" in approved_texts
    assert "♻️ Перегенерировать" in approved_texts
    assert "🖼 Прикрепить картинку источника" in approved_texts

    for status in ["scheduled", "published", "rejected", "failed"]:
        texts = _keyboard_texts(
            _moderation_keyboard(
                5,
                status,
                source_url="https://example.com/source",
                source_image_url="https://example.com/preview.jpg",
            )
        )
        assert "📅 В ближайший слот" not in texts
        assert "🧹 Убрать воду" not in texts
        assert "📉 Сделать короче" not in texts
        assert "😐 Без рекламного тона" not in texts
        assert "♻️ Перегенерировать" not in texts
        assert "🖼 Прикрепить картинку источника" not in texts


    rewrite_expectations = {
        "rewrite_remove_fluff:5": ("rewrite_remove_fluff", "remove_fluff", "rewrite_remove_fluff", "🧹 Убираю воду из черновика #5..."),
        "rewrite_shorten:5": ("rewrite_shorten", "shorten", "rewrite_shorten", "📉 Сокращаю черновик #5..."),
        "rewrite_neutralize_ads:5": ("rewrite_neutralize_ads", "neutralize_ads", "rewrite_neutralize_ads", "😐 Убираю рекламный тон из черновика #5..."),
    }
    for callback, expected in rewrite_expectations.items():
        action, parsed_draft_id, slot = _parse_callback_data(callback)
        config = _rewrite_action_config(action)
        assert parsed_draft_id == 5
        assert slot is None
        assert action == expected[0]
        assert config["mode"] == expected[1]
        assert config["operation"] == expected[2]
        assert config["progress"].format(draft_id=5) == expected[3]

    rewrite_buttons = [button for button in draft_buttons if (button.callback_data or "").startswith("rewrite_")]
    assert {button.callback_data for button in rewrite_buttons} == set(rewrite_expectations)
    forbidden = ("Shorts", "Reels", "TikTok", "video", "Видео", "видео")
    assert not any(word in button.text for button in rewrite_buttons for word in forbidden)
    assert not any(word in (button.callback_data or "") for button in rewrite_buttons for word in forbidden)

    no_source_texts = _keyboard_texts(_moderation_keyboard(5, "draft"))
    assert "♻️ Перегенерировать" not in no_source_texts


class _FakeCallbackQuery:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(photo=None, video=None, animation=None, document=None, caption=None)
        self.answers: list[tuple[str | None, bool]] = []
        self.edited_text: str | None = None
        self.edited_reply_markup = None

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, reply_markup=None, link_preview_options=None) -> None:
        self.edited_text = text
        self.edited_reply_markup = reply_markup

    async def edit_message_caption(self, caption: str, reply_markup=None) -> None:
        self.edited_text = caption
        self.edited_reply_markup = reply_markup



def _run_cleanup_ui_selftest() -> None:
    counts = {
        "old_rejected_topics": 1,
        "old_used_topics": 2,
        "old_new_topics": 3,
        "old_rejected_drafts": 4,
        "old_failed_drafts": 5,
        "stale_draft_drafts": 6,
        "total": 21,
    }
    text = _render_cleanup_preview_text(counts)
    for phrase in [
        "Старые отклонённые темы",
        "Старые использованные темы",
        "Старые новые темы",
        "Старые отклонённые черновики",
        "Старые failed-черновики",
        "Залежавшиеся нетронутые draft-черновики",
        "Всего строк к удалению: 21",
        "ai_usage не удаляются",
    ]:
        assert phrase in text
    action, parsed_id, slot = _parse_callback_data("cleanup_confirm:0")
    assert (action, parsed_id, slot) == ("cleanup_confirm", 0, None)
    action, parsed_id, slot = _parse_callback_data("cleanup_cancel:0")
    assert (action, parsed_id, slot) == ("cleanup_cancel", 0, None)
    forbidden = ("Shorts", "Reels", "TikTok", "video", "Видео", "видео")
    assert not any(word in text for word in forbidden)
    keyboard = handlers._cleanup_keyboard()
    buttons = _keyboard_buttons(keyboard)
    assert [button.callback_data for button in buttons] == ["cleanup_confirm:0", "cleanup_cancel:0"]
    assert not any(word in button.text for button in buttons for word in forbidden)
    assert not any(word in (button.callback_data or "") for button in buttons for word in forbidden)


async def _run_cleanup_callback_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/cleanup-callback.db")
    with db._connect() as conn:
        conn.execute(
            """
            INSERT INTO topic_candidates (title, url, source, status, created_at, last_seen_at)
            VALUES ('old', 'https://cleanup-callback/old', 'selftest', 'rejected', '2000-01-01 00:00:00', '2000-01-01 00:00:00')
            """
        )
        conn.commit()
    settings = SimpleNamespace(admin_id=123, custom_emoji_aliases={})
    context = SimpleNamespace(
        bot_data={"settings": settings, "db": db},
        user_data={
            handlers.CLEANUP_PREVIEW_GENERATED_AT_KEY: handlers.datetime.now(timezone.utc).timestamp(),
            handlers.CLEANUP_PREVIEW_COUNTS_KEY: db.cleanup_preview(),
        },
    )
    query = _FakeCallbackQuery("cleanup_confirm:0", settings.admin_id)
    update = SimpleNamespace(callback_query=query)
    await handlers.moderation_callback(update, context)
    assert query.edited_text is not None
    assert "Очистка базы выполнена" in query.edited_text
    assert db.cleanup_preview()["total"] == 0
    assert handlers.CLEANUP_PREVIEW_COUNTS_KEY not in context.user_data

    context.user_data[handlers.CLEANUP_PREVIEW_COUNTS_KEY] = {"total": 0}
    context.user_data[handlers.CLEANUP_PREVIEW_GENERATED_AT_KEY] = handlers.datetime.now(timezone.utc).timestamp()
    cancel_query = _FakeCallbackQuery("cleanup_cancel:0", settings.admin_id)
    await handlers.moderation_callback(SimpleNamespace(callback_query=cancel_query), context)
    assert cancel_query.edited_text == "Очистка отменена."
    assert handlers.CLEANUP_PREVIEW_COUNTS_KEY not in context.user_data
    tmp.cleanup()

async def _run_moderation_media_attach_callback_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/moderation-media.db")
    draft_id = db.create_draft("Draft ready for manual media attach")
    keyboard = _moderation_keyboard(draft_id, "draft")
    media_buttons = [button for button in _keyboard_buttons(keyboard) if button.text == "📎 Прикрепить медиа"]
    assert len(media_buttons) == 1
    callback_data = media_buttons[0].callback_data
    action, parsed_draft_id, slot = _parse_callback_data(callback_data)
    assert action == "attach_media_flow"
    assert parsed_draft_id == draft_id
    assert slot is None

    settings = SimpleNamespace(admin_id=123, custom_emoji_aliases={})
    context = SimpleNamespace(bot_data={"settings": settings, "db": db}, user_data={"pending_edit_draft_id": draft_id})
    query = _FakeCallbackQuery(callback_data, settings.admin_id)
    update = SimpleNamespace(callback_query=query)

    await handlers.moderation_callback(update, context)

    assert handlers._get_pending_media(context) == draft_id
    assert "pending_edit_draft_id" not in context.user_data
    assert query.edited_text is not None
    assert f"Прикрепление медиа к черновику #{draft_id}" in query.edited_text
    reply_callbacks = [button.callback_data for button in _keyboard_buttons(query.edited_reply_markup)]
    assert f"attach_media_done:{draft_id}" in reply_callbacks
    assert f"attach_media_cancel:{draft_id}" in reply_callbacks
    tmp.cleanup()


async def _run_collect_stats_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topics.db")
    fresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    items = [
        _with_scoring(TopicItem("Fresh AI tool app for video captions", "https://example.com/fresh", "Test", fresh_date, source_group="tools")),
        _with_scoring(TopicItem("Old AI tool app for video captions", "https://example.com/old", "Test", old_date, source_group="tech_media")),
        _with_scoring(TopicItem("AI tool without date for prompts", "https://example.com/no-date", "Test", None, source_group="tech_media")),
        _with_scoring(TopicItem("OpenAI launches GPT-5.1 for ChatGPT", "https://example.com/model-a", "OpenAI blog", fresh_date, source_group="official_ai")),
        _with_scoring(TopicItem("OpenAI unveils GPT-5.1 with ChatGPT update", "https://example.com/model-b", "The Verge AI", fresh_date, source_group="tech_media")),
    ]
    stats, all_items, inserted = await _collect_topics_with_stats(
        db,
        items=items,
        settings=SimpleNamespace(max_topic_age_days=14, has_ai_provider=False),
    )
    assert stats.total == 5
    assert stats.stale == 1
    assert stats.missing_date == 1
    assert stats.new >= 1
    assert stats.merged_story == 1
    assert any(item.url == "https://example.com/fresh" for item in inserted)
    assert all_items == items
    summary = _render_collect_text(stats, all_items, inserted)
    tmp.cleanup()
    assert "Старые: 1" in summary
    assert "Без даты: 1" in summary
    assert "Объединено с похожими: 1" in summary


def run() -> None:
    aliases = {"claude": ("🤖", "5208880957280522189")}
    text = "Тест [[EMOJI:claude]]"

    moderation = _build_moderation_text(
        draft_id=1,
        content=text,
        source_url="https://example.com",
        custom_emoji_aliases=aliases,
    )
    assert "🤖" in moderation
    assert "Картинка источника: нет" in moderation
    moderation_with_image = _build_moderation_text(
        draft_id=1,
        content=text,
        source_url="https://example.com",
        source_image_url="https://example.com/preview.jpg",
        custom_emoji_aliases=aliases,
    )
    assert "Картинка источника: есть" in moderation_with_image

    caption = _build_media_preview_caption(
        draft_id=2,
        content=text,
        source_url="https://example.com",
        media_type="photo",
        custom_emoji_aliases=aliases,
    )
    assert "🤖" in caption
    assert "settings" not in _send_moderation_preview.__code__.co_names

    failed_drafts = [
        {
            "id": 42,
            "source_url": "https://example.com/news",
            "media_url": "abc",
            "media_type": "photo",
            "updated_at": "2026-05-05 10:00:00",
            "content": "Тестовый упавший черновик\nсо второй строкой",
        },
        {
            "id": 43,
            "source_url": "https://example.com/other",
            "media_url": "",
            "media_type": "",
            "updated_at": "2026-05-05 11:00:00",
            "content": "Второй упавший черновик",
        },
    ]
    failed_text = _render_failed_drafts_text(failed_drafts)
    assert "#42" in failed_text
    assert "#43" in failed_text
    assert "Можно восстановить: /restore_draft ID" in failed_text

    keyboard = _failed_drafts_keyboard(failed_drafts)
    assert len(keyboard.inline_keyboard) == 2
    first_row = keyboard.inline_keyboard[0]
    assert first_row[0].text == "Открыть #42"
    assert first_row[0].callback_data == "draft_info:42"
    assert first_row[1].text == "🔁 Восстановить #42"
    assert first_row[1].callback_data == "restore_draft:42"
    second_row = keyboard.inline_keyboard[1]
    assert second_row[0].text == "Открыть #43"
    assert second_row[0].callback_data == "draft_info:43"
    assert second_row[1].text == "🔁 Восстановить #43"
    assert second_row[1].callback_data == "restore_draft:43"

    _run_keyboard_selftest()
    _run_cleanup_ui_selftest()
    asyncio.run(_run_moderation_media_attach_callback_selftest())
    asyncio.run(_run_cleanup_callback_selftest())
    asyncio.run(_run_collect_stats_selftest())

    print("OK")


if __name__ == "__main__":
    run()
