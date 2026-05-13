from datetime import datetime, timedelta, timezone
import os

import bot.sources as sources
from bot.sources import _parse_dt, _parse_rss, build_github_topic_ru_metadata, fetch_x_topics, parse_custom_topic_feeds


class _FakeResponse:
    def __init__(self, json_data=None, text: str = ""):
        self._json_data = json_data or {}
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._json_data


def _with_env(env: dict[str, str], fn) -> None:
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        fn()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def run() -> None:
    feeds = parse_custom_topic_feeds("Karpathy X|custom|https://example.com/k.rss,Testing|https://example.com/t.rss,bad")
    assert len(feeds) == 2
    assert feeds[0][1] == "custom"

    assert _parse_dt("Tue, 12 May 2026 14:30:00 +0000") == "2026-05-12 14:30:00"
    assert _parse_dt("2026-05-12T14:30:00Z") == "2026-05-12 14:30:00"
    assert _parse_dt("2026-05-12T14:30:00+00:00") == "2026-05-12 14:30:00"
    assert _parse_dt("2026-05-12") == "2026-05-12 00:00:00"
    assert _parse_dt("not a date") is None

    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    atom = f"""<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><entry><title>T1</title><link href='https://example.com/1'/><updated>{recent}</updated></entry></feed>"""
    items = _parse_rss(atom, "A", "community", max_items=5)
    assert len(items) == 1
    assert items[0].url == "https://example.com/1"
    assert items[0].published_at is not None
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

    original_get = sources.requests.get
    original_github = sources._fetch_github_trending_ai
    try:
        calls: list[str] = []

        def fake_get(url, *args, **kwargs):
            calls.append(url)
            return _FakeResponse(text="<rss><channel></channel></rss>")

        sources.requests.get = fake_get
        sources._fetch_github_trending_ai = lambda: []

        def _reddit_disabled() -> None:
            items, reports = sources.collect_topics_with_diagnostics()
            assert items == []
            assert not any("reddit.com" in url for url in calls)
            assert any(r.name == "Reddit community RSS" and r.status == "skipped" for r in reports)

        _with_env({"ENABLE_REDDIT_SOURCES": "false", "ENABLE_X_SOURCES": "false", "CUSTOM_TOPIC_FEEDS": ""}, _reddit_disabled)

        calls.clear()

        def _reddit_enabled() -> None:
            sources.collect_topics_with_diagnostics()
            assert any("reddit.com" in url for url in calls)

        _with_env({"ENABLE_REDDIT_SOURCES": "true", "ENABLE_X_SOURCES": "false", "CUSTOM_TOPIC_FEEDS": ""}, _reddit_enabled)

        calls.clear()

        def _x_disabled_no_api_call() -> None:
            sources.collect_topics_with_diagnostics()
            assert not any("api.x.com" in url for url in calls)
            assert not any(r.source_group == "x" for r in sources.collect_topics_with_diagnostics()[1])

        _with_env({"ENABLE_X_SOURCES": "false", "ENABLE_REDDIT_SOURCES": "false", "CUSTOM_TOPIC_FEEDS": ""}, _x_disabled_no_api_call)

        def _x_enabled_missing_config() -> None:
            _items, reports = sources.collect_topics_with_diagnostics()
            report = next(r for r in reports if r.name == "X API")
            assert report.status == "skipped"
            assert "X_API_BEARER_TOKEN" in report.error and "X_ACCOUNTS" in report.error

        _with_env({"ENABLE_X_SOURCES": "true", "ENABLE_REDDIT_SOURCES": "false", "CUSTOM_TOPIC_FEEDS": ""}, _x_enabled_missing_config)
    finally:
        sources.requests.get = original_get
        sources._fetch_github_trending_ai = original_github

    original_get = sources.requests.get
    try:
        api_calls: list[tuple[str, dict]] = []

        def fake_x_get(url, *args, **kwargs):
            api_calls.append((url, kwargs.get("params") or {}))
            if url.endswith("/2/users/by/username/openai"):
                return _FakeResponse({"data": {"id": "42", "username": "OpenAI"}})
            if url.endswith("/2/users/42/tweets"):
                return _FakeResponse(
                    {
                        "data": [
                            {"id": "1", "text": "New AI model update with practical details for builders and product teams.", "created_at": "2026-05-12T14:30:00Z"},
                            {"id": "2", "text": "This should be excluded because it is a repost about AI tooling.", "created_at": "2026-05-12T14:31:00Z", "referenced_tweets": [{"type": "retweeted", "id": "1"}]},
                            {"id": "3", "text": "Another AI agent release with enough context for a useful Telegram topic.", "created_at": "2026-05-12T14:32:00Z"},
                        ]
                    }
                )
            raise AssertionError(f"Unexpected X API URL: {url}")

        sources.requests.get = fake_x_get
        x_items, x_reports = fetch_x_topics("token", ["openai"], 2)
        assert len(x_items) == 1
        assert x_items[0].title.startswith("X: @OpenAI - New AI model")
        assert x_items[0].url == "https://x.com/OpenAI/status/1"
        assert x_items[0].source == "X @OpenAI"
        assert x_items[0].source_group == "x"
        assert x_items[0].published_at == "2026-05-12 14:30:00"
        assert x_items[0].summary_ru
        assert x_reports[0].status == "ok" and x_reports[0].item_count == 1
        tweet_params = [params for url, params in api_calls if url.endswith("/2/users/42/tweets")][0]
        assert tweet_params["max_results"] == 2
        assert tweet_params["exclude"] == "retweets,replies"
        assert "pagination_token" not in tweet_params
    finally:
        sources.requests.get = original_get


if __name__ == "__main__":
    run()
    print("OK")
