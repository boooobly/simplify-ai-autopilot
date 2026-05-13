from bot.topic_scoring import normalize_topic_title, score_topic


def run() -> None:
    useful_tool, useful_category, _ = score_topic(
        "Free AI video editor app for quick captions and shorts",
        "Product Hunt",
        "https://producthunt.com/posts/ai-video-editor",
        "tools",
        published_at="2026-05-12 00:00:00",
    )
    assert useful_tool >= 80 and useful_category in {"creator", "tool", "mobile"}

    funding, funding_category, _ = score_topic(
        "Enterprise AI startup raises $80M Series B funding",
        "TechCrunch AI",
        "https://example.com/funding",
        "tech_media",
        published_at="2026-05-12 00:00:00",
    )
    assert funding_category == "business"
    assert funding < useful_tool - 25
    assert funding < 60

    research, research_category, _ = score_topic(
        "New AI research paper proposes dataset for abstract reasoning",
        "arXiv",
        "https://arxiv.org/abs/123",
        "tech_media",
        published_at="2026-05-12 00:00:00",
    )
    assert research_category == "research"
    assert research < 60

    github_clear, github_clear_category, _ = score_topic(
        "GitHub Trending: owner / ClipWizard",
        "GitHub Trending AI",
        "https://github.com/owner/clipwizard",
        "github",
        description="Open-source local app and browser extension for AI video editing workflow automation",
        stars_today="420 stars today",
        published_at="2026-05-12 00:00:00",
    )
    github_empty, _, github_empty_reason = score_topic(
        "GitHub Trending: owner / llm-kernel-bindings",
        "GitHub Trending AI",
        "https://github.com/owner/llm-kernel-bindings",
        "github",
        description="",
        stars_today="12 stars today",
        published_at="2026-05-12 00:00:00",
    )
    assert github_clear_category in {"tool", "creator", "agent", "dev"}
    assert github_clear >= github_empty + 20
    assert github_clear < 100
    assert "GitHub без описания" in github_empty_reason

    spam, _, _ = score_topic(
        "Best AI crypto casino airdrop and token presale",
        "Spam",
        "https://example.com/casino",
        "custom",
        published_at="2026-05-12 00:00:00",
    )
    assert spam < 35

    missing_date, _, reason = score_topic("AI tool for prompts", "Blog", "https://example.com", "tech_media")
    assert "нет даты" in reason

    assert normalize_topic_title("The new GPT-5 release!!!") == "gpt 5 release"


if __name__ == "__main__":
    run()
    print("OK")
