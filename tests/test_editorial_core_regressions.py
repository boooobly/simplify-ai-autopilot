import logging

from bot.database import DraftDatabase
from bot.topic_scoring import has_ai_relevance_signal, hybrid_topic_score, score_topic
from main import SecretRedactionFilter


def test_keyword_matching_does_not_treat_apple_as_app():
    score, category, reason = score_topic(
        "Apple sues OpenAI over alleged trade-secret theft",
        "Tech media",
        "https://example.com/apple-openai-lawsuit",
        "tech_media",
        description="A lawsuit about alleged hardware secrets.",
        published_at="2026-07-13 10:00:00",
    )

    assert category != "mobile"
    assert "приложение/расширение" not in reason
    assert score < 90


def test_keyword_matching_does_not_treat_government_as_event():
    _score, _category, reason = score_topic(
        "Our approach to government and national security partnerships",
        "OpenAI blog",
        "https://openai.com/news/government-partnerships",
        "official_ai",
        description="How an AI company approaches public-sector partnerships.",
        published_at="2026-07-13 10:00:00",
    )

    assert "вакансия/ивент" not in reason


def test_normal_ai_token_discussion_is_not_spam():
    score, _category, reason = score_topic(
        "New technique cuts LLM token usage in half",
        "AI Lab",
        "https://example.com/llm-token-efficiency",
        "tech_media",
        description="A practical inference optimization for language models.",
        published_at="2026-07-13 10:00:00",
    )

    assert "спам/крипта" not in reason
    assert score >= 50


def test_broad_non_ai_russian_tech_story_fails_relevance_gate():
    score, category, reason = score_topic(
        "Lenovo выпустила тонкий игровой мини-ПК ThinkCentre",
        "iXBT",
        "https://example.com/lenovo-mini-pc",
        "ru_tech",
        description="Новый компьютер получил процессор Ryzen и компактный корпус.",
        published_at="2026-07-13 10:00:00",
    )

    assert score < 50
    assert category == "other"
    assert "нет явного AI-сигнала" in reason


def test_official_customer_case_study_is_not_scored_like_product_launch():
    score, category, reason = score_topic(
        "How Deutsche Telekom is rewiring telecommunications with AI",
        "OpenAI blog",
        "https://openai.com/customer-stories/deutsche-telekom",
        "official_ai",
        description="The enterprise uses AI agents and automation across its customer operations.",
        published_at="2026-07-13 10:00:00",
    )
    assert score < 80
    assert category == "business"
    assert "корпоративный PR" in reason


def test_ai_relevance_uses_tokens_not_substrings():
    assert has_ai_relevance_signal("New LLM agent with MCP support")
    assert has_ai_relevance_signal("Новый ИИ-инструмент для работы с видео")
    assert not has_ai_relevance_signal("Daily train timetable and retail details")


def test_ai_editorial_score_can_rescue_or_reject_a_keyword_borderline():
    assert hybrid_topic_score(38, 92) >= 60
    assert hybrid_topic_score(88, 10) < 60


def test_secret_redaction_filter_redacts_propagated_record_content():
    record = logging.LogRecord(
        name="bot.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="request failed for /botsecret-token-12345 and key api-secret-98765",
        args=(),
        exc_info=None,
    )
    redactor = SecretRedactionFilter(["secret-token-12345", "api-secret-98765"])

    assert redactor.filter(record)
    assert "secret-token-12345" not in record.getMessage()
    assert "api-secret-98765" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_secret_redaction_filter_redacts_exception_traceback():
    try:
        raise RuntimeError("request to /botsecret-token-12345 failed")
    except RuntimeError as exc:
        record = logging.LogRecord(
            name="bot.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="provider error",
            args=(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    redactor = SecretRedactionFilter(["secret-token-12345"])
    assert redactor.filter(record)
    assert record.exc_info is None
    assert "secret-token-12345" not in (record.exc_text or "")
    assert "[REDACTED]" in (record.exc_text or "")


def test_story_merge_promotes_better_primary_source_and_resolves_related_url(tmp_path):
    db = DraftDatabase(str(tmp_path / "topics.db"))
    first_url = "https://blog.example.com/acme-agent"
    better_url = "https://acme.ai/news/agent-launch"

    assert db.upsert_topic_candidate_with_reason(
        title="Acme agent is coming soon",
        url=first_url,
        source="Secondary blog",
        published_at="2026-07-12 08:00:00",
        category="news",
        score=61,
        reason="secondary report",
        normalized_title="acme agent coming soon",
        source_group="tech_media",
        original_description="A secondary report about the Acme agent.",
        canonical_key="acme agent launch",
    ) == "inserted"

    assert db.upsert_topic_candidate_with_reason(
        title="Acme launches its new AI agent",
        url=better_url,
        source="Acme official",
        published_at="2026-07-13 09:00:00",
        category="agent",
        score=88,
        reason="official practical launch",
        normalized_title="acme ai agent",
        source_group="official_ai",
        original_description="The official launch with product details.",
        canonical_key="acme agent launch",
    ) == "merged_story"

    merged = db.find_topic_candidate_by_url(better_url)
    assert merged is not None
    assert merged["url"] == better_url
    assert merged["title"] == "Acme launches its new AI agent"
    assert merged["source"] == "Acme official"
    assert merged["source_group"] == "official_ai"
    assert merged["published_at"] == "2026-07-13 09:00:00"
    assert int(merged["deterministic_score"]) == 88
    assert first_url in (merged["related_urls"] or "")


def test_recollection_preserves_good_ai_metadata_and_reblends_score(tmp_path):
    db = DraftDatabase(str(tmp_path / "recollect.db"))
    url = "https://example.com/ai-tool"
    db.upsert_topic_candidate_with_reason(
        title="AI tool launch",
        url=url,
        source="Product feed",
        published_at="2026-07-13 09:00:00",
        category="tool",
        score=70,
        reason="deterministic",
        normalized_title="ai tool",
        source_group="tools",
    )
    topic = db.find_topic_candidate_by_url(url)
    db.force_update_topic_candidate_display_fields(
        int(topic["id"]),
        title_ru="Полезный AI-инструмент",
        summary_ru="Понятное описание после AI-проверки.",
        angle_ru="Показать практический сценарий.",
        reason_ru="AI подтвердил пользу темы.",
        score=81,
        ai_value_score=95,
        ai_value_reason_ru="сильная практическая польза",
        audience_fit_ru="подходит новичкам",
        metadata_source="ai_bulk",
    )

    assert db.upsert_topic_candidate_with_reason(
        title="AI tool launch",
        url=url,
        source="Product feed",
        published_at="2026-07-13 09:00:00",
        category="tool",
        score=60,
        reason="rescored deterministic",
        normalized_title="ai tool",
        source_group="tools",
        title_ru="Слабый fallback",
        summary_ru="Слабый fallback.",
        angle_ru="Слабый fallback.",
    ) == "existing_url"

    recollected = db.find_topic_candidate_by_url(url)
    assert recollected["title_ru"] == "Полезный AI-инструмент"
    assert int(recollected["deterministic_score"]) == 60
    assert int(recollected["score"]) == hybrid_topic_score(60, 95)
    assert int(recollected["ai_value_score"]) == 95
    assert recollected["metadata_source"] == "ai_bulk"
