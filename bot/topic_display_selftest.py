from __future__ import annotations

from bot.handlers import _render_plan_text, _topic_card_text
from bot.topic_display import MANUAL_REVIEW_NOTE_RU, build_deterministic_topic_metadata_ru, is_weak_topic_metadata, topic_compact_preview_ru, topic_display_reason, topic_display_title


def run() -> None:
    topic = {
        "id": 7,
        "title": "OpenAI releases a new model",
        "title_ru": "OpenAI выпустила новую модель",
        "summary_ru": "Коротко объясняет новую возможность модели.",
        "angle_ru": "Можно показать, что изменится для обычных пользователей.",
        "reason": "model",
        "reason_ru": "Тема набрала высокий вес, потому что это официальный AI-релиз.",
        "source": "OpenAI blog",
        "source_group": "official_ai",
        "category": "model",
        "score": 91,
        "url": "https://example.com/openai-model",
        "related_sources": "OpenAI blog\nThe Verge AI\nTechCrunch AI\nReddit r/OpenAI",
        "related_urls": "https://example.com/openai-model\nhttps://example.com/verge\nhttps://example.com/tc\nhttps://reddit.com/r/openai/1",
        "related_count": 4,
    }
    card = _topic_card_text(topic)
    assert "OpenAI выпустила новую модель" in card
    assert "О чем:" in card
    assert "Коротко объясняет новую возможность модели." in card
    assert "Идея поста:" in card
    assert "Можно показать, что изменится" in card
    assert "Оригинал: OpenAI releases a new model" in card
    assert "Почему: Тема набрала высокий вес" in card
    assert "URL: https://example.com/openai-model" in card
    assert "Повторы: еще 3 источника" in card
    assert "Также встречалось: The Verge AI, TechCrunch AI, Reddit r/OpenAI" in card

    fallback_topic = {
        "id": 8,
        "title": "English fallback title",
        "source": "TechCrunch AI",
        "source_group": "tech_media",
        "category": "news",
        "score": 70,
        "reason": "новость/релиз",
        "url": "https://example.com/fallback",
    }
    fallback_card = _topic_card_text(fallback_topic)
    assert topic_display_title(fallback_topic) == "Тема требует ручной проверки"
    assert topic_display_reason(fallback_topic) == "Категория: AI-новость; скоринг 70/100. Сигналы скоринга: новость/релиз."
    assert "Тема требует ручной проверки" in fallback_card
    assert "О чем:" in fallback_card
    assert MANUAL_REVIEW_NOTE_RU in fallback_card
    assert "Оригинальный заголовок: English fallback title" in fallback_card
    assert 'Открыть источник или нажать "Понять тему через AI"' in fallback_card
    assert "Идея поста:" in fallback_card
    assert 'Открыть источник или нажать "Понять тему через AI"' in fallback_card
    assert "Оригинал: English fallback title" in fallback_card

    rss_fallback = build_deterministic_topic_metadata_ru({
        "title": "Anthropic updates Claude Opus 4.8 and workflow tool",
        "source": "Anthropic news",
        "source_group": "official_ai",
        "category": "model",
        "score": 88,
        "reason": "official AI release",
        "original_description": "Claude Opus 4.8 adds better coding performance and a workflow tool for teams.",
        "url": "https://www.anthropic.com/news/claude-opus-4-8",
    })
    assert rss_fallback["title_ru"].startswith("Новость от Anthropic news")
    assert "Источник Anthropic news пишет про тему" in rss_fallback["summary_ru"]
    assert "Claude Opus 4.8" in rss_fallback["summary_ru"]
    assert "Нужна проверка деталей" in rss_fallback["summary_ru"]
    assert "official AI release" in rss_fallback["reason_ru"]

    github_metadata = build_deterministic_topic_metadata_ru({
        "title": "GitHub Trending: owner / useful-ai-tool",
        "source": "GitHub Trending AI",
        "source_group": "github",
        "category": "dev",
        "score": 82,
        "reason": "github stars; AI tool",
        "original_description": "An AI agent for summarizing browser tabs",
        "stars_today": "540 stars today",
        "url": "https://github.com/owner/useful-ai-tool",
    })
    assert github_metadata["title_ru"] == "GitHub-репозиторий: owner / useful-ai-tool"
    assert "пишет про репозиторий" in github_metadata["summary_ru"]
    assert "AI-агента" in github_metadata["summary_ru"]
    assert "540 stars today" in github_metadata["summary_ru"]
    assert "README" in github_metadata["angle_ru"]

    telegram_metadata = build_deterministic_topic_metadata_ru({
        "title": "Вышел новый AI-сервис для заметок",
        "source": "@ai_news_ru",
        "source_group": "telegram",
        "category": "tool",
        "score": 76,
        "reason": "telegram signal",
        "original_description": "Канал пишет, что сервис умеет собирать конспект встречи.",
        "url": "https://t.me/ai_news_ru/42",
    })
    assert telegram_metadata["title_ru"].startswith("Пост из Telegram")
    assert "Пост из Telegram-канала @ai_news_ru" in telegram_metadata["summary_ru"]
    assert "проверить первоисточник" in telegram_metadata["summary_ru"]

    title_only_metadata = build_deterministic_topic_metadata_ru({
        "title": "New AI calendar assistant launches",
        "source": "Product Hunt",
        "source_group": "tools",
        "category": "tool",
        "score": 69,
        "url": "https://www.producthunt.com/posts/ai-calendar",
    })
    assert title_only_metadata["title_ru"] == "Новый AI-инструмент: New AI calendar assistant launches"
    assert "New AI calendar assistant launches" in title_only_metadata["summary_ru"]
    assert "Нужна проверка деталей" in title_only_metadata["summary_ru"]
    assert MANUAL_REVIEW_NOTE_RU not in title_only_metadata["summary_ru"]

    github_fallback = {
        "id": 9,
        "title": "GitHub Trending: owner / repo",
        "source": "GitHub Trending AI",
        "source_group": "github",
        "category": "dev",
        "score": 80,
        "reason": "разработка/GitHub",
        "url": "https://github.com/owner/repo",
    }
    github_card = _topic_card_text(github_fallback)
    assert MANUAL_REVIEW_NOTE_RU in github_card
    assert "Тема требует ручной проверки" in github_card
    assert "URL: https://github.com/owner/repo" in github_card

    compact = topic_compact_preview_ru(topic)
    assert compact.startswith("OpenAI выпустила новую модель")
    assert "О чем:" in compact
    assert "Коротко объясняет" in compact

    compact_missing_ru = topic_compact_preview_ru(fallback_topic)
    assert compact_missing_ru.startswith("Тема требует ручной проверки")
    assert MANUAL_REVIEW_NOTE_RU in compact_missing_ru

    assert is_weak_topic_metadata(
        "agentmemory - #1 Persistent memory for AI coding AI-агенты based on real-world benchmarks",
        "Репозиторий выглядит как проект про Persistent memory for AI coding agents.",
        "Можно подать как GitHub-проект.",
        original_title="agentmemory - #1 Persistent memory for AI coding agents based on real-world benchmarks",
    )
    assert is_weak_topic_metadata(
        "Personal_AI_Infrastructure - Agentic AI Infrastructure for magnifying HUMAN capabilities",
        "Репозиторий выглядит как проект про Agentic AI Infrastructure.",
        "Можно подать как GitHub-проект.",
        original_title="Personal_AI_Infrastructure - Agentic AI Infrastructure for magnifying HUMAN capabilities",
    )
    assert not is_weak_topic_metadata(
        "LLMs-from-scratch - пошаговая сборка ChatGPT-подобной модели на PyTorch",
        "Репозиторий показывает, как с нуля собрать ChatGPT-подобную LLM на PyTorch.",
        "Можно подать как полезный open-source проект.",
        original_title="GitHub Trending: rasbt / LLMs-from-scratch",
    )

    ru_preview_topic = {
        "title": "GitHub Trending: rasbt / LLMs-from-scratch",
        "title_ru": "LLMs-from-scratch - пошаговая сборка ChatGPT-подобной модели на PyTorch",
        "summary_ru": "Репозиторий показывает, как с нуля собрать ChatGPT-подобную LLM на PyTorch.",
        "angle_ru": "Можно подать как полезный open-source проект.",
        "score": 95,
        "category": "dev",
    }
    collect_preview = topic_compact_preview_ru(ru_preview_topic)
    assert collect_preview.startswith("LLMs-from-scratch - пошаговая сборка")
    assert "Репозиторий показывает" in collect_preview
    assert "GitHub Trending:" not in collect_preview

    ai_card_topic = {
        **ru_preview_topic,
        "id": 10,
        "source": "GitHub Trending AI",
        "source_group": "github",
        "reason": "разработка/GitHub",
        "url": "https://github.com/rasbt/LLMs-from-scratch",
    }
    ai_card = _topic_card_text(ai_card_topic)
    assert "LLMs-from-scratch - пошаговая сборка ChatGPT-подобной модели на PyTorch" in ai_card.split("О чем:", 1)[0]
    assert "Оригинал: GitHub Trending: rasbt / LLMs-from-scratch" in ai_card

    plan = _render_plan_text("сегодня", ["10:00"], [topic])
    assert "OpenAI выпустила новую модель" in plan
    assert "OpenAI releases a new model" not in plan


if __name__ == "__main__":
    run()
    print("topic_display_selftest: ok")
