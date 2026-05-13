"""Self-tests for deterministic CTA link cleanup."""

from __future__ import annotations

import tempfile
from pathlib import Path

from bot.database import DraftDatabase
from bot.link_policy import is_testable_cta_url, strip_disallowed_cta_links


def run() -> None:
    anthropic = "[[EMOJI:link]] Подробнее: [[LINK:читать|https://www.anthropic.com/news/claude-update]]"
    cleaned = strip_disallowed_cta_links(anthropic, source_url="https://www.anthropic.com/news/claude-update")
    assert cleaned == ""

    tech_media = "Текст\n[[EMOJI:link]] Подробнее: [[LINK:статья|https://techcrunch.com/2026/01/01/ai-news/]]"
    cleaned = strip_disallowed_cta_links(tech_media, source_group="tech_media", category="news")
    assert "[[LINK:" not in cleaned
    assert cleaned == "Текст"

    github = "[[EMOJI:link]] Код: [[LINK:GitHub|https://github.com/rasbt/LLMs-from-scratch]]"
    assert strip_disallowed_cta_links(github) == github
    assert is_testable_cta_url("https://github.com/rasbt/LLMs-from-scratch") is True

    huggingface = "[[EMOJI:link]] Модель: [[LINK:Hugging Face|https://huggingface.co/openai/gpt-oss-20b]]"
    assert strip_disallowed_cta_links(huggingface) == huggingface
    assert is_testable_cta_url("https://huggingface.co/openai/gpt-oss-20b") is True

    product_hunt = "[[EMOJI:link]] Продукт: [[LINK:Product Hunt|https://www.producthunt.com/products/example-ai]]"
    assert strip_disallowed_cta_links(product_hunt) == product_hunt
    assert is_testable_cta_url("https://www.producthunt.com/products/example-ai") is True

    service = "[[EMOJI:link]] Тестим: [[LINK:сервис|https://example-ai-service.com]]"
    assert strip_disallowed_cta_links(service, category="tools", title="Example AI service") == service
    assert is_testable_cta_url("https://example-ai-service.com", category="tools", title="Example AI service") is True

    blog = "[[EMOJI:link]] Подробнее: [[LINK:пост|https://example-ai-service.com/blog/launch]]"
    assert strip_disallowed_cta_links(blog, category="tools", title="Example AI service") == ""
    assert is_testable_cta_url("https://example-ai-service.com/blog/launch", category="tools", title="Example AI service") is False

    with tempfile.TemporaryDirectory() as tmp:
        db = DraftDatabase(str(Path(tmp) / "drafts.db"))
        source_url = "https://www.anthropic.com/news/claude-update"
        content = strip_disallowed_cta_links("Пост\n" + anthropic, source_url=source_url)
        draft_id = db.create_draft(content, source_url=source_url)
        stored = db.get_draft(draft_id)
        assert stored is not None
        assert stored["source_url"] == source_url
        assert "[[LINK:" not in stored["content"]
        assert stored["content"] == "Пост"


if __name__ == "__main__":
    run()
    print("link_policy_selftest: ok")
