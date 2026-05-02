"""Collect candidate AI topics from curated public sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


@dataclass
class TopicItem:
    title: str
    url: str
    source: str
    published_at: str | None = None


RSS_SOURCES: list[tuple[str, str]] = [
    ("OpenAI blog", "https://openai.com/news/rss.xml"),
    ("Anthropic news", "https://www.anthropic.com/news/rss.xml"),
    ("Google AI blog", "https://blog.google/technology/ai/rss/"),
    ("Perplexity blog", "https://www.perplexity.ai/hub/blog/rss.xml"),
    ("Hugging Face blog", "https://huggingface.co/blog/feed.xml"),
]


def _parse_rss(xml_text: str, source_name: str, max_items: int = 8) -> list[TopicItem]:
    root = ET.fromstring(xml_text)
    topics: list[TopicItem] = []

    for item in root.findall(".//item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or item.findtext("published") or "").strip()
        if not title or not link:
            continue

        published_at = None
        if pub_date_raw:
            try:
                dt = parsedate_to_datetime(pub_date_raw)
                published_at = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                published_at = None

        topics.append(TopicItem(title=title, url=link, source=source_name, published_at=published_at))

    return topics


def _fetch_github_trending_ai() -> list[TopicItem]:
    response = requests.get("https://github.com/trending", timeout=12)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    topics: list[TopicItem] = []

    for article in soup.select("article.Box-row")[:20]:
        repo_tag = article.select_one("h2 a")
        if not repo_tag:
            continue
        repo_name = " ".join(repo_tag.get_text(" ", strip=True).split())
        lower = repo_name.lower() + " " + article.get_text(" ", strip=True).lower()
        if "ai" not in lower and "llm" not in lower and "model" not in lower:
            continue
        repo_path = repo_tag.get("href", "").strip()
        if not repo_path.startswith("/"):
            continue
        topics.append(
            TopicItem(
                title=f"GitHub Trending: {repo_name}",
                url=f"https://github.com{repo_path}",
                source="GitHub Trending AI",
                published_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    return topics[:8]


def collect_topics() -> list[TopicItem]:
    """Fetch fresh topic candidates from curated RSS feeds and public pages."""

    collected: list[TopicItem] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; simplify-ai-autopilot/1.0; +https://t.me/simplify_ai)"
    }

    for source_name, rss_url in RSS_SOURCES:
        try:
            response = requests.get(rss_url, timeout=12, headers=headers)
            response.raise_for_status()
            collected.extend(_parse_rss(response.text, source_name))
        except Exception:
            continue

    try:
        collected.extend(_fetch_github_trending_ai())
    except Exception:
        pass

    return collected
