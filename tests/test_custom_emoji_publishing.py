from bot import publisher
from bot.publisher import _prepare_media_caption, _render_or_plain
from bot.telegram_formatting import _apply_custom_emoji, render_post_html


EMOJI_MAP = {"🔥": "5368324170671202286", "💭": "1234567890123456789"}
EMOJI_ALIASES = {
    "fire": ("🔥", "5368324170671202286"),
    "thought": ("💭", "1234567890123456789"),
}


def test_raw_custom_emoji_map_renders_in_non_strict_mode():
    rendered = render_post_html("🔥 Новость", custom_emoji_map=EMOJI_MAP)

    assert rendered == '<tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji> Новость'


def test_raw_custom_emoji_map_renders_in_strict_mode():
    rendered = render_post_html("🔥 Новость", custom_emoji_map=EMOJI_MAP, strict_custom_emoji=True)

    assert rendered == '<tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji> Новость'


def test_custom_emoji_alias_renders_in_both_modes():
    for strict in (False, True):
        rendered = render_post_html(
            "[[EMOJI:fire]] Новость",
            custom_emoji_aliases=EMOJI_ALIASES,
            strict_custom_emoji=strict,
        )
        assert rendered == '<tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji> Новость'


def test_unknown_alias_falls_back_safely():
    assert render_post_html("[[EMOJI:unknown]] Новость", strict_custom_emoji=False) == " Новость"
    assert render_post_html("[[EMOJI:unknown]] Новость", strict_custom_emoji=True) == "Новость"


def test_links_and_blockquotes_survive_custom_emoji_rendering():
    rendered = render_post_html(
        "[[QUOTE]]\n🔥 Важно\n[[/QUOTE]]\n[[LINK:💭 Подробнее|https://example.com]]",
        custom_emoji_map=EMOJI_MAP,
        strict_custom_emoji=True,
    )

    assert "<blockquote>" in rendered
    assert '<a href="https://example.com">' in rendered
    assert rendered.count("<tg-emoji ") == 2


def test_custom_emoji_replacement_does_not_touch_link_attributes():
    rendered = render_post_html(
        "[[LINK:🔥 Подробнее|https://example.com/🔥]]",
        custom_emoji_map=EMOJI_MAP,
        strict_custom_emoji=True,
    )

    assert 'href="https://example.com/🔥"' in rendered
    assert '<a href="https://example.com/🔥"><tg-emoji ' in rendered


def test_custom_emoji_map_does_not_replace_inside_existing_tag():
    existing = '<tg-emoji emoji-id="111">🔥</tg-emoji> 🔥'

    rendered = _apply_custom_emoji(existing, {"🔥": "222"})

    assert rendered == '<tg-emoji emoji-id="111">🔥</tg-emoji> <tg-emoji emoji-id="222">🔥</tg-emoji>'


def test_overlapping_custom_emoji_fallbacks_do_not_create_nested_tags():
    rendered = render_post_html(
        "✏️ ✏",
        custom_emoji_map={"✏️": "222", "✏": "111"},
        strict_custom_emoji=True,
    )

    assert rendered == (
        '<tg-emoji emoji-id="222">✏️</tg-emoji> '
        '<tg-emoji emoji-id="111">✏</tg-emoji>'
    )
    assert rendered.count("<tg-emoji") == 2


def test_malformed_custom_emoji_id_is_not_rendered():
    rendered = render_post_html("🔥 Новость", custom_emoji_map={"🔥": "not-an-id"})

    assert "<tg-emoji" not in rendered
    assert rendered == "🔥 Новость"


def test_non_emoji_fallback_is_not_rendered_as_custom_emoji():
    rendered = render_post_html("text🔥 Новость", custom_emoji_map={"text🔥": "123"})

    assert rendered == "text🔥 Новость"
    assert "<tg-emoji" not in rendered


def test_publisher_returns_html_when_custom_emoji_is_generated():
    rendered, parse_mode = _render_or_plain("🔥 Новость", custom_emoji_map=EMOJI_MAP)

    assert parse_mode == "HTML"
    assert '<tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji>' in rendered


def test_media_caption_preserves_custom_emoji_html():
    caption = _prepare_media_caption("💭 Подпись", custom_emoji_map=EMOJI_MAP)

    assert caption.parse_mode == "HTML"
    assert '<tg-emoji emoji-id="1234567890123456789">💭</tg-emoji>' in caption.text


def test_plain_fallback_does_not_remove_the_post(monkeypatch):
    def _raise_render(*args, **kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(publisher, "render_post_html", _raise_render)

    rendered, parse_mode = _render_or_plain("🔥 Важная новость", custom_emoji_map=EMOJI_MAP)

    assert parse_mode is None
    assert rendered == "Важная новость"
