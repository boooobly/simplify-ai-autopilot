"""Self-test for @simplify_ai style guide constants."""

from bot.style_guide import HUMANIZER_RULES_FOR_SIMPLIFY_AI, SIMPLIFY_AI_EMOJI_ALIAS_GUIDE, SIMPLIFY_AI_STYLE_GUIDE


def main() -> None:
    combined = f"{SIMPLIFY_AI_STYLE_GUIDE}\n{HUMANIZER_RULES_FOR_SIMPLIFY_AI}\n{SIMPLIFY_AI_EMOJI_ALIAS_GUIDE}".lower()
    assert '[[emoji:claude]]' in combined
    assert '[[emoji:chatgpt]]' in combined
    assert '[[emoji:deepseek]]' in combined
    assert '[[emoji:github]]' in combined
    assert 'never invent new [[emoji:...]] names'.lower() in combined
    assert '[[link:text|url]]' in combined
    assert 'correct:' in combined
    assert '[[emoji:screen_card]] minimax-m1' in combined
    assert 'wrong:' in combined
    assert 'never output raw emoji in final draft text'.lower() in combined
    print('style_guide_selftest: ok')


if __name__ == '__main__':
    main()
