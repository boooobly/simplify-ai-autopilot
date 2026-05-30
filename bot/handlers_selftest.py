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
import bot.moderation_handlers as moderation_handlers
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
    assert "🔁 Перевести заново" in _keyboard_texts(topic_keyboard)
    assert any(button.callback_data == "topic_reenrich:7" for button in topic_buttons)
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
        limit_titles = [
            "OpenAI launches useful ChatGPT voice update",
            "Anthropic releases Claude agent workflow tool",
            "Google launches Gemini image app",
            "Microsoft adds Copilot audio feature",
            "Perplexity releases new research assistant",
        ]
        limit_items = [
            _with_scoring(TopicItem(title, f"https://example.com/limit-{idx}", "OpenAI blog", fresh_date, source_group="official_ai"))
            for idx, title in enumerate(limit_titles)
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
        model_topic_enrich="topic-model",
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


async def _run_daily_plan_draft_routing_selftest() -> None:
    context = SimpleNamespace(user_data={})
    original_empty_slots = handlers._empty_slots_for_day
    original_select_topics = handlers._select_daily_plan_topics
    original_create = handlers._create_draft_from_topic
    created_topic_ids: list[int] = []

    async def fake_create(*, context, settings, db, topic_id):
        created_topic_ids.append(topic_id)
        return topic_id + 100, None

    handlers._empty_slots_for_day = lambda db, settings, day_offset: ["10:00", "14:00"]
    handlers._select_daily_plan_topics = lambda db, limit: [{"id": 11}, {"id": 12}]
    handlers._create_draft_from_topic = fake_create
    try:
        result = await handlers._generate_drafts_from_plan(
            context=context,
            settings=SimpleNamespace(),
            db=SimpleNamespace(),
            day_offset=0,
        )
    finally:
        handlers._empty_slots_for_day = original_empty_slots
        handlers._select_daily_plan_topics = original_select_topics
        handlers._create_draft_from_topic = original_create

    assert created_topic_ids == [11, 12]
    assert context.user_data["pending_plan_schedule_items"] == [
        {"slot": "10:00", "draft_id": 111, "topic_id": 11},
        {"slot": "14:00", "draft_id": 112, "topic_id": 12},
    ]
    assert "Создано: 2" in result


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



async def _run_topic_reenrich_callback_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topic-reenrich.db")
    settings = _topic_settings()
    context = SimpleNamespace(bot_data={"settings": settings, "db": db}, bot=_FakeBot(), user_data={})
    topic_id = _insert_topic(db, url="https://example.com/old-english")
    db.force_update_topic_candidate_display_fields(
        topic_id,
        title_ru="Old English fallback title",
        summary_ru="Old summary fallback",
        angle_ru="Old angle fallback",
        reason_ru="Old reason fallback",
    )
    models: list[str] = []
    calls: list[dict] = []
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        models.append(kwargs["model"])
        calls.append(kwargs)
        return GenerationResult(
            content=(
                "Новый русский заголовок темы\n"
                "Новая русская сводка темы.\n"
                "Новый русский ракурс для поста.\n"
                "Новая русская причина важности.\n"
                "ai_value_score: 82\n"
                "ai_value_reason_ru: полезная обновленная карточка\n"
                "audience_fit_ru: подходит аудитории канала"
            ),
            prompt_tokens=11,
            completion_tokens=22,
            total_tokens=33,
            model=kwargs["model"],
        )

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        query = _FakeCallbackQuery(f"topic_reenrich:{topic_id}", settings.admin_id)
        await handlers.moderation_callback(SimpleNamespace(callback_query=query), context)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich

    assert models == [settings.model_topic_enrich]
    assert calls[0]["title"].startswith("New AI repo v2.1 discussed")
    assert calls[0]["source"] == "Reddit LocalLLaMA"
    assert calls[0]["description"].startswith("Reddit users discuss")
    updated = db.get_topic_candidate(topic_id)
    assert updated["title_ru"] == "Новый русский заголовок темы"
    assert updated["summary_ru"] == "Новая русская сводка темы."
    assert updated["angle_ru"] == "Новый русский ракурс для поста."
    assert updated["reason_ru"].startswith("Новая русская причина важности")
    assert "AI-оценка" in updated["reason_ru"]
    assert query.edited_text is not None
    card_lines = query.edited_text.splitlines()
    assert card_lines[3] == "Новый русский заголовок темы"
    assert "Old English fallback title" not in query.edited_text
    original_lines = [line for line in card_lines if line.startswith("Оригинал:")]
    assert original_lines == ["Оригинал: New AI repo v2.1 discussed on Reddit https://example.com/old-english"]
    assert "New AI repo v2.1 discussed" not in query.edited_text.replace(original_lines[0], "")
    assert query.edited_reply_markup is not None
    assert "🔁 Перевести заново" in _keyboard_texts(query.edited_reply_markup)
    tmp.cleanup()


async def _run_topic_reenrich_parse_failure_error_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topic-reenrich-error.db")
    settings = _topic_settings()
    context = SimpleNamespace(bot_data={"settings": settings, "db": db}, bot=_FakeBot(), user_data={})
    topic_id = _insert_topic(db, url="https://example.com/parse-failure")
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return GenerationResult(content="title: Only English title\nsummary:", model=kwargs["model"])

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        query = _FakeCallbackQuery(f"topic_reenrich:{topic_id}", settings.admin_id)
        await handlers.moderation_callback(SimpleNamespace(callback_query=query), context)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich
        tmp.cleanup()

    assert query.edited_text == "Модель вернула ответ, но бот не смог разобрать формат."


async def _run_topic_reenrich_empty_error_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topic-reenrich-empty.db")
    settings = _topic_settings()
    context = SimpleNamespace(bot_data={"settings": settings, "db": db}, bot=_FakeBot(), user_data={})
    topic_id = _insert_topic(db, url="https://example.com/empty-response")
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return GenerationResult(content="   ", model=kwargs["model"])

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        query = _FakeCallbackQuery(f"topic_reenrich:{topic_id}", settings.admin_id)
        await handlers.moderation_callback(SimpleNamespace(callback_query=query), context)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich
        tmp.cleanup()

    assert query.edited_text == "Модель вернула пустой ответ."


async def _run_topic_reenrich_too_english_error_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/topic-reenrich-english.db")
    settings = _topic_settings()
    context = SimpleNamespace(bot_data={"settings": settings, "db": db}, bot=_FakeBot(), user_data={})
    topic_id = _insert_topic(db, url="https://example.com/english-response")
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return GenerationResult(
            content=(
                "Persistent memory for AI coding agents based on real-world benchmarks\n"
                "Русская сводка про память.\n"
                "Русский ракурс для канала.\n"
                "Русская причина важности.\n"
                "ai_value_score: 55\n"
                "ai_value_reason_ru: техническая тема\n"
                "audience_fit_ru: слабо подходит новичкам"
            ),
            model=kwargs["model"],
        )

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        query = _FakeCallbackQuery(f"topic_reenrich:{topic_id}", settings.admin_id)
        await handlers.moderation_callback(SimpleNamespace(callback_query=query), context)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich
        tmp.cleanup()

    assert query.edited_text == "Модель вернула слишком английский текст, перевод отклонён."


async def _run_topic_model_routing_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/models.db")
    item = _with_scoring(
        TopicItem(
            "LLMs-from-scratch - Implement a ChatGPT-like LLM in PyTorch from scratch, step by step",
            "https://github.com/rasbt/LLMs-from-scratch",
            "GitHub Trending AI",
            datetime.now(timezone.utc).isoformat(),
            source_group="community",
        )
    )
    db.upsert_topic_candidate_with_reason(
        item.title,
        item.url,
        item.source,
        item.published_at,
        item.category,
        item.score,
        item.reason,
        item.normalized_title,
        item.source_group,
        item.title_ru,
        item.summary_ru,
        item.angle_ru,
        item.reason_ru,
        item.original_description,
    )
    settings = SimpleNamespace(
        has_ai_provider=True,
        openrouter_api_key="or-key",
        openai_api_key=None,
        openrouter_app_name="Simplify AI Autopilot",
        openrouter_site_url=None,
        model_draft="draft-model",
        model_topic_enrich="topic-model",
        model_polish="polish-model",
        openrouter_input_cost_per_1m=0.0,
        openrouter_output_cost_per_1m=0.0,
        openai_input_cost_per_1m=0.0,
        openai_output_cost_per_1m=0.0,
    )
    models: list[str] = []
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        models.append(kwargs["model"])
        return GenerationResult(
            content=(
                "LLMs-from-scratch - пошаговая сборка ChatGPT-подобной модели на PyTorch\n"
                "Репозиторий показывает, как с нуля собрать ChatGPT-подобную LLM на PyTorch.\n"
                "Можно подать как полезный open-source проект для понимания устройства LLM.\n"
                "Важно как практичный учебный репозиторий.\n"
                "ai_value_score: 73\n"
                "ai_value_reason_ru: полезно для понимания LLM\n"
                "audience_fit_ru: подходит любознательной части аудитории"
            ),
            model=kwargs["model"],
        )

    original_translate = handlers._run_translate_topic_title_to_ru

    async def fake_translate(**kwargs):
        models.append(kwargs["model"])
        return GenerationResult(content="OpenAI выпускает полезное обновление модели", model=kwargs["model"])

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    handlers._run_translate_topic_title_to_ru = fake_translate
    try:
        await handlers._enrich_topic_metadata_if_available(item, settings, db)
        translate_item = _with_scoring(TopicItem("OpenAI launches useful model update", "https://example.com/translate-model", "OpenAI blog", datetime.now(timezone.utc).isoformat(), source_group="official_ai"))
        db.upsert_topic_candidate_with_reason(translate_item.title, translate_item.url, translate_item.source, translate_item.published_at, translate_item.category, translate_item.score, translate_item.reason, translate_item.normalized_title, translate_item.source_group, translate_item.title_ru, translate_item.summary_ru, translate_item.angle_ru, translate_item.reason_ru, translate_item.original_description)
        await handlers._translate_topic_title_if_available(translate_item, settings, db)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich
        handlers._run_translate_topic_title_to_ru = original_translate
        tmp.cleanup()

    assert models == ["topic-model", "topic-model"]
    assert models[0] != settings.model_draft
    assert item.title_ru.startswith("LLMs-from-scratch - пошаговая сборка")
    assert "PyTorch" in item.title_ru and "ChatGPT" in item.title_ru
    assert "Implement a ChatGPT-like LLM" not in item.title_ru

    draft_source = inspect.getsource(handlers._generate_topic_metadata_fallback_draft)
    assert "model=settings.model_draft" in draft_source
    callback_source = inspect.getsource(moderation_handlers.handle_draft_moderation_callback)
    assert "model=settings.model_polish" in callback_source
    assert "run_polish_post_draft" in callback_source
    assert "run_rewrite_post_draft" in callback_source


async def _run_weak_topic_metadata_overwrite_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/weak-topic.db")
    settings = _topic_settings()
    fresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    item = _with_scoring(
        TopicItem(
            "GitHub Trending: owner / agentmemory",
            "https://github.com/owner/agentmemory",
            "GitHub Trending AI",
            fresh_date,
            source_group="github",
            title_ru="agentmemory - #1 Persistent memory for AI coding AI-агенты based on real-world benchmarks",
            summary_ru="Репозиторий выглядит как проект про Persistent memory for AI coding agents based on real-world benchmarks.",
            angle_ru="Можно подать как GitHub-проект.",
            reason_ru="GitHub без автотопа",
            original_description="#1 Persistent memory for AI coding agents based on real-world benchmarks",
        )
    )
    db.upsert_topic_candidate_with_reason(
        item.title,
        item.url,
        item.source,
        item.published_at,
        item.category,
        item.score,
        item.reason,
        item.normalized_title,
        item.source_group,
        item.title_ru,
        item.summary_ru,
        item.angle_ru,
        item.reason_ru,
        item.original_description,
    )
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return GenerationResult(
            content=(
                "agentmemory - память для AI-агентов в кодинге\n"
                "Репозиторий предлагает persistent memory для AI-агентов и оценивает её на практических бенчмарках.\n"
                "Можно показать как пример инфраструктуры для более полезных coding agents.\n"
                "Важно из-за фокуса на памяти агентов и проверке на бенчмарках.\n"
                "ai_value_score: 70\n"
                "ai_value_reason_ru: понятный open-source пример про агентов\n"
                "audience_fit_ru: подойдет технической части аудитории"
            ),
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            model=kwargs["model"],
        )

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        await handlers._enrich_topic_metadata_if_available(item, settings, db)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich

    stored = db.find_topic_candidate_by_url(item.url)
    assert stored is not None
    assert stored["title_ru"] == "agentmemory - память для AI-агентов в кодинге"
    assert "Persistent memory for AI coding" not in stored["title_ru"]
    assert item.title_ru == stored["title_ru"]
    tmp.cleanup()


async def _run_good_topic_metadata_no_overwrite_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/good-topic.db")
    settings = _topic_settings()
    fresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    item = _with_scoring(
        TopicItem(
            "OpenAI launches a useful model update",
            "https://example.com/good-topic",
            "OpenAI blog",
            fresh_date,
            source_group="official_ai",
            title_ru="OpenAI представила полезное обновление модели",
            summary_ru="Обновление добавляет практичные возможности для пользователей и разработчиков.",
            angle_ru="Можно спокойно объяснить, кому это пригодится и что изменится.",
            reason_ru="Официальный AI-релиз с высоким сигналом.",
        )
    )
    db.upsert_topic_candidate_with_reason(
        item.title,
        item.url,
        item.source,
        item.published_at,
        item.category,
        item.score,
        item.reason,
        item.normalized_title,
        item.source_group,
        item.title_ru,
        item.summary_ru,
        item.angle_ru,
        item.reason_ru,
        item.original_description,
    )
    original_enrich = handlers._run_enrich_topic_metadata_ru
    calls = 0

    async def fake_enrich(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("Good Russian metadata should not be enriched again")

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        await handlers._enrich_topic_metadata_if_available(item, settings, db)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich

    stored = db.find_topic_candidate_by_url(item.url)
    assert stored is not None
    assert stored["title_ru"] == "OpenAI представила полезное обновление модели"
    assert calls == 0
    tmp.cleanup()


async def _run_github_metadata_ai_preferred_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/github-topic.db")
    settings = _topic_settings()
    fresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    item = _with_scoring(
        TopicItem(
            "GitHub Trending: owner / Personal_AI_Infrastructure",
            "https://github.com/owner/Personal_AI_Infrastructure",
            "GitHub Trending AI",
            fresh_date,
            source_group="github",
            title_ru="Personal_AI_Infrastructure - Agentic AI Infrastructure for magnifying HUMAN capabilities",
            summary_ru="Репозиторий выглядит как проект про Agentic AI Infrastructure for magnifying HUMAN capabilities.",
            angle_ru="Можно подать как GitHub-проект.",
            reason_ru="GitHub без автотопа",
            original_description="Agentic AI Infrastructure for magnifying HUMAN capabilities",
        )
    )
    db.upsert_topic_candidate_with_reason(
        item.title,
        item.url,
        item.source,
        item.published_at,
        item.category,
        item.score,
        item.reason,
        item.normalized_title,
        item.source_group,
        item.title_ru,
        item.summary_ru,
        item.angle_ru,
        item.reason_ru,
        item.original_description,
    )
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return GenerationResult(
            content=(
                "Personal_AI_Infrastructure - инфраструктура для персональных AI-агентов\n"
                "Репозиторий собирает компоненты для агентной AI-инфраструктуры вокруг задач пользователя.\n"
                "Можно обсудить как такие проекты пытаются превратить AI в личный рабочий слой.\n"
                "Важно как пример интереса к персональной AI-инфраструктуре.\n"
                "ai_value_score: 74\n"
                "ai_value_reason_ru: тема про персональных AI-агентов\n"
                "audience_fit_ru: подходит аудитории, если объяснить просто"
            ),
            prompt_tokens=7,
            completion_tokens=14,
            total_tokens=21,
            model=kwargs["model"],
        )

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        await handlers._enrich_topic_metadata_if_available(item, settings, db)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich

    stored = db.find_topic_candidate_by_url(item.url)
    assert stored is not None
    assert stored["title_ru"] == "Personal_AI_Infrastructure - инфраструктура для персональных AI-агентов"
    assert "Agentic AI Infrastructure" not in stored["title_ru"]
    tmp.cleanup()


async def _run_topic_enrichment_ai_score_updates_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/ai-score.db")
    settings = _topic_settings()
    item = TopicItem(
        "Useful AI note-taking tool launches",
        "https://example.com/ai-score",
        "Product Hunt",
        "2026-05-13 10:00:00",
        category="tool",
        score=70,
        reason="детерминированная причина",
        reason_ru="Хорошая тема: есть практическая польза.",
        normalized_title="useful ai note taking tool launches",
        source_group="tools",
    )
    db.upsert_topic_candidate_with_reason(item.title, item.url, item.source, item.published_at, item.category, item.score, item.reason, item.normalized_title, item.source_group, item.title_ru, item.summary_ru, item.angle_ru, item.reason_ru, item.original_description)
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return GenerationResult(
            content=(
                "title_ru: Полезный AI-инструмент для заметок\n"
                "summary_ru: Сервис помогает быстро превращать встречи в понятные заметки.\n"
                "angle_ru: Можно показать практический сценарий для обычных пользователей.\n"
                "reason_ru: Практичный запуск с понятной пользой.\n"
                "ai_value_score: 95\n"
                "ai_value_reason_ru: полезно для широкого круга читателей\n"
                "audience_fit_ru: хорошо подходит новичкам @simplify_ai"
            ),
            model=kwargs["model"],
        )

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        await handlers._enrich_topic_metadata_if_available(item, settings, db)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich

    stored = db.find_topic_candidate_by_url(item.url)
    assert stored is not None
    assert stored["score"] == 79
    assert stored["deterministic_score"] == 70
    assert "AI-оценка" in stored["reason_ru"]
    assert item.score == 79
    tmp.cleanup()


async def _run_topic_enrichment_invalid_ai_score_falls_back_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/no-ai-score.db")
    settings = _topic_settings()
    item = TopicItem(
        "Narrow AI kernel bindings update",
        "https://example.com/no-ai-score",
        "GitHub Trending AI",
        "2026-05-13 10:00:00",
        category="dev",
        score=85,
        reason="детерминированная причина",
        reason_ru="Хорошая тема: есть технический сигнал.",
        normalized_title="narrow ai kernel bindings update",
        source_group="github",
    )
    db.upsert_topic_candidate_with_reason(item.title, item.url, item.source, item.published_at, item.category, item.score, item.reason, item.normalized_title, item.source_group, item.title_ru, item.summary_ru, item.angle_ru, item.reason_ru, item.original_description)
    stored_before = db.find_topic_candidate_by_url(item.url)
    assert stored_before is not None
    db.force_update_topic_candidate_display_fields(
        int(stored_before["id"]),
        title_ru=item.title,
        summary_ru="Нужен ручной просмотр: старая слабая карточка.",
        angle_ru="проверь тему вручную",
        reason_ru="Старая AI-причина.",
        ai_value_score=77,
        ai_value_reason_ru="старая оценка",
        audience_fit_ru="старое соответствие",
    )
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return GenerationResult(
            content=(
                "title_ru: Узкое обновление AI kernel bindings\n"
                "summary_ru: Репозиторий обновляет низкоуровневые биндинги для AI-разработки.\n"
                "angle_ru: Лучше брать только если нужен технический разбор.\n"
                "reason_ru: Тема техническая и требует ручной проверки.\n"
                "ai_value_score: не число\n"
                "ai_value_reason_ru: слишком узко\n"
                "audience_fit_ru: слабо подходит новичкам"
            ),
            model=kwargs["model"],
        )

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        status = await handlers._enrich_topic_metadata_if_available(item, settings, db)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich

    stored = db.find_topic_candidate_by_url(item.url)
    assert status == "invalid_model_output"
    assert stored is not None
    assert stored["score"] == 85
    assert stored["ai_value_score"] is None
    assert stored["title_ru"].startswith("GitHub-репозиторий:")
    assert "AI-оценка" not in (stored["reason_ru"] or "")
    tmp.cleanup()


async def _run_topic_reenrich_ai_score_refresh_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/reenrich-ai-score.db")
    settings = _topic_settings()
    topic_id = _insert_topic(db, url="https://example.com/reenrich-ai-score")
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return GenerationResult(
            content=(
                "title_ru: Новый русский заголовок темы\n"
                "summary_ru: Новая русская сводка темы.\n"
                "angle_ru: Новый русский ракурс темы.\n"
                "reason_ru: Новая русская причина важности.\n"
                "ai_value_score: 20\n"
                "ai_value_reason_ru: слишком технически для новичков\n"
                "audience_fit_ru: подходит только небольшой части аудитории"
            ),
            model=kwargs["model"],
        )

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        updated, error = await handlers._reenrich_topic_candidate_display_metadata(topic_id, settings, db)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich

    assert error is None
    assert updated is not None
    assert updated["score"] == 76
    assert "AI-оценка" in updated["reason_ru"]
    assert "слишком технически" in updated["reason_ru"]
    tmp.cleanup()


def _run_topic_card_final_score_concise_selftest() -> None:
    topic = {
        "id": 42,
        "score": 79,
        "category": "tool",
        "title": "Useful AI note-taking tool launches",
        "title_ru": "Полезный AI-инструмент для заметок",
        "summary_ru": "Сервис помогает быстро превращать встречи в понятные заметки.",
        "angle_ru": "Можно показать практический сценарий для обычных пользователей.",
        "reason_ru": "Хорошая тема: есть практическая польза. AI-оценка: полезно для широкого круга читателей.",
        "source": "Product Hunt",
        "source_group": "tools",
        "url": "https://example.com/card",
    }
    card = handlers._topic_card_text(topic)
    assert "Тема #42 - 79" in card
    assert "Почему: Хорошая тема" in card
    assert "AI-оценка" in card
    assert len(card) < 700


async def _run_topic_enrichment_failure_fallback_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/fallback.db")
    item = _with_scoring(TopicItem("OpenAI launches a useful model update", "https://example.com/fallback", "OpenAI blog", datetime.now(timezone.utc).isoformat(), source_group="official_ai"))
    db.upsert_topic_candidate_with_reason(item.title, item.url, item.source, item.published_at, item.category, item.score, item.reason, item.normalized_title, item.source_group, item.title_ru, item.summary_ru, item.angle_ru, item.reason_ru, item.original_description)
    settings = SimpleNamespace(
        has_ai_provider=True,
        openrouter_api_key="or-key",
        openai_api_key=None,
        openrouter_app_name="Simplify AI Autopilot",
        openrouter_site_url=None,
        model_draft="draft-model",
        model_topic_enrich="topic-model",
        openrouter_input_cost_per_1m=0.0,
        openrouter_output_cost_per_1m=0.0,
        openai_input_cost_per_1m=0.0,
        openai_output_cost_per_1m=0.0,
    )
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return None

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        await handlers._enrich_topic_metadata_if_available(item, settings, db)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich
        tmp.cleanup()

    assert item.title_ru == "Новость от OpenAI blog: OpenAI launches a useful model update"
    assert "Источник OpenAI blog пишет про тему" in item.summary_ru
    assert "Нужна проверка деталей" in item.summary_ru
    assert handlers.TOPIC_ENRICH_FALLBACK_SUMMARY_RU not in item.summary_ru
    assert "практическом выводе" in item.angle_ru


def _run_topic_ai_candidate_selection_selftest() -> None:
    candidates = [
        SimpleNamespace(id=1, url="https://example.com/a", canonical_key="same", normalized_title="same", title="A", score=60),
        SimpleNamespace(id=2, url="https://example.com/b", canonical_key="same", normalized_title="same", title="B", score=95),
        SimpleNamespace(id=3, url="https://example.com/c", canonical_key="other", normalized_title="other", title="C", score=80),
    ]
    selected, skipped = handlers.select_topic_ai_enrichment_candidates(sorted(candidates, key=lambda item: item.score, reverse=True), 1)
    assert [item.url for item in selected] == ["https://example.com/b"]
    assert skipped == 1



def _run_topic_preview_candidate_selection_selftest() -> None:
    inserted = [
        SimpleNamespace(id=i, url=f"https://example.com/new-{i}", canonical_key=f"new-{i}", normalized_title=f"new {i}", title=f"New topic {i}", source="Source", source_group="official_ai", category="news", score=95 - i)
        for i in range(6)
    ]
    lively = [
        SimpleNamespace(id=20 + i, url=f"https://example.com/tool-{i}", canonical_key=f"tool-{i}", normalized_title=f"tool {i}", title=f"Tool topic {i}", source="Product Hunt", source_group="tools", category="tool", score=88 - i)
        for i in range(5)
    ]
    raw_only = SimpleNamespace(id=99, url="https://example.com/raw", canonical_key="raw", normalized_title="raw", title="Raw unshown topic", source="Raw", source_group="tech_media", category="news", score=100)
    preview = handlers._collect_preview_candidates(inserted, [*inserted, *lively, raw_only])
    selected, skipped = handlers.select_topic_ai_enrichment_candidates(preview, 8)
    selected_urls = {item.url for item in selected}
    assert raw_only.url not in selected_urls
    assert all(item.url in selected_urls for item in inserted[:5])
    assert skipped == max(0, len(preview) - 8)


def _run_topic_preview_ai_display_selftest() -> None:
    topic = {
        "id": 1,
        "title": "Original English fallback title",
        "url": "https://example.com/ai",
        "source": "Test",
        "source_group": "tech_media",
        "category": "model",
        "score": 91,
        "title_ru": "Claude получил новый инструмент для агентов",
        "summary_ru": "Anthropic добавила понятную функцию для работы с группой подагентов.",
        "angle_ru": "Объяснить простыми словами, почему агенты становятся рабочими помощниками.",
        "reason_ru": "Высокая ценность для аудитории.",
        "ai_value_score": 94,
        "content_format": "news",
    }
    normal = handlers._render_collect_topic_line(topic, debug=False)
    debug = handlers._render_collect_topic_line(topic, debug=True)
    assert "Claude получил новый инструмент" in normal[0]
    assert "О чем: Anthropic добавила" in "\n".join(normal)
    assert "Идея: Объяснить" in "\n".join(normal)
    assert "[AI]" not in "\n".join(normal)
    assert "[AI]" in "\n".join(debug)


def _run_collect_debug_preview_coverage_selftest() -> None:
    stats = handlers.TopicCollectStats(total=1, new=1, ai_enriched=1, ai_enrich_limit=8)
    ai_item = SimpleNamespace(
        id=1, title="AI source title", url="https://example.com/ai", source="OpenAI blog", published_at=None,
        category="model", score=90, reason="", normalized_title="ai", source_group="official_ai",
        title_ru="Русский AI-заголовок", summary_ru="Понятное русское описание темы.", angle_ru="Сделать короткий пост о пользе.",
        reason_ru="AI оценил тему высоко.", ai_value_score=90, content_format="news",
    )
    text = handlers._render_collect_text(stats, [ai_item], [ai_item], debug=True)
    assert "Покрытие preview: [AI] 1, [fallback] 0" in text
    assert "[AI]" in text
    fallback_stats = handlers.TopicCollectStats(total=1, new=1, ai_enriched=1, ai_enrich_limit=8)
    fallback_item = SimpleNamespace(
        id=2, title="Fallback title", url="https://example.com/fallback", source="Source", published_at=None,
        category="news", score=80, reason="", normalized_title="fallback", source_group="official_ai",
        title_ru="Новость от Source: Fallback title", summary_ru="Источник пишет про тему.", angle_ru="Проверить вручную.", reason_ru="",
    )
    fallback_text = handlers._render_collect_text(fallback_stats, [fallback_item], [fallback_item], debug=True)
    assert "Покрытие preview: [AI] 0, [fallback] 1" in fallback_text
    assert "AI enriched topics are not present in preview list. Check enrichment candidate selection." in fallback_text

async def _run_ai_topic_card_enrichment_success_selftest() -> None:
    tmp = TemporaryDirectory()
    db = DraftDatabase(f"{tmp.name}/success.db")
    settings = _topic_settings()
    item = TopicItem(
        "These new iOS 27 renders hint at Siri’s big redesign",
        "https://example.com/siri",
        "The Verge AI",
        datetime.now(timezone.utc).isoformat(),
        category="news",
        score=82,
        reason="fresh AI news",
        reason_ru="Сильная новость про AI-интерфейсы.",
        normalized_title="ios siri redesign",
        source_group="tech_media",
        original_description="Apple's long-awaited Siri overhaul gets new renders.",
    )
    db.upsert_topic_candidate_with_reason(item.title, item.url, item.source, item.published_at, item.category, item.score, item.reason, item.normalized_title, item.source_group, item.title_ru, item.summary_ru, item.angle_ru, item.reason_ru, item.original_description)
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        return GenerationResult(
            content=(
                '{"title_ru":"Новость от The Verge AI: рендеры нового интерфейса Siri в iOS 27",'
                '"summary_ru":"Apple может готовить крупный редизайн Siri. Ассистент станет больше похож на отдельный AI-чат внутри iPhone, а не просто голосовую команду.",'
                '"angle_ru":"Коротко объяснить, как Apple пытается догнать ChatGPT и Gemini внутри iPhone.",'
                '"reason_ru":"Понятная новость про потребительский AI.",'
                '"ai_value_score":88,"ai_value_reason_ru":"широкая тема про iPhone и AI","audience_fit_ru":"подходит широкой аудитории","content_format":"news"}'
            ),
            model=kwargs["model"],
        )

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        status = await handlers._enrich_topic_metadata_if_available(item, settings, db)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich
    stored = db.find_topic_candidate_by_url(item.url)
    assert status == "enriched"
    assert stored is not None
    assert "Apple может готовить" in stored["summary_ru"]
    assert "Apple's long-awaited" not in stored["summary_ru"]
    tmp.cleanup()


async def _run_collect_ai_diagnostics_selftest() -> None:
    tmp = TemporaryDirectory()
    fresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    item = _with_scoring(TopicItem("Clipline", "https://example.com/clipline", "Product Hunt", fresh_date, source_group="tools", original_description="AI Video Cutter for viral Shorts, Reels, TikTok"))
    item.title = "Clipline AI video cutter"
    item.normalized_title = "clipline ai video cutter"
    stats_no_provider, _, inserted_no_provider = await _collect_topics_with_stats(
        DraftDatabase(f"{tmp.name}/no-provider.db"),
        items=[item],
        settings=SimpleNamespace(max_topic_age_days=14, has_ai_provider=False, topic_ai_enrich_limit=8),
    )
    text_no_provider = _render_collect_text(stats_no_provider, [item], inserted_no_provider, debug=True)
    assert stats_no_provider.ai_enrichment_skipped_no_provider == 1
    assert "не настроен провайдер" in text_no_provider
    assert "ai_enrichment_attempted=0" in text_no_provider
    assert "ai_invalid_json=0" in text_no_provider
    assert "ai_invalid_fields=0" in text_no_provider
    assert "ai_provider_errors=0" in text_no_provider
    assert "ai_json_mode_unsupported=0" in text_no_provider
    assert "deterministic_fallback_used=1" in text_no_provider
    assert "ai_enrichment_skipped_no_provider=1" in text_no_provider

    item2 = _with_scoring(TopicItem("Useful AI product launch", "https://example.com/limit-zero", "Product Hunt", fresh_date, source_group="tools"))
    stats_limit, _, inserted_limit = await _collect_topics_with_stats(
        DraftDatabase(f"{tmp.name}/limit-zero.db"),
        items=[item2],
        settings=SimpleNamespace(max_topic_age_days=14, has_ai_provider=True, topic_ai_enrich_limit=0),
    )
    text_limit = _render_collect_text(stats_limit, [item2], inserted_limit, debug=True)
    assert stats_limit.ai_enrichment_skipped_limit == 1
    assert "TOPIC_AI_ENRICH_LIMIT=0" in text_limit
    assert "ai_enrichment_skipped_limit=1" in text_limit
    tmp.cleanup()


async def _run_collect_partial_ai_enrichment_selftest() -> None:
    tmp = TemporaryDirectory()
    fresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    settings = _topic_settings()
    settings.topic_ai_enrich_limit = 8
    items = [
        _with_scoring(TopicItem("Broken AI response topic", "https://example.com/broken-ai", "Test Source", fresh_date, source_group="tools", original_description="Useful AI tool")),
        _with_scoring(TopicItem("Valid AI response topic", "https://example.com/valid-ai", "Test Source", fresh_date, source_group="tools", original_description="Useful AI tool")),
    ]
    for item in items:
        item.score = 80
    original_enrich = handlers._run_enrich_topic_metadata_ru

    async def fake_enrich(**kwargs):
        diagnostics = kwargs.get("diagnostics")
        if "Broken" in kwargs["title"]:
            if diagnostics is not None:
                diagnostics["ai_invalid_json"] = diagnostics.get("ai_invalid_json", 0) + 1
            return None
        return GenerationResult(
            content=(
                '{"title_ru":"Валидная AI-карточка темы","summary_ru":"Модель вернула понятное русское описание для второй темы, поэтому пакет не должен падать целиком.",'
                '"angle_ru":"Показать, что одна ошибка не ломает остальные карточки.","reason_ru":"Важная проверка надежности обогащения.",'
                '"ai_value_score":82,"ai_value_reason_ru":"полезная тема","audience_fit_ru":"подходит аудитории"}'
            ),
            model=kwargs["model"],
        )

    handlers._run_enrich_topic_metadata_ru = fake_enrich
    try:
        stats, _, _ = await _collect_topics_with_stats(DraftDatabase(f"{tmp.name}/partial.db"), items=items, settings=settings)
    finally:
        handlers._run_enrich_topic_metadata_ru = original_enrich
        tmp.cleanup()

    assert stats.ai_enrichment_attempted == 2
    assert stats.ai_enriched == 1
    assert stats.ai_enrichment_invalid_json == 1
    assert stats.ai_enrichment_failed == 1


def _run_english_fallback_readable_selftest() -> None:
    from bot.topic_display import build_deterministic_topic_metadata_ru

    long_description = "Anthropic releases Claude Opus 4.8, which beats GPT-5.5 and Gemini 3.1 Pro in most benchmarks and includes many technical details that should not be dumped directly into the Russian admin card."
    metadata = build_deterministic_topic_metadata_ru(
        TopicItem(
            "Anthropic releases Claude Opus 4.8, which beats GPT-5.5 and Gemini 3.1 Pro in most benchmarks",
            "https://example.com/claude",
            "The Decoder",
            datetime.now(timezone.utc).isoformat(),
            category="model",
            score=80,
            source_group="tech_media",
            original_description=long_description,
        )
    )
    assert "Нужна проверка деталей" in metadata["summary_ru"]
    assert "which beats GPT-5.5 and Gemini 3.1 Pro in most benchmarks and includes" not in metadata["summary_ru"]
    assert "сравнение AI-моделей" in metadata["summary_ru"]


def _run_source_fallback_examples_selftest() -> None:
    from bot.topic_display import build_deterministic_topic_metadata_ru

    product = build_deterministic_topic_metadata_ru(TopicItem("Clipline", "https://example.com/clipline", "Product Hunt", source_group="tools", category="tool", original_description="AI Video Cutter for viral Shorts, Reels, TikTok"))
    github = build_deterministic_topic_metadata_ru(TopicItem("anthropics / claude-code", "https://github.com/anthropics/claude-code", "GitHub Trending AI", source_group="github", category="dev", original_description="Claude Code is an agentic coding tool that lives in your terminal"))
    rss = build_deterministic_topic_metadata_ru(TopicItem("These new iOS 27 renders hint at Siri’s big redesign", "https://example.com/siri", "The Verge AI", source_group="tech_media", category="news", original_description="Apple's long-awaited Siri overhaul"))
    telegram = build_deterministic_topic_metadata_ru(TopicItem("Gemini app update discussion", "https://t.me/example/1", "@ai_news", source_group="telegram", category="news", original_description="Users discuss Gemini app update and new image tools"))

    assert "короткие видео" in product["summary_ru"]
    assert "AI-агента для программирования" in github["summary_ru"]
    assert "обновления Apple" in rss["summary_ru"]
    assert "Нужна проверка деталей" in telegram["summary_ru"]

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
    asyncio.run(_run_daily_plan_draft_routing_selftest())
    asyncio.run(_run_topic_reenrich_callback_selftest())
    asyncio.run(_run_topic_reenrich_parse_failure_error_selftest())
    asyncio.run(_run_topic_reenrich_empty_error_selftest())
    asyncio.run(_run_topic_reenrich_too_english_error_selftest())
    asyncio.run(_run_topic_model_routing_selftest())
    asyncio.run(_run_weak_topic_metadata_overwrite_selftest())
    asyncio.run(_run_good_topic_metadata_no_overwrite_selftest())
    asyncio.run(_run_github_metadata_ai_preferred_selftest())
    asyncio.run(_run_topic_enrichment_ai_score_updates_selftest())
    asyncio.run(_run_topic_enrichment_invalid_ai_score_falls_back_selftest())
    asyncio.run(_run_topic_reenrich_ai_score_refresh_selftest())
    _run_topic_card_final_score_concise_selftest()
    asyncio.run(_run_topic_enrichment_failure_fallback_selftest())
    _run_topic_ai_candidate_selection_selftest()
    _run_topic_preview_candidate_selection_selftest()
    _run_topic_preview_ai_display_selftest()
    _run_collect_debug_preview_coverage_selftest()
    asyncio.run(_run_ai_topic_card_enrichment_success_selftest())
    asyncio.run(_run_collect_ai_diagnostics_selftest())
    asyncio.run(_run_collect_partial_ai_enrichment_selftest())
    _run_english_fallback_readable_selftest()
    _run_source_fallback_examples_selftest()
    asyncio.run(_run_topic_403_fallback_and_failure_selftest())
    asyncio.run(_run_topic_callback_warning_selftest())

    print("OK")


if __name__ == "__main__":
    run()
