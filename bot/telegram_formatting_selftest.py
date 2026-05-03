"""Лёгкие self-test проверки для форматирования Telegram."""

from bot.telegram_formatting import render_post_html, strip_quote_markers


def run() -> None:
    case1 = "Title\n\n➖ one\n➖ two\n\nEnd"
    out1 = render_post_html(case1)
    assert "<blockquote>" in out1
    assert "➖ one" in out1

    case2 = "Title\n\n▌ ➖ one\n▌ ➖ two"
    out2 = render_post_html(case2)
    assert "<blockquote>" in out2
    assert "▌" not in out2

    case3 = "Title\n\n[[QUOTE]]\n➖ one\n➖ two\n[[/QUOTE]]"
    out3 = render_post_html(case3)
    assert out3.count("<blockquote>") == 1

    case4 = "Title\n\n➖ one\n\nEnd"
    out4 = render_post_html(case4)
    assert "<blockquote>" not in out4

    case5 = "Забираем [[LINK:тут|https://example.com]]"
    out5 = render_post_html(case5)
    assert '<a href="https://example.com">тут</a>' in out5

    case6 = "Плохо [[LINK:клик|javascript:alert(1)]]"
    out6 = render_post_html(case6)
    assert "<a href=" not in out6

    case7 = "Тест [тут](https://example.com)"
    out7 = render_post_html(case7)
    assert '<a href="https://example.com">тут</a>' in out7

    case8 = strip_quote_markers("Проверка [[LINK:тут|https://example.com]] и [здесь](https://example.com)")
    assert "тут" in case8 and "здесь" in case8
    assert "https://example.com" not in case8

    case9 = render_post_html("Огонь 🔥", custom_emoji_map={"🔥": "123456"})
    assert '<tg-emoji emoji-id="123456">🔥</tg-emoji>' in case9
    case10 = render_post_html("Огонь 🔥")
    assert '<tg-emoji emoji-id="' not in case10


if __name__ == "__main__":
    run()
    print("ok")
