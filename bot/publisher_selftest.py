"""Lightweight self-tests for safe Telegram caption preparation."""

import bot.publisher as publisher
from bot.publisher import MEDIA_PREVIEW_CAPTION_LIMIT, _prepare_media_caption, _render_or_plain, _shorten_internal_text


ALIASES = {
    "claude": ("🤖", "5208880957280522189"),
}


def _assert_balanced_tag(rendered: str, tag: str) -> None:
    assert rendered.count(f"<{tag}") == rendered.count(f"</{tag}>")


def run() -> None:
    link_marker = "[[LINK:" + "x" * 80 + "|https://example.com/" + "y" * 80 + "]]"
    long_link_post = "A" * 260 + link_marker + " tail " + "Z" * 900
    link_caption = _prepare_media_caption(long_link_post)
    assert link_caption.send_full_text_after is True
    assert link_caption.parse_mode == "HTML"
    assert len(_shorten_internal_text(long_link_post)) <= MEDIA_PREVIEW_CAPTION_LIMIT
    assert "[[LINK:" not in link_caption.text
    _assert_balanced_tag(link_caption.text, "a")

    emoji_marker = "[[EMOJI:claude]]"
    long_emoji_post = "B" * 290 + emoji_marker + " tail " + "C" * 900
    emoji_caption = _prepare_media_caption(long_emoji_post, custom_emoji_aliases=ALIASES)
    assert emoji_caption.send_full_text_after is True
    assert emoji_caption.parse_mode == "HTML"
    assert "[[EMOJI:" not in emoji_caption.text
    _assert_balanced_tag(emoji_caption.text, "tg-emoji")

    long_post = "Intro " + ("word " * 260)
    preview_caption = _prepare_media_caption(long_post)
    assert preview_caption.send_full_text_after is True
    assert preview_caption.text.endswith("…")
    assert len(_shorten_internal_text(long_post)) <= MEDIA_PREVIEW_CAPTION_LIMIT

    short_post = "Short [[LINK:site|https://example.com]]"
    short_caption = _prepare_media_caption(short_post)
    assert short_caption.send_full_text_after is False
    assert '<a href="https://example.com">site</a>' in short_caption.text

    fallback_caption = _prepare_media_caption("[[EMOJI:fire]] Hot [[EMOJI:unknown]]")
    assert fallback_caption.text == "🔥 Hot "
    assert "[[EMOJI:" not in fallback_caption.text

    original_render = publisher.render_post_html

    def _raise_render(*args, **kwargs):
        raise RuntimeError("forced render failure")

    try:
        publisher.render_post_html = _raise_render
        plain_text, parse_mode = _render_or_plain("[[EMOJI:idea]] Plain [[EMOJI:unknown]]")
    finally:
        publisher.render_post_html = original_render
    assert parse_mode is None
    assert plain_text == "💡 Plain "
    assert "[[EMOJI:" not in plain_text


if __name__ == "__main__":
    run()
    print("publisher_selftest: ok")
