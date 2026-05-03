from bot.topic_scoring import normalize_topic_title, score_topic


def run() -> None:
    s1, c1, _ = score_topic("Free open-source AI video editor", "GitHub", "https://github.com/x", "tools")
    assert s1 >= 80 and c1 in {"creator", "tool", "dev", "guide"}

    s2, c2, _ = score_topic("Company raises funding", "News", "https://x", "official_ai")
    assert s2 < s1 or c2 == "business"

    s3, c3, _ = score_topic("Reddit users found weird ChatGPT bug", "Reddit", "https://reddit.com", "community")
    assert s3 >= 75 and c3 in {"drama", "meme", "community", "other"}

    assert normalize_topic_title("The new GPT-5 release!!!") == "gpt 5 release"


if __name__ == "__main__":
    run()
    print("OK")
