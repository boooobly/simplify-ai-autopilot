"""AI-powered draft generation for Telegram posts."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

logger = logging.getLogger(__name__)
STYLE_PATH = Path("prompts/post_style.md")
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "ref", "ref_src",
}
NOISE_PATTERNS = ["cookie", "cookies", "accept all", "subscribe", "sign up", "newsletter", "privacy policy", "terms of use", "реклама", "подписаться", "принять", "куки", "политика конфиденциальности"]
ARTICLE_SELECTORS = ["article", "main", '[role="main"]', ".post", ".article", ".entry-content", ".post-content", ".content"]

class EmptyAIResponseError(RuntimeError):
    pass


def _has_meaningful_body(text: str, source_url: str | None = None) -> bool:
    cleaned = text.strip()
    if source_url:
        cleaned = cleaned.replace(f"Источник: {source_url}", "")
    cleaned = "\n".join(
        line for line in cleaned.splitlines() if not line.strip().startswith("Источник:")
    ).strip()
    return len(cleaned) >= 40


def _load_style_prompt() -> str:
    return STYLE_PATH.read_text(encoding="utf-8").strip()


def _build_client(api_key: str, base_url: str | None = None) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)


def _generate_with_chat_completion(
    api_key: str,
    model: str,
    user_prompt: str,
    system_prompt: str,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    client = _build_client(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=900,
        extra_headers=extra_headers,
    )
    choice = response.choices[0]
    finish_reason = choice.finish_reason
    message_content = choice.message.content
    content = ""
    if isinstance(message_content, str):
        content = message_content
    elif isinstance(message_content, list):
        text_parts: list[str] = []
        for part in message_content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                elif isinstance(part.get("content"), str):
                    text_parts.append(part["content"])
            elif hasattr(part, "text") and isinstance(part.text, str):
                text_parts.append(part.text)
        content = "\n".join(text_parts)
    elif message_content is not None:
        content = str(message_content)
    stripped = content.strip()
    logger.info(
        "AI completion received: model=%s finish_reason=%s text_length=%s",
        model,
        finish_reason,
        len(stripped),
    )
    if not stripped:
        raise EmptyAIResponseError("AI model returned empty content")
    return stripped


def _limit_text_preserving_source(text: str, source_url: str | None = None, limit: int = 900) -> str:
    if len(text) <= limit:
        return text
    if not source_url:
        return text[: limit - 3].rstrip() + "..."
    source_line = f"Источник: {source_url}"
    if len(source_line) >= limit:
        return source_line[:limit]
    suffix = f"\n\n{source_line}"
    body_limit = limit - len(suffix) - 3
    if body_limit <= 0:
        return source_line
    body = text.replace(source_line, "").strip()
    return body[:body_limit].rstrip() + "..." + suffix


def generate_post_draft(
    api_key: str,
    model: str,
    source_url: str | None = None,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    style = _load_style_prompt()
    source_line = f"Источник: {source_url}" if source_url else "Источник: не указан"
    user_prompt = (
        "Создай один черновик поста для Telegram-канала @simplify_ai. "
        "Верни только готовый текст поста, без пояснений, без markdown-блока и без служебных комментариев. "
        "Длина — до 900 символов. "
        f"{source_line}"
    )
    logger.info("Генерация черновика: model=%s", model)
    text = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_preserving_source(text, source_url=source_url)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    return final_text


def polish_post_draft(
    api_key: str,
    model: str,
    draft_text: str,
    source_url: str | None = None,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    style = _load_style_prompt()
    source_line = f"Источник: {source_url}" if source_url else "Источник: не указан"
    user_prompt = (
        "Улучши черновик для @simplify_ai. Сохрани простой человеческий тон, как у реального автора Telegram-канала. "
        "Сделай текст яснее и живее, но не делай его стерильным или корпоративным. "
        "Не меняй факты и не добавляй новые факты. Не перегружай объяснениями. "
        "Сохрани строку с источником в тексте. Верни только финальный текст до 900 символов. "
        "Без AI-клише, без эм-даша, без кавычек-ёлочек. "
        "Избегай штампов: 'не про..., а про...', 'главный вывод простой', 'важно отметить', 'давайте разберем', 'в заключение'.\n\n"
        f"{source_line}\n\n"
        f"Текущий черновик:\n{draft_text}"
    )
    logger.info("Полировка черновика: model=%s", model)
    text = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_preserving_source(text, source_url=source_url)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    return final_text


def find_first_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip(".,!?:;)") if match else None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    filtered_query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS], doseq=True)
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
        if normalized in seen or _is_noisy_line(line):
            continue
        seen.add(normalized)
        cleaned.append(line)
    return "\n".join(cleaned).strip()[:12000]


def fetch_page_content(source_url: str, timeout_seconds: int = 12) -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; simplify-ai-autopilot/1.0; +https://t.me/simplify_ai)"}
    response = requests.get(source_url, timeout=timeout_seconds, headers=headers)
    response.raise_for_status()
    if "text/html" not in response.headers.get("Content-Type", ""):
        raise ValueError("URL не содержит HTML-страницу.")
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "button", "input", "nav", "footer", "header", "aside"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    if not title:
        og_title = soup.find("meta", property="og:title")
        tw_title = soup.find("meta", attrs={"name": "twitter:title"})
        title = (og_title.get("content", "") if og_title else "").strip() or (tw_title.get("content", "") if tw_title else "").strip() or "Без заголовка"
    container = next((soup.select_one(sel) for sel in ARTICLE_SELECTORS if soup.select_one(sel)), None) or soup.body or soup
    text = _clean_lines(container.get_text("\n").splitlines())
    if len(text) < 700:
        raise ValueError("На странице слишком мало полезного текста.")
    return title, text


def generate_post_draft_from_page(api_key: str, model: str, source_url: str, title: str, page_text: str, base_url: str | None = None, extra_headers: dict[str, str] | None = None) -> str:
    style = _load_style_prompt()
    user_prompt = (
        "Ниже ссылка и извлечённый текст страницы. Опирайся только на этот текст страницы. "
        "Не выдумывай факты, если их нет в тексте. "
        "Сделай один готовый пост в стиле @simplify_ai до 900 символов. "
        "Структура: короткий заголовок с emoji, 1-2 простых вводных предложения, 2-4 коротких пункта с символом ➖ (если уместно), практический смысл простыми словами, короткая финальная мысль с 💭 (когда уместно). "
        "Пиши живо и по-человечески: без сухого пресс-релизного стиля, без корпоративного тона, без AI-клише. "
        "Не используй фразы: 'не про..., а про...', 'главный вывод простой', 'важно отметить', 'давайте разберем', 'в заключение'. "
        "Не используй эм-даш и кавычки-ёлочки. "
        "Не оставляй ответ пустым: даже если статья слабая, сделай осторожный короткий пост только по подтверждённым фактам. "
        "В конце обязательно добавь строку: Источник: <source_url>. "
        "Верни только финальный текст поста без комментариев и без markdown-блоков.\n\n"
        f"Источник: {source_url}\nЗаголовок: {title}\n\nТекст страницы:\n{page_text}"
    )
    logger.info("Генерация по URL: model=%s", model)
    text = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_preserving_source(text, source_url=source_url)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    return final_text
