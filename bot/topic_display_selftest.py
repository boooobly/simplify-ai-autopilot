from __future__ import annotations

from bot.handlers import _render_plan_text, _topic_card_text
from bot.topic_display import topic_display_reason, topic_display_title


def run() -> None:
    topic = {
        "id": 7,
        "title": "OpenAI releases a new model",
        "title_ru": "OpenAI выпустила новую модель",
        "reason": "model",
        "reason_ru": "модель",
        "source": "OpenAI blog",
        "source_group": "official_ai",
        "category": "model",
        "score": 91,
        "url": "https://example.com/openai-model",
    }
    card = _topic_card_text(topic)
    assert "OpenAI выпустила новую модель" in card
    assert "Оригинал: OpenAI releases a new model" in card
    assert "Почему: модель" in card
    assert "URL: https://example.com/openai-model" in card

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
    assert "Оригинал:" not in fallback_card

    plan = _render_plan_text("сегодня", ["10:00"], [topic])
    assert "OpenAI выпустила новую модель" in plan
    assert "OpenAI releases a new model" not in plan


if __name__ == "__main__":
    run()
    print("topic_display_selftest: ok")
