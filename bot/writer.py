"""OpenAI-powered draft generation for Telegram posts."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from pathlib import Path

from openai import OpenAI

STYLE_PATH = Path("prompts/post_style.md")
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


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
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))
    return normalized.rstrip("/")


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
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    title = (soup.title.string or "").strip() if soup.title else ""
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    if not text:
        raise ValueError("Не удалось извлечь текст страницы.")

    return title or "Без заголовка", text[:12000]


def generate_post_draft_from_page(
    api_key: str, source_url: str, title: str, page_text: str
) -> str:
    client = OpenAI(api_key=api_key)
    style = _load_style_prompt()

    user_prompt = (
        "Ниже ссылка и текст страницы. "
        "Сначала коротко суммаризуй материал для себя, затем на основе summary создай один готовый пост в стиле @simplify_ai. "
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
