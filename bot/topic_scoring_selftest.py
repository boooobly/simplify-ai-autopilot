from bot.topic_scoring import canonical_topic_key, is_similar_topic_key, normalize_topic_title, score_topic


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

    assert normalize_topic_title("The new GPT-5 release!!!") == "gpt 5"

    assert canonical_topic_key("OpenAI: announces new GPT-5.1!!!", "official_ai") == "gpt 5.1"
    assert canonical_topic_key("GitHub Trending: owner / Sora video agent", "github") == "owner sora video agent"
    assert canonical_topic_key("Anthropic представила новый Claude 3.0", "official_ai") == "anthropic claude 3.0"
    assert is_similar_topic_key(
        canonical_topic_key("OpenAI launches GPT-5.1 for ChatGPT"),
        canonical_topic_key("The Verge: OpenAI unveils GPT-5.1 with ChatGPT update"),
    )
    assert not is_similar_topic_key(
        canonical_topic_key("OpenAI changes ChatGPT privacy controls"),
        canonical_topic_key("OpenAI releases Sora video editing tools"),
    )
    assert not is_similar_topic_key("openai chatgpt", "openai chatgpt plus")


if __name__ == "__main__":
    run()
    print("OK")
