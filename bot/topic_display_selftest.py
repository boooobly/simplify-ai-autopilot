from __future__ import annotations

from bot.handlers import _render_plan_text, _topic_card_text
from bot.topic_display import topic_compact_preview_ru, topic_display_reason, topic_display_title


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
    assert topic_display_title(fallback_topic) == "English fallback title"
    assert topic_display_reason(fallback_topic) == "новость/релиз"
    assert "English fallback title" in fallback_card
    assert "О чем:" in fallback_card
    assert "Источник предлагает новость по AI" in fallback_card
    assert "Идея поста:" in fallback_card
    assert "Оригинал:" not in fallback_card

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
    assert "Похоже на GitHub-проект по AI/разработке" in github_card
    assert "URL: https://github.com/owner/repo" in github_card

    compact = topic_compact_preview_ru(topic)
    assert compact.startswith("OpenAI выпустила новую модель")
    assert "О чем:" in compact
    assert "Коротко объясняет" in compact

    compact_missing_ru = topic_compact_preview_ru(fallback_topic)
    assert compact_missing_ru.startswith("Нужна проверка: English fallback title")
    assert "О чем:" in compact_missing_ru

    plan = _render_plan_text("сегодня", ["10:00"], [topic])
    assert "OpenAI выпустила новую модель" in plan
    assert "OpenAI releases a new model" not in plan


if __name__ == "__main__":
    run()
    print("topic_display_selftest: ok")
