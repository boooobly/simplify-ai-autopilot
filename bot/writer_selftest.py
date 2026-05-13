from __future__ import annotations


import bot.writer as writer
from bot.writer import _ensure_custom_emoji_markers, fetch_page_content, fetch_page_content_details


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


def main() -> None:
    _assert_preview_extraction()

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
