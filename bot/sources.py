"""Collect candidate AI topics from curated public sources."""

from __future__ import annotations

import asyncio
import os
import re
import warnings
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from bs4 import MarkupResemblesLocatorWarning
from bot.config import _parse_bool_env, _parse_csv_env, _parse_int_range_env
from bot.topic_scoring import humanize_topic_reason_ru, normalize_topic_title, score_topic
from bot.topic_display import is_weak_topic_metadata
from bot.telegram_sources import fetch_telegram_channel_topics
from bot.source_normalization import normalize_source_url, normalize_telegram_channel_input


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
    status: str  # "ok", "empty", "error", "skipped"
    item_count: int = 0
    error: str = ""


OFFICIAL_AI_RSS = [("OpenAI blog", "https://openai.com/news/rss.xml"), ("Anthropic news", "https://www.anthropic.com/news/rss.xml"), ("Google AI blog", "https://blog.google/technology/ai/rss/"), ("Perplexity blog", "https://www.perplexity.ai/hub/blog/rss.xml"), ("Hugging Face blog", "https://huggingface.co/blog/feed.xml"), ("Microsoft AI blog", "https://blogs.microsoft.com/ai/feed/"), ("NVIDIA blog AI", "https://blogs.nvidia.com/blog/category/ai/feed/")]
TECH_MEDIA_RSS = [("VentureBeat AI", "https://venturebeat.com/ai/feed/"), ("The Decoder", "https://the-decoder.com/feed/"), ("MarkTechPost", "https://www.marktechpost.com/feed/"), ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"), ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"), ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"), ("Ars Technica AI", "https://arstechnica.com/ai/feed/")]
RU_TECH_RSS = [("Habr AI", "https://habr.com/ru/rss/hubs/ai/all/"), ("Habr ML", "https://habr.com/ru/rss/hub/machine_learning/"), ("Habr Dev", "https://habr.com/ru/rss/all/all/?fl=ru"), ("vc.ru technology", "https://vc.ru/rss/all"), ("Tproger", "https://tproger.ru/feed"), ("3DNews", "https://3dnews.ru/news/rss"), ("iXBT", "https://www.ixbt.com/export/news.rss")]
TOOLS_RSS = [("Product Hunt", "https://www.producthunt.com/feed")]
COMMUNITY_RSS = [("Reddit r/artificial", "https://www.reddit.com/r/artificial/.rss"), ("Reddit r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/.rss"), ("Reddit r/OpenAI", "https://www.reddit.com/r/OpenAI/.rss"), ("Reddit r/ChatGPT", "https://www.reddit.com/r/ChatGPT/.rss"), ("Reddit r/ClaudeAI", "https://www.reddit.com/r/ClaudeAI/.rss"), ("Reddit r/SideProject", "https://www.reddit.com/r/SideProject/.rss"), ("Reddit r/InternetIsBeautiful", "https://www.reddit.com/r/InternetIsBeautiful/.rss")]
VC_RU_AI_SOURCE = ("vc.ru AI", "https://vc.ru/ai", "ru_tech")
BUILTIN_SOURCE_OVERRIDES: dict[str, dict[str, str]] = {
    # key format: f"{source_type}:{normalize_source_url(url)}"
    # action=disable -> never fetch in /collect, but keep visible in inventory.
}


X_API_BASE_URL = "https://api.x.com"
X_API_TIMEOUT_SECONDS = 10
X_AI_KEYWORDS = (
    "ai",
    "artificial intelligence",
    "llm",
    "gpt",
    "model",
    "agent",
    "openai",
    "anthropic",
    "claude",
    "gemini",
    "deepseek",
    "machine learning",
    "neural",
    "генератив",
    "нейросет",
    "ии",
)
_DESCRIPTION_MAX_LEN = 1000
_BOILERPLATE_PATTERNS = [
    re.compile(r"^\s*(read more|continue reading)\b[:\s.-]*", re.IGNORECASE),
    re.compile(r"^\s*(source|via)\s*:\s*", re.IGNORECASE),
]


def reddit_sources_enabled() -> bool:
    return _parse_bool_env("ENABLE_REDDIT_SOURCES", False)


def x_sources_enabled() -> bool:
    return _parse_bool_env("ENABLE_X_SOURCES", False)


def x_source_config() -> tuple[str, list[str], int]:
    return (
        os.getenv("X_API_BEARER_TOKEN", "").strip(),
        _parse_csv_env("X_ACCOUNTS"),
        _parse_int_range_env("X_MAX_POSTS_PER_ACCOUNT", 5, 1, 20),
    )


def get_builtin_source_override(source_type: str, source_url: str) -> dict[str, str] | None:
    key = f"{source_type}:{normalize_source_url(source_url)}"
    override = BUILTIN_SOURCE_OVERRIDES.get(key)
    return dict(override) if override else None


def _has_ai_signal(text: str) -> bool:
    lowered = (text or "").casefold()
    return any(keyword in lowered for keyword in X_AI_KEYWORDS)


def _short_post_text(text: str, limit: int = 140) -> str:
    return _shorten(" ".join((text or "").split()), limit)


def _is_reply_or_repost(tweet: dict) -> bool:
    for ref in tweet.get("referenced_tweets") or []:
        if ref.get("type") in {"retweeted", "replied_to"}:
            return True
    return False


def _x_get_json(path: str, token: str, params: dict | None = None) -> dict:
    response = requests.get(
        f"{X_API_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "simplify-ai-autopilot/1.0"},
        params=params or {},
        timeout=X_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("X API returned non-object JSON")
    return data


def _x_topic_ru_metadata(username: str, text: str) -> tuple[str, str, str]:
    clean_text = _short_post_text(text, 180)
    title_ru = _shorten(f"Пост @{username} в X: {clean_text}", 120)
    summary_ru = _shorten(f"Администратор добавил @{username} как X-источник. В посте обсуждается: {clean_text}", 220)
    angle_ru = "Можно использовать как свежий сигнал из X, но перед публикацией лучше открыть пост и проверить контекст."
    return title_ru, summary_ru, angle_ru


def _tweet_to_topic(username: str, tweet: dict) -> TopicItem | None:
    tweet_id = str(tweet.get("id") or "").strip()
    text = str(tweet.get("text") or "").strip()
    if not tweet_id or len(text) < 40:
        return None
    if _is_reply_or_repost(tweet):
        return None
    # X_ACCOUNTS is a manual admin allowlist. Keep the keyword check available for
    # future non-trusted sources, but trust these configured accounts by default.
    trusted_admin_source = True
    if not trusted_admin_source and not _has_ai_signal(text):
        return None
    short_text = _short_post_text(text)
    title_ru, summary_ru, angle_ru = _x_topic_ru_metadata(username, text)
    return _with_scoring(
        TopicItem(
            title=f"X: @{username} - {short_text}",
            url=f"https://x.com/{username}/status/{tweet_id}",
            source=f"X @{username}",
            source_group="x",
            published_at=_parse_dt(str(tweet.get("created_at") or "")),
            original_description=short_text,
            title_ru=title_ru,
            summary_ru=summary_ru,
            angle_ru=angle_ru,
        )
    )


def fetch_x_topics(token: str, accounts: list[str], max_posts_per_account: int) -> tuple[list[TopicItem], list[SourceReport]]:
    topics: list[TopicItem] = []
    reports: list[SourceReport] = []
    max_posts = max(1, min(20, int(max_posts_per_account or 5)))
    for username in accounts[:20]:
        safe_username = username.strip().lstrip("@").strip()
        if not safe_username:
            continue
        source_name = f"X @{safe_username}"
        try:
            user_data = _x_get_json(f"/2/users/by/username/{safe_username}", token, params={"user.fields": "username"})
            user_id = str((user_data.get("data") or {}).get("id") or "").strip()
            api_username = str((user_data.get("data") or {}).get("username") or safe_username).strip().lstrip("@")
            if not user_id:
                raise RuntimeError("X API did not return user id")
            tweet_data = _x_get_json(
                f"/2/users/{user_id}/tweets",
                token,
                params={
                    "max_results": max_posts,
                    "tweet.fields": "created_at,public_metrics,entities,referenced_tweets",
                    "exclude": "retweets,replies",
                },
            )
            account_items: list[TopicItem] = []
            for tweet in (tweet_data.get("data") or [])[:max_posts]:
                if not isinstance(tweet, dict):
                    continue
                item = _tweet_to_topic(api_username, tweet)
                if item is not None:
                    account_items.append(item)
            topics.extend(account_items)
            reports.append(SourceReport(name=source_name, url=f"https://x.com/{safe_username}", source_group="x", status="ok" if account_items else "empty", item_count=len(account_items)))
        except Exception as exc:
            reports.append(SourceReport(name=source_name, url=f"https://x.com/{safe_username}", source_group="x", status="error", error=str(exc)[:160]))
    return topics, reports




def discover_rss_feed_url(input_url: str, timeout: int = 12) -> tuple[str | None, str]:
    url = (input_url or "").strip()
    if not url.startswith("http"):
        return None, "URL должен начинаться с http/https"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; simplify-ai-autopilot/1.0; +https://t.me/simplify_ai)"}

    def _try_feed(candidate_url: str) -> tuple[str | None, str]:
        try:
            resp = requests.get(candidate_url, timeout=timeout, headers=headers)
            resp.raise_for_status()
            parsed = _parse_rss(resp.text, "Проверка", "custom", max_items=3)
            if parsed:
                return candidate_url, ""
            return None, "Лента найдена, но в ней нет записей"
        except Exception as exc:
            return None, str(exc)[:160]

    feed_url, err = _try_feed(url)
    if feed_url:
        return feed_url, ""

    try:
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
    except Exception as exc:
        return None, f"Не удалось открыть страницу: {str(exc)[:160]}"

    content_type = (resp.headers.get("content-type") or "").lower()
    body = resp.text
    if "xml" in content_type or body.lstrip().startswith("<?xml") or "<rss" in body[:300].lower() or "<feed" in body[:300].lower():
        feed_url, err = _try_feed(url)
        if feed_url:
            return feed_url, ""

    soup = BeautifulSoup(body, "html.parser")
    link_candidates: list[str] = []
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        ltype = (link.get("type") or "").lower()
        href = (link.get("href") or "").strip()
        if "alternate" in rel and ltype in {"application/rss+xml", "application/atom+xml"} and href:
            link_candidates.append(urljoin(url, href))

    parsed_url = urlparse(url)
    base = f"{parsed_url.scheme}://{parsed_url.netloc}"
    for path in ("/feed", "/rss", "/rss.xml", "/feed.xml"):
        link_candidates.append(urljoin(base, path))

    seen = set()
    for candidate in link_candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        feed_url, _ = _try_feed(candidate)
        if feed_url:
            return feed_url, ""

    return None, "Не нашёл RSS/Atom-ленту"

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


def _normalize_description(raw: str | None, limit: int = _DESCRIPTION_MAX_LEN) -> str | None:
    if not raw:
        return None
    if "<" not in raw and "&" not in raw:
        clean = raw.strip()
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
            clean = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    clean = re.sub(r"\s+", " ", clean).strip()
    for pattern in _BOILERPLATE_PATTERNS:
        clean = pattern.sub("", clean).strip()
    if clean.endswith("..."):
        clean = clean[:-3].rstrip()
    if len(clean) < 20:
        return None
    return _shorten(clean, limit)


def _find_text_any_ns(item: ET.Element, local_name: str) -> str:
    direct = (item.findtext(local_name) or "").strip()
    if direct:
        return direct
    suffix = f"}}{local_name}"
    for child in list(item):
        if child.tag == local_name or str(child.tag).endswith(suffix):
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _extract_rss_description(item: ET.Element) -> str | None:
    candidates = [
        _find_text_any_ns(item, "description"),
        _find_text_any_ns(item, "encoded"),
        _find_text_any_ns(item, "summary"),
        _find_text_any_ns(item, "content"),
    ]
    for candidate in candidates:
        normalized = _normalize_description(candidate)
        if normalized:
            return normalized
    return None


def _extract_atom_description(entry: ET.Element) -> str | None:
    candidates = [
        entry.findtext("{http://www.w3.org/2005/Atom}summary") or entry.findtext("summary") or "",
        entry.findtext("{http://www.w3.org/2005/Atom}content") or entry.findtext("content") or "",
    ]
    for candidate in candidates:
        normalized = _normalize_description(candidate)
        if normalized:
            return normalized
    return None


def _parse_rss(xml_text: str, source_name: str, source_group: str, max_items: int = 8) -> list[TopicItem]:
    root = ET.fromstring(xml_text)
    topics: list[TopicItem] = []
    items = root.findall(".//item")
    entries = root.findall(".//{http://www.w3.org/2005/Atom}entry") or root.findall(".//entry")

    for item in items[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or item.findtext("published") or "").strip()
        description = _extract_rss_description(item)
        if title and link:
            topics.append(_with_scoring(TopicItem(title=title, url=link, source=source_name, published_at=_parse_dt(pub_date_raw), source_group=source_group, original_description=description)))

    if not items:
        for entry in entries[:max_items]:
            title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or entry.findtext("title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            if link_el is None:
                link_el = entry.find("link")
            link = (link_el.get("href") if link_el is not None and link_el.get("href") else (link_el.text if link_el is not None and link_el.text else "")).strip()
            published = (entry.findtext("{http://www.w3.org/2005/Atom}published") or entry.findtext("published") or entry.findtext("{http://www.w3.org/2005/Atom}updated") or entry.findtext("updated") or "")
            description = _extract_atom_description(entry)
            if title and link:
                topics.append(_with_scoring(TopicItem(title=title, url=link, source=source_name, published_at=_parse_dt(published), source_group=source_group, original_description=description)))
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


def _github_description_details_ru(language: str | None, stars: str | None, stars_today: str | None) -> str:
    details: list[str] = []
    if language:
        details.append(f"Формат - {language}.")

    stat_bits: list[str] = []
    if stars:
        clean_stars = stars.strip()
        stat_bits.append(clean_stars if "star" in clean_stars.casefold() else f"{clean_stars} stars")
    if stars_today:
        stat_bits.append(stars_today.strip())
    if stat_bits:
        details.append(f"На GitHub: {', '.join(stat_bits)}.")
    return " ".join(details)


def _translate_github_description_fragment_ru(text: str) -> str:
    translated = " ".join((text or "").split()).strip(" .")
    replacements = [
        (r"\bChatGPT-like\s+LLM\b", "ChatGPT-подобная LLM"),
        (r"\bLLMs\b", "LLM"),
        (r"\bAI agents\b", "AI-агенты"),
        (r"\bagents\b", "AI-агенты"),
        (r"\bAI agent\b", "AI-агент"),
        (r"\bagent\b", "AI-агент"),
        (r"\bworkflows\b", "рабочие процессы"),
        (r"\bworkflow\b", "рабочий процесс"),
        (r"\bimages\b", "изображения"),
        (r"\bimage\b", "изображения"),
        (r"\bvideos\b", "видео"),
        (r"\bvideo\b", "видео"),
        (r"\baudios\b", "аудио"),
        (r"\baudio\b", "аудио"),
        (r"\bfinancial trading\b", "финансового трейдинга"),
        (r"\bmachine learning\b", "machine learning"),
    ]
    for pattern, replacement in replacements:
        translated = re.sub(pattern, replacement, translated, flags=re.IGNORECASE)
    translated = re.sub(r"\s+", " ", translated).strip()
    return translated


def _github_description_ru(description: str) -> tuple[str | None, str | None, str | None]:
    """Return deterministic Russian title phrase, summary sentence and angle for common GitHub descriptions."""
    clean = " ".join((description or "").split()).strip()
    if not clean:
        return None, None, None

    implement_match = re.match(r"(?i)^implement\s+(.+?)(?:,?\s+step by step)?$", clean)
    if implement_match and "from scratch" in clean.casefold():
        subject = implement_match.group(1)
        subject = re.sub(r"(?i)\s+from scratch\b", "", subject).strip(" ,")
        tech_match = re.search(r"(?i)\s+in\s+([A-Za-z0-9 ._+/#-]+?)$", subject)
        tech = None
        if tech_match:
            tech = tech_match.group(1).strip()
            subject = subject[: tech_match.start()].strip(" ,")

        if re.fullmatch(r"(?i)(?:an?\s+)?ChatGPT-like\s+LLM", subject):
            tech_part = f" на {tech}" if tech else ""
            step_title = "пошаговая " if "step by step" in clean.casefold() else ""
            title_phrase = f"{step_title}сборка ChatGPT-подобной модели{tech_part}"
            summary_sentence = f"Репозиторий показывает, как с нуля собрать ChatGPT-подобную LLM{tech_part}."
            angle = "Можно подать как полезный open-source проект для тех, кто хочет понять, как LLM устроены изнутри."
            return title_phrase, summary_sentence, angle

        translated_subject = _translate_github_description_fragment_ru(re.sub(r"(?i)^an?\s+", "", subject))
        tech_part = f" на {tech}" if tech else ""
        step_title = "пошаговая " if "step by step" in clean.casefold() else ""
        title_phrase = f"{step_title}сборка {translated_subject}{tech_part} с нуля"
        summary_sentence = f"Репозиторий показывает, как с нуля собрать {translated_subject}{tech_part}."
        return title_phrase, summary_sentence, None

    tutorial_match = re.search(r"(?i)\btutorial\b", clean)
    if tutorial_match:
        translated = _translate_github_description_fragment_ru(re.sub(r"(?i)\btutorial\b", "", clean).strip(" :-"))
        title_phrase = "обучающий проект" + (f" по теме: {translated}" if translated else "")
        summary_sentence = "Репозиторий выглядит как обучающий проект" + (f" по теме: {translated}." if translated else ".")
        return title_phrase, summary_sentence, None

    framework_match = re.match(r"(?i)^(?:an?\s+)?(?:open-source\s+)?(.+?framework)\s+for\s+(.+)$", clean)
    if framework_match:
        raw_framework = framework_match.group(1)
        if re.fullmatch(r"(?i)multi-agent framework", raw_framework):
            framework = "фреймворк с AI-агентами"
        else:
            framework = _translate_github_description_fragment_ru(raw_framework)
            framework = re.sub(r"(?i)^framework$", "фреймворк", framework)
        purpose = _translate_github_description_fragment_ru(framework_match.group(2))
        title_phrase = f"{framework} для {purpose}"
        summary_sentence = f"Репозиторий выглядит как {framework} для {purpose}."
        return title_phrase, summary_sentence, None

    translated = _translate_github_description_fragment_ru(clean)
    if translated != clean:
        return translated, f"Репозиторий выглядит как проект про {translated}.", None

    return None, None, None


def build_github_topic_ru_metadata(
    repo_name: str, description: str | None = None, language: str | None = None, stars: str | None = None, stars_today: str | None = None
) -> tuple[str, str, str]:
    """Build deterministic Russian explanation for a GitHub Trending topic."""
    repo_short = _repo_short_name(repo_name) or "GitHub-проект"
    clean_description = _shorten(description or "")
    title_phrase_ru, summary_sentence_ru, angle_override = _github_description_ru(clean_description) if clean_description else (None, None, None)
    details_ru = _github_description_details_ru(language, stars, stars_today)

    if _contains_cyrillic(repo_name) and not clean_description:
        title_ru = repo_name
    elif title_phrase_ru:
        title_ru = _shorten(f"{repo_short} - {title_phrase_ru}", 120)
        if is_weak_topic_metadata(title_ru, summary_sentence_ru, angle_override, original_title=clean_description):
            title_ru = f"{repo_short} - GitHub-проект, нужен AI-перевод"
    elif clean_description:
        title_ru = f"{repo_short} - GitHub-проект, нужен AI-перевод"
    else:
        title_ru = f"{repo_short} - GitHub-проект по AI/разработке"

    if summary_sentence_ru:
        summary_ru = _shorten(" ".join(bit for bit in [summary_sentence_ru, details_ru] if bit), 260)
    elif clean_description:
        summary_ru = _shorten(
            " ".join(
                bit
                for bit in [
                    f"Репозиторий выглядит как AI/разработческий проект. Описание GitHub: {clean_description}.",
                    details_ru,
                ]
                if bit
            ),
            260,
        )
    else:
        summary_ru = "Похоже на GitHub-проект по AI/разработке. Лучше открыть ссылку и быстро проверить, есть ли там понятная польза для поста."
    angle_ru = angle_override or "Можно подать как пример того, какие AI-инструменты и open-source проекты сейчас быстро набирают внимание у разработчиков."
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




def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

def collect_topics(settings=None, db=None) -> list[TopicItem]:
    items, _reports = collect_topics_with_diagnostics(settings=settings, db=db)
    return items


def collect_topics_with_diagnostics(settings=None, db=None) -> tuple[list[TopicItem], list[SourceReport]]:
    collected: list[TopicItem] = []
    reports: list[SourceReport] = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; simplify-ai-autopilot/1.0; +https://t.me/simplify_ai)"}
    grouped = [(OFFICIAL_AI_RSS, "official_ai", 8), (TECH_MEDIA_RSS, "tech_media", 8), (RU_TECH_RSS, "ru_tech", 8), (TOOLS_RSS, "tools", 8)]
    if reddit_sources_enabled():
        grouped.append((COMMUNITY_RSS, "community", 5))
    else:
        reports.append(SourceReport(name="Reddit community RSS", url="https://www.reddit.com/*.rss", source_group="community", status="skipped", error="Reddit sources disabled by config (ENABLE_REDDIT_SOURCES=false)"))
    custom = parse_custom_topic_feeds(os.getenv("CUSTOM_TOPIC_FEEDS"))

    def _skip_if_needed(source_type: str, source_name: str, source_group: str, source_url: str):
        if db is None:
            return False, ""
        key = normalize_source_url(source_url) if source_type in {"rss", "html"} else source_url.strip().lower()
        return db.should_skip_source(source_type, key)

    def _record(source_type: str, source_name: str, source_group: str, source_url: str, status: str, error: str = ""):
        if db is None:
            return
        key = normalize_source_url(source_url) if source_type in {"rss", "html"} else source_url.strip().lower()
        db.record_source_health(source_type, key, source_name, source_group, status, error)

    for feeds, group, limit in grouped:
        for source_name, rss_url in feeds:
            override = get_builtin_source_override("rss", rss_url)
            if override and override.get("action") == "disable":
                reason = override.get("reason", "Отключён как проблемный встроенный источник.")
                reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="skipped", error=f"Источник отключён: {reason}"))
                _record("rss", source_name, group, rss_url, "skipped", reason)
                continue
            should_skip, reason = _skip_if_needed("rss", source_name, group, rss_url)
            if should_skip:
                reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="skipped", error=f"Источник временно на паузе: {reason}"))
                _record("rss", source_name, group, rss_url, "skipped", reason)
                continue
            try:
                response = requests.get(rss_url, timeout=12, headers=headers)
                response.raise_for_status()
                parsed = _parse_rss(response.text, source_name, group, max_items=limit)
                collected.extend(parsed)
                reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="ok" if parsed else "empty", item_count=len(parsed)))
                _record("rss", source_name, group, rss_url, "ok" if parsed else "empty")
            except Exception as exc:
                reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="error", error=str(exc)[:160]))
                _record("rss", source_name, group, rss_url, "error", str(exc))

    managed_rows = db.list_managed_sources(include_disabled=False) if db is not None else []
    for row in managed_rows:
        if str(row["source_type"]) != "rss":
            continue
        source_name = str(row["name"])
        group = str(row["source_group"] or "custom")
        rss_url = str(row["value"])
        should_skip, reason = _skip_if_needed("rss", source_name, group, rss_url)
        if should_skip:
            reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="skipped", error=f"Источник временно на паузе: {reason}"))
            _record("rss", source_name, group, rss_url, "skipped", reason)
            continue
        try:
            response = requests.get(rss_url, timeout=12, headers=headers)
            response.raise_for_status()
            parsed = _parse_rss(response.text, source_name, group, max_items=8)
            collected.extend(parsed)
            reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="ok" if parsed else "empty", item_count=len(parsed)))
            if db is not None:
                db.update_managed_source_status(int(row["id"]), "ok" if parsed else "empty", "")
        except Exception as exc:
            reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="error", error=str(exc)[:160]))
            if db is not None:
                db.update_managed_source_status(int(row["id"]), "error", str(exc)[:160])

    for source_name, group, rss_url in custom:
        should_skip, reason = _skip_if_needed("rss", source_name, group, rss_url)
        if should_skip:
            reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="skipped", error=f"Источник временно на паузе: {reason}"))
            _record("rss", source_name, group, rss_url, "skipped", reason)
            continue
        try:
            response = requests.get(rss_url, timeout=12, headers=headers)
            response.raise_for_status()
            parsed = _parse_rss(response.text, source_name, group, max_items=8)
            collected.extend(parsed)
            reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="ok" if parsed else "empty", item_count=len(parsed)))
        except Exception as exc:
            reports.append(SourceReport(name=source_name, url=rss_url, source_group=group, status="error", error=str(exc)[:160]))
            _record("rss", source_name, group, rss_url, "error", str(exc))
    vc_name, vc_url, vc_group = VC_RU_AI_SOURCE
    vc_override = get_builtin_source_override("html", vc_url)
    if vc_override and vc_override.get("action") == "disable":
        reason = vc_override.get("reason", "Отключён как проблемный встроенный источник.")
        reports.append(SourceReport(name=vc_name, url=vc_url, source_group=vc_group, status="skipped", error=f"Источник отключён: {reason}"))
        _record("html", vc_name, vc_group, vc_url, "skipped", reason)
    else:
        should_skip, reason = _skip_if_needed("html", vc_name, vc_group, vc_url)
        if should_skip:
            reports.append(SourceReport(name=vc_name, url=vc_url, source_group=vc_group, status="skipped", error=f"Источник временно на паузе: {reason}"))
            _record("html", vc_name, vc_group, vc_url, "skipped", reason)
        else:
            vc_items, vc_report = fetch_vc_ru_ai_topics()
            collected.extend(vc_items)
            reports.append(vc_report)
            _record("html", vc_name, vc_group, vc_url, vc_report.status, vc_report.error)
    if x_sources_enabled():
        x_token, x_accounts, x_max_posts = x_source_config()
        if not x_token or not x_accounts:
            missing = []
            if not x_token:
                missing.append("X_API_BEARER_TOKEN")
            if not x_accounts:
                missing.append("X_ACCOUNTS")
            reports.append(SourceReport(name="X API", url="https://api.x.com/2", source_group="x", status="skipped", error=f"X sources enabled but missing: {', '.join(missing)}"))
        else:
            x_items, x_reports = fetch_x_topics(x_token, x_accounts, x_max_posts)
            collected.extend(x_items)
            reports.extend(x_reports)
    try:
        from bot.config import load_settings

        active_settings = settings or load_settings()
        telegram_channels = []
        if db is not None:
            for row in db.list_managed_sources(include_disabled=False):
                if row["source_type"] == "telegram":
                    telegram_channels.append(str(row["value"]))
        telegram_items, telegram_reports = _run_async(fetch_telegram_channel_topics(active_settings, extra_channels=telegram_channels))
        collected.extend(telegram_items)
        reports.extend(telegram_reports)
    except Exception as exc:
        reports.append(SourceReport(name="Telegram channels", url="https://t.me", source_group="telegram", status="error", error=str(exc)[:160]))
    try:
        github_items = _fetch_github_trending_ai()
        collected.extend(github_items)
        reports.append(SourceReport(name="GitHub Trending AI", url="https://github.com/trending", source_group="github", status="ok" if github_items else "empty", item_count=len(github_items)))
    except Exception as exc:
        reports.append(SourceReport(name="GitHub Trending AI", url="https://github.com/trending", source_group="github", status="error", error=str(exc)[:160]))
    return collected, reports


def fetch_vc_ru_ai_topics(max_items: int = 20) -> tuple[list[TopicItem], SourceReport]:
    name, url, group = VC_RU_AI_SOURCE
    headers = {"User-Agent": "Mozilla/5.0 (compatible; simplify-ai-autopilot/1.0; +https://t.me/simplify_ai)"}
    try:
        resp = requests.get(url, timeout=12, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        topics: list[TopicItem] = []
        seen: set[str] = set()
        for link in soup.select("a[href]"):
            href = (link.get("href") or "").strip()
            full = urljoin(url, href)
            p = urlparse(full)
            if p.netloc != "vc.ru":
                continue
            if not re.match(r"^/[\w-]+/\d+", p.path):
                continue
            if any(x in full for x in ["/u/", "/tag/", "#comments", "/subscribe", "/services"]):
                continue
            title = " ".join(link.get_text(" ", strip=True).split())
            if len(title) < 12:
                continue
            if full in seen:
                continue
            seen.add(full)
            snippet_tag = link.find_parent().find_next("p") if link.find_parent() else None
            snippet = " ".join(snippet_tag.get_text(" ", strip=True).split()) if snippet_tag else title
            topics.append(_with_scoring(TopicItem(title=title, url=full, source=name, source_group=group, original_description=snippet)))
            if len(topics) >= max_items:
                break
        status = "ok" if topics else "empty"
        return topics, SourceReport(name=name, url=url, source_group=group, status=status, item_count=len(topics))
    except Exception as exc:
        return [], SourceReport(name=name, url=url, source_group=group, status="error", error=str(exc)[:160])
