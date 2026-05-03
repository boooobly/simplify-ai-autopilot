from bot.topic_scoring import normalize_topic_title, score_topic


def run() -> None:
    score, category, reason = score_topic("Claude adds new browser agent", "Anthropic news", "https://example.com")
    assert score >= 75
    assert category == "agent"
    assert "агенты" in reason

    score2, category2, _ = score_topic("Company raises funding", "Some blog", "https://example.com")
    assert score2 <= 40 or category2 == "business"

    normalized = normalize_topic_title("The New, AI Agent: запустила!")
    assert normalized == "ai agent"


if __name__ == "__main__":
    run()
