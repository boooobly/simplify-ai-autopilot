from bot.writer import _ensure_custom_emoji_markers


def main() -> None:
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
