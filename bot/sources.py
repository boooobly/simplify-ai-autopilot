"""Collect candidate AI topics from curated public sources."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from bot.topic_scoring import humanize_topic_reason_ru, normalize_topic_title, score_topic


@dataclass
class TopicItem:
    title: str
    url: str
    source: str
    published_at: str | None = None
    category: str = "other"
    score: int = 0
    reason: str = ""
    title_ru: str | None = None
    summary_ru: str | None = None
    angle_ru: str | None = None
    reason_ru: str | None = None
    original_description: str | None = None
    stars_today: str | None = None
    normalized_title: str = ""
    source_group: str = "other"


@dataclass
class SourceReport:
    name: str
    url: str
    source_group: str
    status: str  # "ok", "empty", "error"
    item_count: int = 0
    error: str = ""


OFFICIAL_AI_RSS = [("OpenAI blog", "https://openai.com/news/rss.xml"), ("Anthropic news", "https://www.anthropic.com/news/rss.xml"), ("Google AI blog", "https://blog.google/technology/ai/rss/"), ("Perplexity blog", "https://www.perplexity.ai/hub/blog/rss.xml"), ("Hugging Face blog", "https://huggingface.co/blog/feed.xml"), ("Microsoft AI blog", "https://blogs.microsoft.com/ai/feed/"), ("NVIDIA blog AI", "https://blogs.nvidia.com/blog/category/ai/feed/")]
TECH_MEDIA_RSS = [("VentureBeat AI", "https://venturebeat.com/ai/feed/"), ("The Decoder", "https://the-decoder.com/feed/"), ("MarkTechPost", "https://www.marktechpost.com/feed/"), ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"), ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"), ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"), ("Ars Technica AI", "https://arstechnica.com/ai/feed/")]
RU_TECH_RSS = [("Habr AI", "https://habr.com/ru/rss/hubs/ai/all/"), ("Habr ML", "https://habr.com/ru/rss/hub/machine_learning/"), ("Habr Dev", "https://habr.com/ru/rss/all/all/?fl=ru"), ("vc.ru technology", "https://vc.ru/rss/all"), ("Tproger", "https://tproger.ru/feed"), ("3DNews", "https://3dnews.ru/news/rss"), ("iXBT", "https://www.ixbt.com/export/news.rss")]
TOOLS_RSS = [("Product Hunt", "https://www.producthunt.com/feed")]
COMMUNITY_RSS = [("Reddit r/artificial", "https://www.reddit.com/r/artificial/.rss"), ("Reddit r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/.rss"), ("Reddit r/OpenAI", "https://www.reddit.com/r/OpenAI/.rss"), ("Reddit r/ChatGPT", "https://www.reddit.com/r/ChatGPT/.rss"), ("Reddit r/ClaudeAI", "https://www.reddit.com/r/ClaudeAI/.rss"), ("Reddit r/SideProject", "https://www.reddit.com/r/SideProject/.rss"), ("Reddit r/InternetIsBeautiful", "https://www.reddit.com/r/InternetIsBeautiful/.rss")]


def parse_custom_topic_feeds(env_value: str | None) -> list[tuple[str, str, str]]:
    if not env_value:
        return []
    feeds: list[tuple[str, str, str]] = []
    for raw in env_value.split(","):
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        if len(parts) == 3:
            name, group, url = parts
        elif len(parts) == 2:
            name, url = parts
            group = "custom"
        else:
            continue
        if url.startswith("http"):
            feeds.append((name, group, url))
    return feeds


def _contains_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


def _with_scoring(topic: TopicItem) -> TopicItem:
    score, category, reason = score_topic(
        topic.title,
        topic.source,
        topic.url,
        topic.source_group,
        description=topic.original_description,
        published_at=topic.published_at,
        stars_today=topic.stars_today,
    )
    topic.score = score
    topic.category = category
    topic.reason = reason
    topic.reason_ru = humanize_topic_reason_ru(category, score, topic.source_group, reason)
    if _contains_cyrillic(topic.title):
        topic.title_ru = topic.title
    topic.normalized_title = normalize_topic_title(topic.title)
    return topic


def _parse_rss(xml_text: str, source_name: str, source_group: str, max_items: int = 8) -> list[TopicItem]:
    root = ET.fromstring(xml_text)
    topics: list[TopicItem] = []
    items = root.findall(".//item")
    entries = root.findall(".//{http://www.w3.org/2005/Atom}entry") or root.findall(".//entry")

    for item in items[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or item.findtext("published") or "").strip()
        if title and link:
            topics.append(_with_scoring(TopicItem(title=title, url=link, source=source_name, published_at=_parse_dt(pub_date_raw), source_group=source_group)))

    if not items:
        for entry in entries[:max_items]:
            title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or entry.findtext("title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            if link_el is None:
                link_el = entry.find("link")
            link = (link_el.get("href") if link_el is not None and link_el.get("href") else (link_el.text if link_el is not None and link_el.text else "")).strip()
            published = (entry.findtext("{http://www.w3.org/2005/Atom}published") or entry.findtext("published") or entry.findtext("{http://www.w3.org/2005/Atom}updated") or entry.findtext("updated") or "")
            if title and link:
                topics.append(_with_scoring(TopicItem(title=title, url=link, source=source_name, published_at=_parse_dt(published), source_group=source_group)))
    return topics


def _format_parsed_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(raw: str) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        return _format_parsed_dt(parsedate_to_datetime(value))
    except Exception:
        pass

    iso_value = value.replace("Z", "+00:00")
    try:
        return _format_parsed_dt(datetime.fromisoformat(iso_value))
    except ValueError:
        return None


def _shorten(text: str, limit: int = 180) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _repo_short_name(repo_name: str) -> str:
    return repo_name.split("/")[-1].strip() if "/" in repo_name else repo_name.strip()


def build_github_topic_ru_metadata(
    repo_name: str, description: str | None = None, language: str | None = None, stars: str | None = None, stars_today: str | None = None
) -> tuple[str, str, str]:
    """Build deterministic Russian explanation for a GitHub Trending topic."""
    repo_short = _repo_short_name(repo_name) or "GitHub-проект"
    clean_description = _shorten(description or "")
    tech_bits = [bit for bit in [language, stars, stars_today] if bit]
    suffix = f" ({', '.join(tech_bits)})" if tech_bits else ""
    if _contains_cyrillic(repo_name) and not clean_description:
        title_ru = repo_name
    elif clean_description:
        title_ru = _shorten(f"{repo_short} - open-source проект: {clean_description}", 120)
    else:
        title_ru = f"{repo_short} - GitHub-проект по AI/разработке"

    if clean_description:
        summary_ru = _shorten(f"Репозиторий выглядит как AI/разработческий проект: {clean_description}{suffix}.", 220)
    else:
        summary_ru = "Похоже на GitHub-проект по AI/разработке. Лучше открыть ссылку и быстро проверить, есть ли там понятная польза для поста."
    angle_ru = "Можно подать как пример того, какие AI-инструменты и open-source проекты сейчас быстро набирают внимание у разработчиков."
    return title_ru, summary_ru, angle_ru


def _extract_github_trending_metadata(article) -> tuple[str | None, str, str | None, str | None, str | None, str | None]:
    repo_tag = article.select_one("h2 a")
    if not repo_tag:
        return None, "", None, None, None, None
    repo_name = " ".join(repo_tag.get_text(" ", strip=True).split())
    repo_path = repo_tag.get("href", "").strip()
    description_tag = article.select_one("p")
    description = " ".join(description_tag.get_text(" ", strip=True).split()) if description_tag else None
    language_tag = article.select_one('[itemprop="programmingLanguage"]')
    language = language_tag.get_text(" ", strip=True) if language_tag else None
    star_links = [a.get_text(" ", strip=True) for a in article.select('a[href$="/stargazers"]')]
    stars = star_links[0] if star_links else None
    stars_today_tag = article.select_one("span.d-inline-block.float-sm-right")
    stars_today = " ".join(stars_today_tag.get_text(" ", strip=True).split()) if stars_today_tag else None
    return repo_name, repo_path, description, language, stars, stars_today


def _fetch_github_trending_ai() -> list[TopicItem]:
    response = requests.get("https://github.com/trending", timeout=12)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    topics: list[TopicItem] = []
    for article in soup.select("article.Box-row")[:20]:
        repo_name, repo_path, description, language, stars, stars_today = _extract_github_trending_metadata(article)
        if not repo_name:
            continue
        lower = repo_name.lower() + " " + article.get_text(" ", strip=True).lower()
        if "ai" not in lower and "llm" not in lower and "model" not in lower:
            continue
        if not repo_path.startswith("/"):
            continue
        title_ru, summary_ru, angle_ru = build_github_topic_ru_metadata(repo_name, description, language, stars, stars_today)
        topics.append(_with_scoring(TopicItem(title=f"GitHub Trending: {repo_name}", url=f"https://github.com{repo_path}", source="GitHub Trending AI", published_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), source_group="github", title_ru=title_ru, summary_ru=summary_ru, angle_ru=angle_ru, original_description=description, stars_today=stars_today)))
    return topics[:8]


def collect_topics() -> list[TopicItem]:
    items, _reports = collect_topics_with_diagnostics()
    return items


def collect_topics_with_diagnostics() -> tuple[list[TopicItem], list[SourceReport]]:
    collected: list[TopicItem] = []
    reports: list[SourceReport] = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; simplify-ai-autopilot/1.0; +https://t.me/simplify_ai)"}
    grouped = [(OFFICIAL_AI_RSS, "official_ai", 8), (TECH_MEDIA_RSS, "tech_media", 8), (RU_TECH_RSS, "ru_tech", 8), (TOOLS_RSS, "tools", 8), (COMMUNITY_RSS, "community", 5)]
    custom = parse_custom_topic_feeds(os.getenv("CUSTOM_TOPIC_FEEDS"))

    for feeds, group, limit in grouped:
        for source_name, rss_url in feeds:
            try:
                response = requests.get(rss_url, timeout=12, headers=headers)
                response.raise_for_status()
                parsed = _parse_rss(response.text, source_name, group, max_items=limit)
                collected.extend(parsed)
                reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="ok" if parsed else "empty", item_count=len(parsed)))
            except Exception as exc:
                reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="error", error=str(exc)[:160]))

    for source_name, group, rss_url in custom:
        try:
            response = requests.get(rss_url, timeout=12, headers=headers)
            response.raise_for_status()
            parsed = _parse_rss(response.text, source_name, group, max_items=8)
            collected.extend(parsed)
            reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="ok" if parsed else "empty", item_count=len(parsed)))
        except Exception as exc:
            reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="error", error=str(exc)[:160]))
    try:
        github_items = _fetch_github_trending_ai()
        collected.extend(github_items)
        reports.append(SourceReport(name="GitHub Trending AI", url="https://github.com/trending", source_group="github", status="ok" if github_items else "empty", item_count=len(github_items)))
    except Exception as exc:
        reports.append(SourceReport(name="GitHub Trending AI", url="https://github.com/trending", source_group="github", status="error", error=str(exc)[:160]))
    return collected, reports
