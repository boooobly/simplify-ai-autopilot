from datetime import datetime, timedelta, timezone

from bot.sources import _parse_rss, build_github_topic_ru_metadata, parse_custom_topic_feeds


def run() -> None:
    feeds = parse_custom_topic_feeds("Karpathy X|custom|https://example.com/k.rss,Testing|https://example.com/t.rss,bad")
    assert len(feeds) == 2
    assert feeds[0][1] == "custom"

    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    atom = f"""<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><entry><title>T1</title><link href='https://example.com/1'/><updated>{recent}</updated></entry></feed>"""
    items = _parse_rss(atom, "A", "community", max_items=5)
    assert len(items) == 1
    assert items[0].url == "https://example.com/1"
    assert items[0].reason_ru
    assert items[0].title_ru is None

    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    stale_rss = f"""<rss><channel><item><title>Old AI tool launch</title><link>https://example.com/old</link><pubDate>{old_date}</pubDate></item></channel></rss>"""
    stale_items = _parse_rss(stale_rss, "A", "tech_media", max_items=5)
    assert len(stale_items) == 1
    assert stale_items[0].published_at is not None

    missing_date_rss = """<rss><channel><item><title>AI tool without date</title><link>https://example.com/no-date</link></item></channel></rss>"""
    missing_date_items = _parse_rss(missing_date_rss, "A", "tech_media", max_items=5)
    assert len(missing_date_items) == 1
    assert missing_date_items[0].published_at is None

    rss = """<rss><channel><item><title>Новая модель OpenAI</title><link>https://example.com/ru</link></item></channel></rss>"""
    ru_items = _parse_rss(rss, "RU", "ru_tech", max_items=5)
    assert len(ru_items) == 1
    assert ru_items[0].title_ru == "Новая модель OpenAI"

    title_ru, summary_ru, angle_ru = build_github_topic_ru_metadata(
        "HKUDS / AI-Trader",
        "A multi-agent framework for financial trading",
        "Python",
        "12,345",
        "900 stars today",
    )
    assert "AI-Trader" in title_ru
    assert "open-source" in title_ru
    assert "financial trading" in summary_ru
    assert "AI-инструменты" in angle_ru or "open-source" in angle_ru

    cyr_title, cyr_summary, _ = build_github_topic_ru_metadata("owner / РусскийПроект", None)
    assert "РусскийПроект" in cyr_title
    assert "GitHub-проект" in cyr_summary


if __name__ == "__main__":
    run()
    print("OK")
