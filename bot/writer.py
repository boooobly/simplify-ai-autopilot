"""OpenAI-powered draft generation for Telegram posts."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from pathlib import Path

from openai import OpenAI

STYLE_PATH = Path("prompts/post_style.md")
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}
NOISE_PATTERNS = [
    "cookie",
    "cookies",
    "accept all",
    "subscribe",
    "sign up",
    "newsletter",
    "privacy policy",
    "terms of use",
    "реклама",
    "подписаться",
    "принять",
    "куки",
    "политика конфиденциальности",
]
ARTICLE_SELECTORS = [
    "article",
    "main",
    '[role="main"]',
    ".post",
    ".article",
    ".entry-content",
    ".post-content",
    ".content",
]


def _load_style_prompt() -> str:
    return STYLE_PATH.read_text(encoding="utf-8").strip()


def generate_post_draft(api_key: str, source_url: str | None = None) -> str:
    """Generate a Russian Telegram post draft for @simplify_ai."""

    client = OpenAI(api_key=api_key)
    style = _load_style_prompt()

    source_line = f"Источник: {source_url}" if source_url else "Источник: не указан"
    user_prompt = (
        "Создай один черновик поста для Telegram-канала @simplify_ai. "
        "Верни только готовый текст поста, без пояснений, без markdown-блока и без служебных комментариев. "
        f"{source_line}"
    )

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": style},
            {"role": "user", "content": user_prompt},
        ],
        max_output_tokens=700,
    )

    text = response.output_text.strip()
    if len(text) > 900:
        text = text[:897].rstrip() + "..."
    return text


def find_first_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip(".,!?:;)") if match else None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    filtered_query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS],
        doseq=True,
    )
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, filtered_query, ""))
    return normalized.rstrip("/")


def _is_noisy_line(line: str) -> bool:
    lowered = line.lower()
    if any(pattern in lowered for pattern in NOISE_PATTERNS):
        return True
    if len(line) < 25 and not re.search(r"[0-9]", line):
        useful_tokens = ("ai", "ml", "llm", "openai", "anthropic", "google", "meta", "nvidia", "gpt", "api")
        if not any(token in lowered for token in useful_tokens):
            return True
    return False


def _clean_lines(lines: list[str]) -> str:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in lines:
        line = " ".join(raw.split())
        if not line:
            continue
        normalized = line.casefold()
        if normalized in seen:
            continue
        if _is_noisy_line(line):
            continue
        seen.add(normalized)
        cleaned.append(line)
    text = "\n".join(cleaned).strip()
    return text[:12000]


def fetch_page_content(source_url: str, timeout_seconds: int = 12) -> tuple[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; simplify-ai-autopilot/1.0; +https://t.me/simplify_ai)"
        )
    }
    response = requests.get(source_url, timeout=timeout_seconds, headers=headers)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        raise ValueError("URL не содержит HTML-страницу.")

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(
        ["script", "style", "noscript", "svg", "iframe", "form", "button", "input", "nav", "footer", "header", "aside"]
    ):
        tag.decompose()

    title = (soup.title.string or "").strip() if soup.title else ""
    if not title:
        og_title = soup.find("meta", property="og:title")
        tw_title = soup.find("meta", attrs={"name": "twitter:title"})
        title = (og_title.get("content", "") if og_title else "").strip() or (tw_title.get("content", "") if tw_title else "").strip()
    if not title:
        title = "Без заголовка"

    container = None
    for selector in ARTICLE_SELECTORS:
        found = soup.select_one(selector)
        if found:
            container = found
            break
    if container is None:
        container = soup.body or soup

    raw_lines = container.get_text("\n").splitlines()
    text = _clean_lines(raw_lines)
    if len(text) < 700:
        raise ValueError("На странице слишком мало полезного текста.")

    return title, text


def generate_post_draft_from_page(
    api_key: str, source_url: str, title: str, page_text: str
) -> str:
    client = OpenAI(api_key=api_key)
    style = _load_style_prompt()

    user_prompt = (
        "Ниже ссылка и извлечённый текст страницы. "
        "Опирайся только на этот текст страницы. "
        "Если факт не подтверждается текстом, не выдумывай его. "
        "Сделай один готовый пост в стиле @simplify_ai длиной до 900 символов. "
        "В конце обязательно добавь строку: Источник: <source_url>. "
        "Верни только финальный текст поста без комментариев и без markdown-блоков.\n\n"
        f"Источник: {source_url}\n"
        f"Заголовок: {title}\n\n"
        f"Текст страницы:\n{page_text}"
    )

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": style},
            {"role": "user", "content": user_prompt},
        ],
        max_output_tokens=900,
    )

    text = response.output_text.strip()
    if len(text) > 900:
        text = text[:897].rstrip() + "..."
    return text
