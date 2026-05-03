"""Self-test for @simplify_ai style guide constants."""

from bot.style_guide import HUMANIZER_RULES_FOR_SIMPLIFY_AI, SIMPLIFY_AI_STYLE_GUIDE


def main() -> None:
    combined = f"{SIMPLIFY_AI_STYLE_GUIDE}\n{HUMANIZER_RULES_FOR_SIMPLIFY_AI}".lower()
    assert "@simplify_ai" in combined
    assert "➖" in combined
    assert "не про" in combined
    assert "эм-даш" in combined or "—" in combined
    assert "не выдумывай факты" in combined or "не invent" in combined
    print("style_guide_selftest: ok")


if __name__ == "__main__":
    main()
