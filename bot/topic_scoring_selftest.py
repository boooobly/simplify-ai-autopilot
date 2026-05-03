from bot.topic_scoring import normalize_topic_title, score_topic


def run() -> None:
    s0, c0, _ = score_topic("OpenAI launches new GPT model", "OpenAI Blog", "https://openai.com", "official_ai")
    assert c0 in {"model", "news"}
    assert s0 >= 55

    s1, c1, _ = score_topic("Free open-source AI video editor", "GitHub", "https://github.com/x", "tools")
    assert s1 >= 80 and c1 in {"creator", "tool", "dev", "guide"}

    s2, c2, _ = score_topic("Company raises funding", "News", "https://x", "official_ai")
    assert c2 == "business" or s2 < 65

    s3, c3, _ = score_topic("AI tool website for removing background", "Tool Hunt", "https://example.com", "tools")
    assert c3 == "tool"

    s4, c4, r4 = score_topic("GitHub repo for AI coding agent", "GitHub", "https://github.com/example/repo", "github")
    assert c4 in {"dev", "agent", "tool"} or (s4 >= 70 and "разработка/GitHub" in r4)

    s5, c5, _ = score_topic("New AI research paper on alignment", "arXiv", "https://arxiv.org/abs/123", "tech_media")
    assert c5 == "research"

    assert normalize_topic_title("The new GPT-5 release!!!") == "gpt 5 release"


if __name__ == "__main__":
    run()
    print("OK")
