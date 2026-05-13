import ast
import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from bot.database import DraftDatabase
from bot.writer import GenerationResult
from bot.sources import TopicItem, _with_scoring
import bot.handlers as handlers
from bot.handlers import (
    _build_media_preview_caption,
    _build_moderation_text,
    _failed_drafts_keyboard,
    _render_failed_drafts_text,
    _send_moderation_preview,
    _collect_result_keyboard,
    _collect_topics_with_stats,
    _moderation_keyboard,
    _parse_callback_data,
    _render_cleanup_preview_text,
    _render_collect_text,
    _render_topics_hub_text,
    _rewrite_action_config,
    _topic_actions_keyboard,
    _topics_hub_keyboard,
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
    inserted[0].title_ru = "Свежий AI-инструмент для субтитров"
    inserted[0].summary_ru = "Инструмент помогает быстрее подготовить подписи к ролику."
    summary = _render_collect_text(stats, all_items, inserted)
    assert "Старые: 1" in summary
    assert "Без даты: 1" in summary
    assert "Объединено с похожими: 1" in summary
    assert "Свежий AI-инструмент" in summary
    assert "О чем:" in summary
    assert "Fresh AI tool app for video captions" not in summary
    assert "Время:" in summary
    assert "Обогащено AI:" in summary

    collect_texts = _keyboard_texts(_collect_result_keyboard())
    assert "🧠 Открыть темы" in collect_texts
    assert "🔥 Горячие" in collect_texts
    assert "🆕 Лучшие новые" in collect_texts
    assert not any("Shorts" in text or "Reels" in text or "TikTok" in text or "Видео" in text for text in collect_texts)

    hub_texts = _keyboard_texts(_topics_hub_keyboard())
    assert "🔥 Горячие" in hub_texts
    assert "🆕 Новые" in hub_texts
    assert "🔄 Собрать темы" in hub_texts
    assert not any("Shorts" in text or "Reels" in text or "TikTok" in text or "Видео" in text for text in hub_texts)
    hub_summary = _render_topics_hub_text(db)
    assert "🧠 Темы" in hub_summary
    assert "Горячие:" in hub_summary
    assert "Новые:" in hub_summary

    calls: list[str] = []
    original = handlers._enrich_topic_metadata_if_available

    async def fake_enrich(item, settings, db):
        calls.append(item.url)
        item.title_ru = f"RU {len(calls)}"
        item.summary_ru = "Русское описание"
        item.angle_ru = "Русский угол"

    handlers._enrich_topic_metadata_if_available = fake_enrich
    try:
        db_limited = DraftDatabase(f"{tmp.name}/topics-limit.db")
        limit_items = [
            _with_scoring(TopicItem(f"OpenAI launches useful model update {idx}", f"https://example.com/limit-{idx}", "OpenAI blog", fresh_date, source_group="official_ai"))
            for idx in range(5)
        ]
        stats_limited, _items_limited, _inserted_limited = await _collect_topics_with_stats(
            db_limited,
            items=limit_items,
            settings=SimpleNamespace(max_topic_age_days=14, has_ai_provider=True, topic_ai_enrich_limit=2, topic_ai_translate_limit=2),
        )
        assert len(calls) == 2
        assert stats_limited.ai_enriched == 2
        assert stats_limited.ai_enrich_limit == 2

        calls.clear()
        db_zero = DraftDatabase(f"{tmp.name}/topics-zero.db")
        await _collect_topics_with_stats(
            db_zero,
            items=limit_items,
            settings=SimpleNamespace(max_topic_age_days=14, has_ai_provider=True, topic_ai_enrich_limit=0, topic_ai_translate_limit=8),
        )
        assert calls == []
    finally:
        handlers._enrich_topic_metadata_if_available = original
        tmp.cleanup()


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, object]] = []

    async def reply_text(self, text: str, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))
        return SimpleNamespace(edit_text=lambda *args, **kwargs: None)


async def _run_topics_menu_fallback_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topics-menu.db")
    fresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    item = _with_scoring(TopicItem("A modest AI helper for summarizing tabs", "https://example.com/menu", "Test", fresh_date, source_group="tools"))
    item.score = 60
    item.title_ru = "AI-помощник для кратких пересказов вкладок"
    item.summary_ru = "Новая утилита помогает быстро понять открытые страницы."
    await _collect_topics_with_stats(db, items=[item], settings=SimpleNamespace(max_topic_age_days=14, has_ai_provider=False, topic_ai_enrich_limit=0, topic_ai_translate_limit=0))
    message = _FakeMessage()
    context = SimpleNamespace(bot_data={"settings": SimpleNamespace(admin_id=123), "db": db}, bot=_FakeBot(), args=[])
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=message)
    await handlers.topics_menu_command(update, context)
    combined = "\n".join(text for text, _markup in message.replies)
    assert "Горячих тем пока нет, но есть свежие темы. Показываю лучшие новые." in combined
    assert "AI-помощник" in combined
    tmp.cleanup()


def _topic_settings(admin_id: int = 123):
    return SimpleNamespace(
        admin_id=admin_id,
        has_ai_provider=True,
        openrouter_api_key="",
        openrouter_app_name="simplify-ai-test",
        openrouter_site_url="",
        openai_api_key="test-key",
        model_draft="draft-model",
        model_polish="",
        post_max_chars=800,
        post_soft_chars=600,
        openrouter_input_cost_per_1m=0.0,
        openrouter_output_cost_per_1m=0.0,
        openai_input_cost_per_1m=0.0,
        openai_output_cost_per_1m=0.0,
        custom_emoji_aliases={},
    )


def _insert_topic(db: DraftDatabase, url: str = "https://www.reddit.com/r/LocalLLaMA/comments/test/topic/") -> int:
    unique_title = f"New AI repo v2.1 discussed on Reddit {url}"
    db.upsert_topic_candidate_with_reason(
        title=unique_title,
        url=url,
        source="Reddit LocalLLaMA",
        published_at="2026-05-13 10:00:00",
        category="tools",
        score=91,
        reason="High-signal AI tooling discussion",
        normalized_title=unique_title.lower(),
        source_group="reddit",
        title_ru="Новый AI-репозиторий v2.1 обсуждают на Reddit",
        summary_ru="В теме сохранено описание репозитория и зачем он может быть полезен.",
        angle_ru="Подать осторожно, потому что источник не прочитан напрямую.",
        original_description="Reddit users discuss a new AI repo v2.1 and possible use cases.",
        canonical_key=unique_title.lower(),
    )
    topic = db.find_topic_candidate_by_url(url)
    assert topic is not None
    return int(topic["id"])


async def _run_topic_metadata_fallback_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topic-fallback.db")
    settings = _topic_settings()
    context = SimpleNamespace(bot_data={"settings": settings, "db": db}, bot=_FakeBot(), user_data={})

    original_fetch = handlers._run_fetch_page_content_details
    original_metadata = handlers._run_generate_post_draft_from_topic_metadata
    original_page = handlers._run_generate_post_draft_from_page
    fetch_calls: list[str] = []
    metadata_calls: list[dict] = []
    page_calls: list[str] = []

    async def fake_fetch(url):
        fetch_calls.append(url)
        raise AssertionError("Reddit page fetch must be skipped")

    async def fake_page(*args, **kwargs):
        page_calls.append(str(kwargs.get("source_url") or ""))
        raise AssertionError("Reddit page generation must be skipped")

    async def fake_metadata(*args, **kwargs):
        metadata_calls.append(kwargs)
        return GenerationResult(
            content="[[EMOJI:screen_card]] Осторожный черновик по сохранённому описанию темы.\n\n[[EMOJI:link]] Детали: [[LINK:открыть обсуждение|https://www.reddit.com/r/LocalLLaMA/comments/test/topic/]]",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            model="draft-model",
        )

    handlers._run_fetch_page_content_details = fake_fetch
    handlers._run_generate_post_draft_from_page = fake_page
    handlers._run_generate_post_draft_from_topic_metadata = fake_metadata
    try:
        topic_id = _insert_topic(db)
        new_draft_id, note = await handlers._create_draft_from_topic(
            context=context, settings=settings, db=db, topic_id=topic_id
        )
    finally:
        handlers._run_fetch_page_content_details = original_fetch
        handlers._run_generate_post_draft_from_page = original_page
        handlers._run_generate_post_draft_from_topic_metadata = original_metadata

    assert new_draft_id is not None
    assert fetch_calls == []
    assert page_calls == []
    assert len(metadata_calls) == 1
    assert note is not None and "Источник не удалось прочитать напрямую" in note
    draft = db.get_draft(new_draft_id)
    assert draft is not None
    assert draft["source_url"] == "https://www.reddit.com/r/LocalLLaMA/comments/test/topic/"
    assert draft["source_image_url"] is None
    assert "Источник:" not in draft["content"]
    assert db.get_topic_candidate(topic_id)["status"] == "used"
    with db._connect() as conn:
        usage = conn.execute("SELECT operation, source_url, draft_id FROM ai_usage").fetchone()
    assert usage["operation"] == "generate_topic_metadata_fallback"
    assert usage["source_url"] == "https://www.reddit.com/r/LocalLLaMA/comments/test/topic/"
    assert int(usage["draft_id"]) == new_draft_id
    tmp.cleanup()


async def _run_topic_403_fallback_and_failure_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topic-403.db")
    settings = _topic_settings()
    context = SimpleNamespace(bot_data={"settings": settings, "db": db}, bot=_FakeBot(), user_data={})

    original_fetch = handlers._run_fetch_page_content_details
    original_metadata = handlers._run_generate_post_draft_from_topic_metadata
    fetch_calls: list[str] = []

    async def fake_fetch(url):
        fetch_calls.append(url)
        raise RuntimeError("403 Client Error: Blocked for url: " + url)

    async def fake_metadata(*args, **kwargs):
        return GenerationResult(
            content="[[EMOJI:web]] Черновик по метаданным без строки источника и без лишних фактов.",
            prompt_tokens=1,
            completion_tokens=2,
            total_tokens=3,
            model="draft-model",
        )

    handlers._run_fetch_page_content_details = fake_fetch
    handlers._run_generate_post_draft_from_topic_metadata = fake_metadata
    try:
        topic_id = _insert_topic(db, url="https://blocked.example.com/story")
        new_draft_id, note = await handlers._create_draft_from_topic(
            context=context, settings=settings, db=db, topic_id=topic_id
        )
        assert new_draft_id is not None
        assert fetch_calls == ["https://blocked.example.com/story"]
        assert note is not None and "Источник не удалось прочитать напрямую" in note
        assert db.get_draft(new_draft_id)["source_url"] == "https://blocked.example.com/story"
        assert db.get_topic_candidate(topic_id)["status"] == "used"

        async def failing_metadata(*args, **kwargs):
            raise RuntimeError("metadata fallback model failed")

        handlers._run_generate_post_draft_from_topic_metadata = failing_metadata
        db_fail = DraftDatabase(f"{tmp.name}/topic-403-fail.db")
        context_fail = SimpleNamespace(bot_data={"settings": settings, "db": db_fail}, bot=_FakeBot(), user_data={})
        failed_topic_id = _insert_topic(db_fail, url="https://blocked.example.com/fail")
        failed_draft_id, error = await handlers._create_draft_from_topic(
            context=context_fail, settings=settings, db=db_fail, topic_id=failed_topic_id
        )
        assert failed_draft_id is None
        assert error and "Не удалось создать черновик" in error
        assert db_fail.get_topic_candidate(failed_topic_id)["status"] == "new"

        async def empty_metadata(*args, **kwargs):
            return GenerationResult(content="   ", prompt_tokens=1, completion_tokens=0, total_tokens=1, model="draft-model")

        handlers._run_generate_post_draft_from_topic_metadata = empty_metadata
        db_empty = DraftDatabase(f"{tmp.name}/topic-reddit-empty.db")
        context_empty = SimpleNamespace(bot_data={"settings": settings, "db": db_empty}, bot=_FakeBot(), user_data={})
        reddit_topic_id = _insert_topic(db_empty, url="https://www.reddit.com/r/LocalLLaMA/comments/test/empty/")
        empty_draft_id, empty_error = await handlers._create_draft_from_topic(
            context=context_empty, settings=settings, db=db_empty, topic_id=reddit_topic_id
        )
        assert empty_draft_id is None
        assert empty_error == handlers.REDDIT_METADATA_EMPTY_REPLY_TEXT
        assert db_empty.get_topic_candidate(reddit_topic_id)["status"] == "new"
    finally:
        handlers._run_fetch_page_content_details = original_fetch
        handlers._run_generate_post_draft_from_topic_metadata = original_metadata
        tmp.cleanup()


async def _run_topic_callback_warning_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topic-callback.db")
    settings = _topic_settings()
    context = SimpleNamespace(bot_data={"settings": settings, "db": db}, bot=_FakeBot(), user_data={})
    topic_id = _insert_topic(db)

    original_create = handlers._create_draft_from_topic

    async def fake_create(**kwargs):
        return 77, handlers.TOPIC_METADATA_FALLBACK_NOTE

    handlers._create_draft_from_topic = fake_create
    try:
        query = _FakeCallbackQuery(f"topic_generate:{topic_id}", settings.admin_id)
        await handlers.moderation_callback(SimpleNamespace(callback_query=query), context)
    finally:
        handlers._create_draft_from_topic = original_create
        tmp.cleanup()

    assert query.edited_text is not None
    assert "Создан черновик #77" in query.edited_text
    assert "Источник не удалось прочитать напрямую" in query.edited_text
    assert "Проверь факты" in query.edited_text



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
    asyncio.run(_run_topics_menu_fallback_selftest())
    asyncio.run(_run_topic_metadata_fallback_selftest())
    asyncio.run(_run_topic_403_fallback_and_failure_selftest())
    asyncio.run(_run_topic_callback_warning_selftest())

    print("OK")


if __name__ == "__main__":
    run()
