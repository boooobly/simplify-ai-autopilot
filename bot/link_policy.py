"""Deterministic CTA link policy for generated Telegram drafts."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

LINK_MARKER_PATTERN = re.compile(r"\[\[LINK:(.+?)\|(.+?)\]\]")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")

NEWS_DOMAINS = {
    "techcrunch.com",
    "www.techcrunch.com",
    "theverge.com",
    "www.theverge.com",
    "venturebeat.com",
    "www.venturebeat.com",
    "habr.com",
    "www.habr.com",
    "vc.ru",
    "www.vc.ru",
}
NEWS_PATH_PARTS = {
    "news",
    "blog",
    "blogs",
    "press",
    "press-release",
    "press-releases",
    "newsroom",
    "announcements",
    "announcement",
    "article",
    "articles",
    "media",
    "stories",
}
TESTABLE_PATH_PARTS = {
    "docs",
    "doc",
    "documentation",
    "install",
    "installation",
    "quickstart",
    "getting-started",
    "guide",
    "guides",
    "tutorial",
    "tutorials",
    "demo",
    "demos",
    "examples",
    "example",
    "download",
    "downloads",
    "app",
    "apps",
    "tools",
    "tool",
    "playground",
    "api",
}
TESTABLE_CATEGORIES = {
    "tool",
    "tools",
    "app",
    "apps",
    "service",
    "services",
    "repo",
    "repos",
    "repository",
    "repositories",
    "open_source",
    "opensource",
    "github",
    "demo",
    "guide",
    "docs",
    "model",
    "models",
    "product",
    "products",
}
NEWS_CATEGORIES = {
    "news",
    "article",
    "articles",
    "blog",
    "blogs",
    "press",
    "press_release",
    "press-release",
    "announcement",
    "announcements",
    "media",
    "coverage",
}
DIRECT_PRODUCT_TITLE_HINTS = (
    " tool",
    " service",
    " app",
    " demo",
    " playground",
    " repository",
    " repo",
    " github",
    " hugging face",
    " model",
)


def _host(url: str) -> str:
    return urlsplit(url.strip()).netloc.lower().removeprefix("www.")


def _path_parts(url: str) -> list[str]:
    return [part.lower() for part in urlsplit(url.strip()).path.split("/") if part]


def _same_url(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left_parts = urlsplit(left.strip())
    right_parts = urlsplit(right.strip())
    return (
        left_parts.scheme.lower(),
        left_parts.netloc.lower().removeprefix("www."),
        left_parts.path.rstrip("/"),
    ) == (
        right_parts.scheme.lower(),
        right_parts.netloc.lower().removeprefix("www."),
        right_parts.path.rstrip("/"),
    )


def _is_github_repo(host: str, parts: list[str]) -> bool:
    return host == "github.com" and len(parts) >= 2 and parts[0] not in {"topics", "trending", "marketplace", "features", "about"}


def _is_huggingface_repo(host: str, parts: list[str]) -> bool:
    if host != "huggingface.co" or not parts:
        return False
    if parts[0] in {"blog", "docs", "pricing", "enterprise", "tasks", "models", "spaces", "datasets"}:
        return len(parts) >= 2 and parts[0] in {"models", "spaces", "datasets", "docs"}
    return len(parts) >= 2


def _is_product_hunt_product(host: str, parts: list[str]) -> bool:
    return host == "producthunt.com" and len(parts) >= 2 and parts[0] in {"products", "posts"}


def _looks_like_news_or_blog(url: str, source_group: str | None = None, category: str | None = None) -> bool:
    host = _host(url)
    parts = set(_path_parts(url))
    source_group_value = (source_group or "").strip().lower()
    category_value = (category or "").strip().lower()
    if host in {domain.removeprefix("www.") for domain in NEWS_DOMAINS}:
        return True
    if source_group_value in {"tech_media", "news", "media", "blog"}:
        return True
    if category_value in NEWS_CATEGORIES:
        return True
    return bool(parts & NEWS_PATH_PARTS)


def is_testable_cta_url(
    url: str,
    source_group: str | None = None,
    category: str | None = None,
    title: str | None = None,
) -> bool:
    """Return True only when a CTA URL lets readers test/use something."""

    parsed = urlsplit((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    host = parsed.netloc.lower().removeprefix("www.")
    parts = _path_parts(url)
    category_value = (category or "").strip().lower()
    title_value = f" {(title or '').strip().lower()} "

    if _is_github_repo(host, parts):
        return True
    if _is_huggingface_repo(host, parts):
        return True
    if _is_product_hunt_product(host, parts):
        return True

    if _looks_like_news_or_blog(url, source_group=source_group, category=category):
        return False

    if set(parts) & TESTABLE_PATH_PARTS:
        return True

    if category_value in TESTABLE_CATEGORIES and len(parts) <= 2:
        return True

    return any(hint in title_value for hint in DIRECT_PRODUCT_TITLE_HINTS) and len(parts) <= 2


def _line_urls(line: str) -> list[str]:
    urls = [match.group(2).strip() for match in LINK_MARKER_PATTERN.finditer(line)]
    urls.extend(match.group(2).strip() for match in MARKDOWN_LINK_PATTERN.finditer(line))
    return urls


def strip_disallowed_cta_links(
    text: str,
    source_url: str | None = None,
    source_group: str | None = None,
    category: str | None = None,
    title: str | None = None,
) -> str:
    """Remove generated CTA link lines unless every CTA URL is testable/useful."""

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        urls = _line_urls(line)
        if urls:
            if not all(
                is_testable_cta_url(url, source_group=source_group, category=category, title=title)
                for url in urls
            ):
                continue
            # If the only reason for a CTA is a non-testable source_url, it is removed
            # by the URL policy above; testable source URLs (repos, demos, docs) survive.
            if source_url and all(_same_url(url, source_url) for url in urls) and not is_testable_cta_url(
                source_url,
                source_group=source_group,
                category=category,
                title=title,
            ):
                continue
        cleaned_lines.append(line)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()
