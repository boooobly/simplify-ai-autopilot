from bot.sources import _parse_rss, parse_custom_topic_feeds


def run() -> None:
    feeds = parse_custom_topic_feeds("Karpathy X|custom|https://example.com/k.rss,Testing|https://example.com/t.rss,bad")
    assert len(feeds) == 2
    assert feeds[0][1] == "custom"

    atom = """<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><entry><title>T1</title><link href='https://example.com/1'/><updated>2026-01-01T00:00:00Z</updated></entry></feed>"""
    items = _parse_rss(atom, "A", "community", max_items=5)
    assert len(items) == 1
    assert items[0].url == "https://example.com/1"


if __name__ == "__main__":
    run()
    print("OK")
