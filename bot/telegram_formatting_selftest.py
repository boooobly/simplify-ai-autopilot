"""Лёгкие self-test проверки для форматирования Telegram-цитат."""

from bot.telegram_formatting import render_post_html


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


if __name__ == "__main__":
    run()
    print("ok")
