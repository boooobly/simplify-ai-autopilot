from __future__ import annotations


import bot.writer as writer
from bot.writer import GenerationResult, _ensure_custom_emoji_markers, fetch_page_content, fetch_page_content_details, generate_post_draft_from_topic_metadata, rewrite_post_draft


class _Response:
    def __init__(self, html: str) -> None:
        self.text = html
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def raise_for_status(self) -> None:
        return None


def _long_text() -> str:
    return " ".join(["Полезный текст страницы про AI инструменты и новости"] * 120)


def _html(meta: str, title: str = "Title") -> str:
    return f"""
    <html>
      <head><title>{title}</title>{meta}</head>
      <body><article>{_long_text()}</article></body>
    </html>
    """


def _with_fake_get(html: str):
    class _Patch:
        def __enter__(self):
            self.original = writer.requests.get
            writer.requests.get = lambda *args, **kwargs: _Response(html)

        def __exit__(self, exc_type, exc, tb):
            writer.requests.get = self.original

    return _Patch()


def _assert_preview_extraction() -> None:
    with _with_fake_get(_html('<meta property="og:image" content="https://cdn.example.com/og.jpg">')):
        details = fetch_page_content_details("https://example.com/post")
        assert details.title == "Title"
        assert details.preview_image_url == "https://cdn.example.com/og.jpg"

    with _with_fake_get(_html('<meta name="twitter:image" content="https://cdn.example.com/twitter.jpg">')):
        details = fetch_page_content_details("https://example.com/post")
        assert details.preview_image_url == "https://cdn.example.com/twitter.jpg"

    with _with_fake_get(_html('<meta property="og:image" content="/images/preview.jpg">')):
        details = fetch_page_content_details("https://example.com/news/post")
        assert details.preview_image_url == "https://example.com/images/preview.jpg"

    for bad_url in ["data:image/png;base64,abc", "javascript:alert(1)", "blob:https://example.com/abc"]:
        with _with_fake_get(_html(f'<meta property="og:image" content="{bad_url}">')):
            details = fetch_page_content_details("https://example.com/post")
            assert details.preview_image_url is None

    with _with_fake_get(_html('<meta property="og:image" content="https://cdn.example.com/compat.jpg">', title="Compat")):
        title, text = fetch_page_content("https://example.com/post")
        assert title == "Compat"
        assert len(text) >= 700


def _assert_rewrite_prompts() -> None:
    calls: list[tuple[str, str]] = []

    def fake_generate(api_key, model, user_prompt, system_prompt, base_url=None, extra_headers=None, max_tokens=900):
        calls.append((user_prompt, system_prompt))
        return GenerationResult(
            content="[[EMOJI:screen_card]] Обновлённый черновик с фактами.\n\n[[EMOJI:link]] Детали: [[LINK:источник|https://example.com]]",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            model=model,
        )

    original = writer._generate_with_chat_completion
    writer._generate_with_chat_completion = fake_generate
    try:
        results = {}
        for mode in ("remove_fluff", "shorten", "neutralize_ads"):
            result = rewrite_post_draft(
                "key",
                "model-x",
                "Источник: https://example.com\n[[EMOJI:screen_card]] Текст с фактами и [[LINK:источником|https://example.com]]",
                source_url="https://example.com",
                mode=mode,
                max_chars=500,
                soft_chars=350,
            )
            results[mode] = result.content
    finally:
        writer._generate_with_chat_completion = original

    assert len(calls) == 3
    prompts = [call[0] for call in calls]
    assert "Режим: убрать воду" in prompts[0]
    assert "Режим: сделать короче" in prompts[1]
    assert "60-70%" in prompts[1]
    assert "Режим: убрать рекламный тон" in prompts[2]
    assert all("Не добавляй строку Источник" in prompt for prompt in prompts)
    assert all("Сохраняй полезные маркеры ссылок" in prompt for prompt in prompts)
    assert all("Сохраняй существующие [[EMOJI:alias]]" in prompt for prompt in prompts)
    assert all("Источник:" not in content for content in results.values())
    assert all("[[LINK:источник|https://example.com]]" in content for content in results.values())
    assert len(set(prompts)) == 3

    try:
        rewrite_post_draft("key", "model-x", "Достаточно длинный осмысленный текст черновика", mode="bad_mode")
        raise AssertionError("unsupported rewrite mode must fail")
    except ValueError:
        pass



def _assert_topic_metadata_generation() -> None:
    calls: list[tuple[str, str]] = []

    def fake_generate(api_key, model, user_prompt, system_prompt, base_url=None, extra_headers=None, max_tokens=900):
        calls.append((user_prompt, system_prompt))
        return GenerationResult(
            content="Источник: https://reddit.com/r/LocalLLaMA/comments/test\n[[EMOJI:screen_card]] Обсуждают новый AI-инструмент по описанию темы.\n\n[[EMOJI:link]] Детали: [[LINK:открыть обсуждение|https://reddit.com/r/LocalLLaMA/comments/test]]",
            prompt_tokens=11,
            completion_tokens=22,
            total_tokens=33,
            model=model,
        )

    original = writer._generate_with_chat_completion
    writer._generate_with_chat_completion = fake_generate
    try:
        result = generate_post_draft_from_topic_metadata(
            api_key="key",
            model="model-x",
            topic_title="New AI tool v2.1",
            topic_title_ru="Новый AI-инструмент v2.1",
            topic_summary_ru="Краткое описание сохранено в теме.",
            topic_angle_ru="Почему это полезно админам канала.",
            topic_original_description="Original Reddit description",
            topic_source="Reddit",
            topic_source_group="reddit",
            topic_category="tools",
            source_url="https://reddit.com/r/LocalLLaMA/comments/test",
            max_chars=500,
            soft_chars=350,
        )
    finally:
        writer._generate_with_chat_completion = original

    assert len(calls) == 1
    prompt = calls[0][0]
    assert "Полная страница источника не была прочитана" in prompt
    assert "Не выдумывай факты" in prompt
    assert "New AI tool v2.1" in prompt
    assert "Новый AI-инструмент v2.1" in prompt
    assert "Источник:" not in result.content
    assert result.prompt_tokens == 11
    assert result.model == "model-x"


def main() -> None:
    _assert_preview_extraction()
    _assert_rewrite_prompts()
    _assert_topic_metadata_generation()

    out = _ensure_custom_emoji_markers("🤖 MiniMax-M1: миллион токенов", title="MiniMax-M1")
    assert out.startswith("[[EMOJI:screen_card]]")

    out = _ensure_custom_emoji_markers("💭 Финальная мысль")
    assert out == "[[EMOJI:thought]] Финальная мысль"

    out = _ensure_custom_emoji_markers("🧾 Веса - [[LINK:на Hugging Face|https://huggingface.co/x]]")
    assert out == "[[EMOJI:link]] Веса - [[LINK:на Hugging Face|https://huggingface.co/x]]"

    src = "Заголовок\n➖ пункт 1\n➖ пункт 2\nТекст 🤖 внутри"
    out = _ensure_custom_emoji_markers(src, title="MiniMax")
    assert "➖ пункт 1" in out and "➖ пункт 2" in out
    assert "Текст 🤖 внутри" in out

    out = _ensure_custom_emoji_markers("Claude 4 update", title="Claude 4 update")
    assert out.startswith("[[EMOJI:claude]]")

    out = _ensure_custom_emoji_markers("ChatGPT теперь быстрее", title="ChatGPT теперь быстрее")
    assert out.startswith("[[EMOJI:chatgpt]]")

    out = _ensure_custom_emoji_markers("DeepSeek выпустил релиз", title="DeepSeek выпустил релиз")
    assert out.startswith("[[EMOJI:deepseek]]")

    print("writer_selftest: ok")


if __name__ == '__main__':
    main()
