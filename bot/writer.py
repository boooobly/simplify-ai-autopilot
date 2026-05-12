"""AI-powered draft generation for Telegram posts."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from bot.style_guide import HUMANIZER_RULES_FOR_SIMPLIFY_AI, SIMPLIFY_AI_EMOJI_ALIAS_GUIDE, SIMPLIFY_AI_STYLE_GUIDE

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


@dataclass
class GenerationResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""


def _strip_source_lines(text: str) -> str:
    filtered = [
        line
        for line in text.splitlines()
        if not line.strip().startswith("Источник:") and not line.strip().startswith("Source:")
    ]
    cleaned = "\n".join(filtered).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _has_meaningful_body(text: str, source_url: str | None = None) -> bool:
    cleaned = _strip_source_lines(text).strip()
    if source_url:
        cleaned = cleaned.replace(source_url, "")
    return len(cleaned.strip()) >= 40


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
    max_tokens: int = 900,
) -> GenerationResult:
    client = _build_client(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
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
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    stripped = content.strip()
    logger.info(
        "AI completion received: model=%s finish_reason=%s text_length=%s",
        model,
        finish_reason,
        len(stripped),
    )
    if not stripped:
        raise EmptyAIResponseError("AI model returned empty content")
    return GenerationResult(
        content=stripped,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        model=model,
    )


def translate_topic_title_to_ru(
    *,
    api_key: str,
    model: str,
    title: str,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> GenerationResult | None:
    """Translate only a short topic title to Russian; return None on safe fallback."""
    clean_title = title.strip()
    if not clean_title:
        return None
    if any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in clean_title):
        return GenerationResult(content=clean_title, model=model)
    system_prompt = (
        "Translate a topic title into short natural Russian. Preserve product names, "
        "model names, company names, version numbers and brand names. Do not add facts. "
        "Return only the translated title."
    )
    user_prompt = clean_title[:300]
    try:
        result = _generate_with_chat_completion(
            api_key=api_key,
            model=model,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            base_url=base_url,
            extra_headers=extra_headers,
            max_tokens=80,
        )
    except Exception as exc:
        logger.warning("Topic title translation failed: %s", exc)
        return None
    translated = result.content.strip().strip('\"“”')
    if not translated:
        return None
    result.content = translated.replace("\n", " ")[:300]
    return result


def _limit_text_safely(text: str, limit: int) -> str:
    prepared = text.rstrip()
    if len(prepared) <= limit:
        return prepared

    cut_zone = prepared[:limit].rstrip()

    paragraph_idx = cut_zone.rfind("\n\n")
    if paragraph_idx >= max(0, int(limit * 0.55)):
        candidate = cut_zone[:paragraph_idx].rstrip()
    else:
        sentence_points = [cut_zone.rfind(mark) for mark in (". ", "! ", "? ", ".\n", "!\n", "?\n")]
        sentence_idx = max(sentence_points)
        if sentence_idx >= max(0, int(limit * 0.45)):
            candidate = cut_zone[: sentence_idx + 1].rstrip()
        else:
            word_idx = cut_zone.rfind(" ")
            if word_idx >= max(0, int(limit * 0.35)):
                candidate = cut_zone[:word_idx].rstrip()
            else:
                candidate = cut_zone.rstrip()

    if not candidate:
        candidate = cut_zone.rstrip()
    candidate = _repair_quote_markers(candidate)
    return candidate + "..."




def _repair_quote_markers(text: str) -> str:
    open_marker = "[[QUOTE]]"
    close_marker = "[[/QUOTE]]"
    open_count = text.count(open_marker)
    close_count = text.count(close_marker)
    if open_count == close_count:
        return text
    if open_count > close_count:
        last_open = text.rfind(open_marker)
        last_close = text.rfind(close_marker)
        if last_open > last_close:
            return text[:last_open].rstrip()
        return text + close_marker
    while close_count > open_count:
        idx = text.find(close_marker)
        if idx < 0:
            break
        text = (text[:idx] + text[idx + len(close_marker):]).strip()
        close_count -= 1
    return text


def _select_title_alias(text: str, source_url: str | None = None, title: str | None = None) -> str | None:
    haystack = " ".join(part for part in [title or "", text, source_url or ""]).lower()
    keyword_aliases: list[tuple[tuple[str, ...], str]] = [
        (("claude", "anthropic"), "claude"),
        (("chatgpt", "openai", "gpt"), "chatgpt"),
        (("deepseek",), "deepseek"),
        (("google", "gemini", "deepmind"), "google"),
        (("github", "repo", "repository", "open-source repo"), "github"),
        (("photoshop", "adobe"), "photoshop"),
        (("windows", "microsoft windows"), "windows"),
        (("telegram", "bot", "bots", "channel", "channels"), "telegram"),
        (("vpn", "privacy", "security", "leak", "leaks", "приватность", "безопасность", "утечка"), "lock"),
        (("model", "llm", "tokens", "context", "open-source model", "weights", "hugging face", "minimax", "qwen", "llama", "mistral", "модель", "токен", "контекст", "веса"), "screen_card"),
        (("website", "web", "browser", "site", "сайт", "веб", "браузер"), "web"),
        (("trend", "viral", "hot", "сильное обновление", "тренд"), "fire"),
    ]
    for keywords, alias in keyword_aliases:
        if any(keyword in haystack for keyword in keywords):
            return alias
    return None


def _ensure_custom_emoji_markers(text: str, source_url: str | None = None, title: str | None = None) -> str:
    lines = text.splitlines()
    alias = _select_title_alias(text=text, source_url=source_url, title=title)
    first_non_empty_idx = next((idx for idx, line in enumerate(lines) if line.strip()), None)
    if (
        first_non_empty_idx is not None
        and alias
        and "[[EMOJI:" not in lines[first_non_empty_idx]
        and "[[LINK:" not in lines[first_non_empty_idx]
    ):
        lines[first_non_empty_idx] = re.sub(r"^\s*[^\w\s]\s*", "", lines[first_non_empty_idx], count=1).strip()
        lines[first_non_empty_idx] = f"[[EMOJI:{alias}]] {lines[first_non_empty_idx]}".strip()

    thought_like = ("💭", "🤔", "🧠")
    link_like = ("🔗", "🧾", "🌐", "📎")
    warn_like = ("❗️", "❗")
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(thought_like):
            lines[idx] = re.sub(r"^(\s*)(💭|🤔|🧠)\s*", r"\1[[EMOJI:thought]] ", line, count=1)
        if "[[LINK:" in line and "[[EMOJI:" not in line:
            if stripped.startswith(link_like):
                lines[idx] = re.sub(r"^(\s*)(🔗|🧾|🌐|📎)\s*", r"\1[[EMOJI:link]] ", line, count=1)
            else:
                indent = line[: len(line) - len(stripped)]
                lines[idx] = f"{indent}[[EMOJI:link]] {stripped}"
        if stripped.startswith(warn_like):
            lowered = stripped.lower()
            if any(token in lowered for token in ("warning", "risk", "limitation", "огранич", "риск", "предупреж")):
                lines[idx] = re.sub(r"^(\s*)(❗️|❗)\s*", r"\1[[EMOJI:alert]] ", line, count=1)
    return "\n".join(lines).strip()

def generate_post_draft(
    api_key: str,
    model: str,
    source_url: str | None = None,
    max_chars: int = 1400,
    soft_chars: int = 1100,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> GenerationResult:
    style = _load_style_prompt() + "\n\n" + SIMPLIFY_AI_STYLE_GUIDE + "\n\n" + SIMPLIFY_AI_EMOJI_ALIAS_GUIDE
    source_context = source_url or "не указан"
    user_prompt = (
        "Создай один черновик поста для Telegram-канала @simplify_ai. "
        "Соблюдай стиль-гайд ниже как основные правила. "
        "Верни только готовый текст поста, без пояснений, без markdown-блока и без служебных комментариев. "
        f"Желательная длина до {soft_chars} символов. Жёсткий максимум {max_chars} символов. Не обрывай мысль. "
        "Не добавляй строку Источник в сам пост. Ссылка хранится отдельно в модерации. "
        "Для заголовка, CTA и финальной мысли используй custom emoji aliases через [[EMOJI:alias]], а не raw emoji. "
        "Если есть подходящий alias, не используй обычные emoji. Для финальной мысли используй [[EMOJI:thought]], для CTA-строки [[EMOJI:link]]. "
        "Для generic AI/tool/model news используй [[EMOJI:screen_card]] или [[EMOJI:fire]]; для MiniMax/Mistral/Qwen/Llama без отдельного alias используй [[EMOJI:screen_card]]. "
        "Используй [[EMOJI:claude]] для Claude/Anthropic, [[EMOJI:chatgpt]] для ChatGPT/OpenAI/GPT, [[EMOJI:deepseek]] для DeepSeek, [[EMOJI:google]] для Google/Gemini/DeepMind. "
        "Для GitHub используй [[EMOJI:github]], для Photoshop/Adobe [[EMOJI:photoshop]], для Windows [[EMOJI:windows]], для Telegram [[EMOJI:telegram]], для privacy/security/VPN [[EMOJI:lock]], для web/services [[EMOJI:web]]. "
        "Если alias явно не подходит, можно использовать обычный emoji или не использовать emoji. "
        f"Источник (контекст модерации): {source_context}."
    )
    logger.info("Генерация черновика: model=%s", model)
    result = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_safely(_strip_source_lines(result.content), limit=max_chars)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    result.content = _ensure_custom_emoji_markers(final_text, source_url=source_url, title=None)
    return result


def polish_post_draft(
    api_key: str,
    model: str,
    draft_text: str,
    source_url: str | None = None,
    max_chars: int = 1400,
    soft_chars: int = 1100,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> GenerationResult:
    style = _load_style_prompt() + "\n\n" + SIMPLIFY_AI_STYLE_GUIDE + "\n\n" + SIMPLIFY_AI_EMOJI_ALIAS_GUIDE
    user_prompt = (
        "Улучши черновик для @simplify_ai. Сохрани простой человеческий тон, как у реального автора Telegram-канала. "
        "Сделай текст яснее и живее, но не делай его стерильным или корпоративным. "
        "Не меняй факты и не добавляй новые факты. Не перегружай объяснениями. "
        "Не добавляй строку Источник в сам пост. Ссылка хранится отдельно в модерации. Верни только финальный текст. "
        "Сохраняй все существующие маркеры ссылок вида [[LINK:text|url]]. Сохраняй существующие [[EMOJI:alias]] маркеры без изменений. "
        "Если пост про сервис/инструмент и в тексте есть raw URL, преобразуй его в короткий CTA с [[LINK:text|url]]. "
        "Не удаляй полезные CTA-ссылки и не выдумывай новые ссылки. "
        "Для перечислений используй обычные строки с ➖. Бот сам оформит блок из 2+ пунктов как цитату. "
        "Можно оставить [[QUOTE]]...[[/QUOTE]], если это действительно уместно, но это не обязательно. "
        "Если в черновике есть \"▌\", убери этот символ. Не выводи raw HTML и не используй markdown blockquote. "
        "Пост должен быть цельным, без обрыва мысли посередине. "
        "Без AI-клише, без эм-даша, без кавычек-ёлочек. "
        "Избегай штампов: 'не про..., а про...', 'главный вывод простой', 'важно отметить', 'давайте разберем', 'в заключение'. "
        "Это финальный humanizer-pass: убери AI-клише, сделай текст естественным, сохрани факты, "
        "сохрани Telegram-формат, сохрани маркеры списка ➖ и короткую человеческую концовку. "
        "Не добавляй строку Источник внутрь поста.\n\n"
        "Используй [[EMOJI:alias]] для заголовка/CTA/финальной мысли, а не raw emoji. "
        "Для финальной мысли - [[EMOJI:thought]], для CTA/link-строки - [[EMOJI:link]]. "
        "Для generic AI/tool/model news - [[EMOJI:screen_card]] (или [[EMOJI:fire]]), для MiniMax/Mistral/Qwen/Llama - [[EMOJI:screen_card]], если нет более точного alias. "
        "Используй [[EMOJI:claude]] для Claude/Anthropic, [[EMOJI:chatgpt]] для ChatGPT/OpenAI/GPT, [[EMOJI:deepseek]] для DeepSeek, [[EMOJI:google]] для Google/Gemini/DeepMind. "
        "Для GitHub используй [[EMOJI:github]], для Photoshop/Adobe [[EMOJI:photoshop]], для Windows [[EMOJI:windows]], для Telegram [[EMOJI:telegram]], для privacy/security/VPN [[EMOJI:lock]], для web/services [[EMOJI:web]]. "
        "Если alias явно не подходит, можно использовать обычный emoji или не использовать emoji.\n\n"
        "Перед возвратом финального текста молча проверь: "
        "похоже ли это на обычного автора Telegram, нет ли AI-клише, нет ли конструкции 'не про..., а про...', "
        "нет ли стерильного маркетингового тона, не слишком ли длинно, нет ли неподтверждённых фактов. "
        "Верни только финальный очищенный пост, без отчёта о проверке.\n\n"
        f"Дополнительные humanizer-правила:\n{HUMANIZER_RULES_FOR_SIMPLIFY_AI}\n\n"
        f"Источник (контекст модерации): {source_url or 'не указан'}\n\n"
        f"Желательная длина до {soft_chars} символов. Жёсткий максимум {max_chars} символов. Не обрывай мысль.\n\n"
        f"Текущий черновик:\n{_strip_source_lines(draft_text)}"
    )
    logger.info("Полировка черновика: model=%s", model)
    result = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_safely(_strip_source_lines(result.content), limit=max_chars)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    result.content = _ensure_custom_emoji_markers(final_text, source_url=source_url, title=None)
    return result


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


def generate_post_draft_from_page(api_key: str, model: str, source_url: str, title: str, page_text: str, max_chars: int = 1400, soft_chars: int = 1100, base_url: str | None = None, extra_headers: dict[str, str] | None = None) -> GenerationResult:
    style = _load_style_prompt() + "\n\n" + SIMPLIFY_AI_STYLE_GUIDE + "\n\n" + SIMPLIFY_AI_EMOJI_ALIAS_GUIDE
    user_prompt = (
        "Ниже ссылка и извлечённый текст страницы. Опирайся только на этот текст страницы. "
        "Соблюдай стиль-гайд ниже как основные правила. "
        "Не выдумывай факты, если их нет в тексте. "
        "Сделай один готовый пост в стиле @simplify_ai. "
        "Структура: короткий заголовок, 1-2 простых вводных предложения, практический смысл простыми словами, короткая финальная мысль (когда уместно). "
        "Для заголовка, CTA и финальной мысли используй custom emoji aliases через маркеры [[EMOJI:alias]], а не raw emoji. "
        "Если есть подходящий alias, не используй обычные emoji. Для финальной мысли используй [[EMOJI:thought]], для CTA-строки используй [[EMOJI:link]]. "
        "Для generic AI/tool/model news используй [[EMOJI:screen_card]] или [[EMOJI:fire]]. Для MiniMax, Mistral, Qwen, Llama и других без отдельного alias используй [[EMOJI:screen_card]]. "
        "Используй [[EMOJI:claude]] для Claude/Anthropic, [[EMOJI:chatgpt]] для ChatGPT/OpenAI/GPT, [[EMOJI:deepseek]] для DeepSeek, [[EMOJI:google]] для Google/Gemini/DeepMind. "
        "Для GitHub используй [[EMOJI:github]], для Photoshop/Adobe [[EMOJI:photoshop]], для Windows [[EMOJI:windows]], для Telegram [[EMOJI:telegram]], для privacy/security/VPN [[EMOJI:lock]], для web/services [[EMOJI:web]]. "
        "Если alias явно не подходит, можно использовать обычный emoji или не использовать emoji. "
        "Для перечислений используй обычные строки с ➖. Бот сам превратит подряд идущие пункты в цитату Telegram. "
        "Можно использовать [[QUOTE]]...[[/QUOTE]], но не делай это обязательным и не завязывай на этом структуру. "
        "Не используй \"▌\", markdown blockquote или HTML. Не используй больше одного quote block, если только тексту действительно это не нужно. "
        "Пиши живо и по-человечески: без сухого пресс-релизного стиля, без корпоративного тона, без AI-клише. "
        "Не используй фразы: 'не про..., а про...', 'главный вывод простой', 'важно отметить', 'давайте разберем', 'в заключение'. "
        "Не используй эм-даш и кавычки-ёлочки. "
        "Не оставляй ответ пустым: даже если статья слабая, сделай осторожный короткий пост только по подтверждённым фактам. "
        "Не добавляй строку Источник в сам пост. Ссылка хранится отдельно в модерации. "
        "Если пост про инструмент/сервис/репозиторий/приложение/демо/гайд, добавь в конце CTA-ссылку. "
        "Используй только маркеры [[LINK:text|url]] и никогда не выводи голые URL. "
        "Не добавляй строку 'Источник:'. Не выдумывай ссылки. "
        f"Желательная длина до {soft_chars} символов. Жёсткий максимум {max_chars} символов. Не обрывай мысль. "
        "Не используй markdown blockquote и не используй HTML. "
        "Верни только финальный текст поста без комментариев и без markdown-блоков.\n\n"
        f"Источник (контекст модерации): {source_url}\nЗаголовок: {title}\n\nТекст страницы:\n{page_text}"
    )
    logger.info("Генерация по URL: model=%s", model)
    result = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_safely(_strip_source_lines(result.content), limit=max_chars)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    result.content = _ensure_custom_emoji_markers(final_text, source_url=source_url, title=title)
    return result
