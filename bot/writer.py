"""AI-powered draft generation for Telegram posts."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from bot.link_policy import strip_disallowed_cta_links
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
class PageContent:
    title: str
    text: str
    preview_image_url: str | None = None


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



def enrich_topic_metadata_ru(
    *,
    api_key: str,
    model: str,
    title: str,
    source: str,
    description: str | None = None,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> GenerationResult | None:
    """Generate short Russian topic display metadata; return pipe-separated fields."""
    clean_title = title.strip()
    if not clean_title:
        return None
    system_prompt = (
        "You help a Russian-speaking Telegram channel admin understand AI topic candidates. "
        "Return exactly three short lines in Russian: title_ru, summary_ru, angle_ru. "
        "Do not translate URLs. Preserve product names, repo names, model names, company names and versions. "
        "Do not invent facts beyond the given title, source and description. Keep each line short."
    )
    user_prompt = (
        f"Title: {clean_title[:300]}\n"
        f"Source: {source[:120]}\n"
        f"Description: {(description or '')[:500]}\n\n"
        "Format:\nTITLE: ...\nSUMMARY: ...\nANGLE: ..."
    )
    try:
        result = _generate_with_chat_completion(
            api_key=api_key,
            model=model,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            base_url=base_url,
            extra_headers=extra_headers,
            max_tokens=220,
        )
    except Exception as exc:
        logger.warning("Topic metadata enrichment failed: %s", exc)
        return None
    lines = [line.strip() for line in result.content.splitlines() if line.strip()]
    values: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in {"title", "title_ru"}:
            values["title"] = value.strip().strip('"“”')
        elif key in {"summary", "summary_ru"}:
            values["summary"] = value.strip().strip('"“”')
        elif key in {"angle", "angle_ru"}:
            values["angle"] = value.strip().strip('"“”')
    if not all(values.get(k) for k in ("title", "summary", "angle")):
        return None
    result.content = "\n".join([values["title"][:180], values["summary"][:260], values["angle"][:260]])
    return result

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



REWRITE_POST_DRAFT_MODE_INSTRUCTIONS: dict[str, str] = {
    "remove_fluff": (
        "Режим: убрать воду. Удали филлер, повторы, generic AI wording и лишние объяснения. "
        "Сохрани смысл, факты, структуру и полезные акценты."
    ),
    "shorten": (
        "Режим: сделать короче. Сделай пост заметно короче: цель примерно 60-70% текущей длины. "
        "Сохрани главную мысль, важные факты, CTA и source/link markers."
    ),
    "neutralize_ads": (
        "Режим: убрать рекламный тон. Удали маркетинговый тон, хайп, salesy claims и обещания. "
        "Сохрани полезные преимущества, но сформулируй их спокойно, как человеческий Telegram-пост, а не рекламу."
    ),
}




def _finalize_generated_content(
    text: str,
    *,
    source_url: str | None = None,
    source_group: str | None = None,
    category: str | None = None,
    title: str | None = None,
) -> str:
    marked = _ensure_custom_emoji_markers(text, source_url=source_url, title=title)
    return strip_disallowed_cta_links(
        marked,
        source_url=source_url,
        source_group=source_group,
        category=category,
        title=title,
    )


def _rewrite_post_draft_instruction(mode: str) -> str:
    try:
        return REWRITE_POST_DRAFT_MODE_INSTRUCTIONS[mode]
    except KeyError as exc:
        raise ValueError(f"Unsupported rewrite mode: {mode}") from exc

def generate_post_draft_from_topic_metadata(
    *,
    api_key: str,
    model: str,
    topic_title: str,
    topic_title_ru: str | None = None,
    topic_summary_ru: str | None = None,
    topic_angle_ru: str | None = None,
    topic_original_description: str | None = None,
    topic_source: str | None = None,
    topic_source_group: str | None = None,
    topic_category: str | None = None,
    source_url: str | None = None,
    max_chars: int = 1400,
    soft_chars: int = 1100,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> GenerationResult:
    style = _load_style_prompt() + "\n\n" + SIMPLIFY_AI_STYLE_GUIDE + "\n\n" + SIMPLIFY_AI_EMOJI_ALIAS_GUIDE
    metadata_lines = [
        f"Original title: {(topic_title or '').strip()[:500]}",
        f"Russian title: {(topic_title_ru or '').strip()[:500]}",
        f"Russian summary: {(topic_summary_ru or '').strip()[:900]}",
        f"Russian angle: {(topic_angle_ru or '').strip()[:700]}",
        f"Original description: {(topic_original_description or '').strip()[:1200]}",
        f"Source name: {(topic_source or '').strip()[:200]}",
        f"Source group: {(topic_source_group or '').strip()[:120]}",
        f"Category: {(topic_category or '').strip()[:120]}",
        f"Source URL for moderation context: {(source_url or '').strip()[:600]}",
    ]
    user_prompt = (
        "Создай один готовый Telegram-пост для @simplify_ai по сохранённым метаданным темы. "
        "Полная страница источника не была прочитана, поэтому не притворяйся, что видел статью или тред целиком. "
        "Опирайся только на метаданные ниже: title, title_ru, summary_ru, angle_ru, original_description, source, source_group, category и URL. "
        "Не выдумывай факты, цифры, цитаты, даты, бенчмарки, названия функций и обещания сверх этих метаданных. "
        "Если деталей мало, пиши осторожно: 'похоже', 'заявлено', 'обсуждают', 'по описанию темы'. "
        "Сохраняй важные названия продуктов, репозиториев, моделей, компаний и версии без искажений. "
        "Соблюдай стиль-гайд ниже как основные правила. "
        "Структура: короткий заголовок, 1-2 простых вводных предложения, практический смысл простыми словами, короткая финальная мысль, если уместно. "
        "Для заголовка, CTA и финальной мысли используй custom emoji aliases через маркеры [[EMOJI:alias]], а не raw emoji. "
        "Никогда не выводи raw emoji в финальном черновике. Если нужен emoji, используй только [[EMOJI:alias]] маркеры. Для финальной мысли используй [[EMOJI:thought]], для CTA-строки используй [[EMOJI:link]]. "
        "Для generic AI/tool/model news используй [[EMOJI:screen_card]] или [[EMOJI:fire]]. Для MiniMax, Mistral, Qwen, Llama и других без отдельного alias используй [[EMOJI:screen_card]]. "
        "Используй [[EMOJI:claude]] для Claude/Anthropic, [[EMOJI:chatgpt]] для ChatGPT/OpenAI/GPT, [[EMOJI:deepseek]] для DeepSeek, [[EMOJI:google]] для Google/Gemini/DeepMind. "
        "Для GitHub используй [[EMOJI:github]], для Photoshop/Adobe [[EMOJI:photoshop]], для Windows [[EMOJI:windows]], для Telegram [[EMOJI:telegram]], для privacy/security/VPN [[EMOJI:lock]], для web/services [[EMOJI:web]]. "
        "Для перечислений используй обычные строки с ➖, если это не ломает структуру; финальный рендер превратит ведущий маркер в custom emoji. Не используй raw emoji в других местах. "
        "Не используй '▌', markdown blockquote или HTML. "
        "Пиши живо и по-человечески: без сухого пресс-релизного стиля, без корпоративного тона, без AI-клише. "
        "Не используй фразы: 'не про..., а про...', 'главный вывод простой', 'важно отметить', 'давайте разберем', 'в заключение'. "
        "Не используй эм-даш и кавычки-ёлочки. "
        "Не добавляй строку Источник в сам пост. URL хранится отдельно в модерации. "
        "source_url нужен в первую очередь для модерации и фактчекинга. Не добавляй CTA только потому, что source_url существует. Если пост про тестируемый инструмент/сервис/репозиторий/приложение/демо/гайд и URL реально полезен читателю, можно добавить короткую CTA-ссылку через [[LINK:text|url]]. Не добавляй 'Подробнее'-ссылки на новости, блоги и статьи. "
        "Используй только маркеры [[LINK:text|url]] и никогда не выводи голые URL. Не выдумывай ссылки. "
        f"Желательная длина до {soft_chars} символов. Жёсткий максимум {max_chars} символов. Не обрывай мысль. "
        "Верни только финальный текст поста без комментариев и без markdown-блоков.\n\n"
        "Метаданные темы:\n" + "\n".join(metadata_lines)
    )
    logger.info("Генерация черновика по метаданным темы: model=%s", model)
    result = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_safely(_strip_source_lines(result.content), limit=max_chars)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    result.content = _finalize_generated_content(final_text, source_url=source_url, source_group=topic_source_group, category=topic_category, title=topic_title_ru or topic_title)
    return result


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
        "Никогда не выводи raw emoji в финальном черновике. Если нужен emoji, используй только [[EMOJI:alias]] маркеры. Для финальной мысли используй [[EMOJI:thought]], для CTA-строки [[EMOJI:link]]. "
        "Для generic AI/tool/model news используй [[EMOJI:screen_card]] или [[EMOJI:fire]]; для MiniMax/Mistral/Qwen/Llama без отдельного alias используй [[EMOJI:screen_card]]. "
        "Используй [[EMOJI:claude]] для Claude/Anthropic, [[EMOJI:chatgpt]] для ChatGPT/OpenAI/GPT, [[EMOJI:deepseek]] для DeepSeek, [[EMOJI:google]] для Google/Gemini/DeepMind. "
        "Для GitHub используй [[EMOJI:github]], для Photoshop/Adobe [[EMOJI:photoshop]], для Windows [[EMOJI:windows]], для Telegram [[EMOJI:telegram]], для privacy/security/VPN [[EMOJI:lock]], для web/services [[EMOJI:web]]. "
        "Если alias явно не подходит, не используй emoji. "
        f"Источник (контекст модерации и фактчекинга, не повод для CTA): {source_context}. Не добавляй CTA только потому, что source_url существует. Добавляй [[LINK:text|url]] только для тестируемого/полезного читателю URL, не для новостей и блогов."
    )
    logger.info("Генерация черновика: model=%s", model)
    result = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_safely(_strip_source_lines(result.content), limit=max_chars)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    result.content = _finalize_generated_content(final_text, source_url=source_url, title=None)
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
        "Сохраняй существующие маркеры ссылок вида [[LINK:text|url]] только если они ведут на тестируемый сервис/инструмент/репозиторий/демо/гайд. Сохраняй существующие [[EMOJI:alias]] маркеры без изменений. "
        "Если пост про сервис/инструмент и в тексте есть raw URL, преобразуй его в короткий CTA с [[LINK:text|url]] только когда URL ведёт на тестируемый сервис/инструмент/репозиторий/демо/гайд. "
        "Не удаляй полезные тестируемые CTA-ссылки и не выдумывай новые ссылки; удаляй CTA на новости/блоги/статьи. "
        "Для перечислений используй обычные строки с ➖, если это не ломает структуру; финальный рендер превратит ведущий маркер в custom emoji. "
        "Можно оставить [[QUOTE]]...[[/QUOTE]], если это действительно уместно, но это не обязательно. "
        "Если в черновике есть \"▌\", убери этот символ. Не выводи raw HTML и не используй markdown blockquote. "
        "Пост должен быть цельным, без обрыва мысли посередине. "
        "Без AI-клише, без эм-даша, без кавычек-ёлочек. "
        "Избегай штампов: 'не про..., а про...', 'главный вывод простой', 'важно отметить', 'давайте разберем', 'в заключение'. "
        "Это финальный humanizer-pass: убери AI-клише, сделай текст естественным, сохрани факты, "
        "сохрани Telegram-формат, сохрани маркеры списка ➖ и короткую человеческую концовку. "
        "Не добавляй строку Источник внутрь поста.\n\n"
        "Никогда не выводи raw emoji в финальном черновике. Используй [[EMOJI:alias]] для заголовка/CTA/финальной мысли и branded bullets. "
        "Для финальной мысли - [[EMOJI:thought]], для CTA/link-строки - [[EMOJI:link]]. "
        "Для generic AI/tool/model news - [[EMOJI:screen_card]] (или [[EMOJI:fire]]), для MiniMax/Mistral/Qwen/Llama - [[EMOJI:screen_card]], если нет более точного alias. "
        "Используй [[EMOJI:claude]] для Claude/Anthropic, [[EMOJI:chatgpt]] для ChatGPT/OpenAI/GPT, [[EMOJI:deepseek]] для DeepSeek, [[EMOJI:google]] для Google/Gemini/DeepMind. "
        "Для GitHub используй [[EMOJI:github]], для Photoshop/Adobe [[EMOJI:photoshop]], для Windows [[EMOJI:windows]], для Telegram [[EMOJI:telegram]], для privacy/security/VPN [[EMOJI:lock]], для web/services [[EMOJI:web]]. "
        "Если alias явно не подходит, не используй emoji.\n\n"
        "Перед возвратом финального текста молча проверь: "
        "похоже ли это на обычного автора Telegram, нет ли AI-клише, нет ли конструкции 'не про..., а про...', "
        "нет ли стерильного маркетингового тона, не слишком ли длинно, нет ли неподтверждённых фактов. "
        "Верни только финальный очищенный пост, без отчёта о проверке.\n\n"
        f"Дополнительные humanizer-правила:\n{HUMANIZER_RULES_FOR_SIMPLIFY_AI}\n\n"
        f"Источник (контекст модерации и фактчекинга, не повод для CTA): {source_url or 'не указан'}. Не добавляй CTA только потому, что source_url существует. Добавляй [[LINK:text|url]] только для тестируемого/полезного читателю URL, не для новостей и блогов.\n\n"
        f"Желательная длина до {soft_chars} символов. Жёсткий максимум {max_chars} символов. Не обрывай мысль.\n\n"
        f"Текущий черновик:\n{_strip_source_lines(draft_text)}"
    )
    logger.info("Полировка черновика: model=%s", model)
    result = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_safely(_strip_source_lines(result.content), limit=max_chars)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    result.content = _finalize_generated_content(final_text, source_url=source_url, title=None)
    return result


def rewrite_post_draft(
    api_key: str,
    model: str,
    draft_text: str,
    source_url: str | None = None,
    mode: str = "remove_fluff",
    max_chars: int = 1400,
    soft_chars: int = 1100,
    base_url: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> GenerationResult:
    """Rewrite an existing Telegram draft in a narrow cleanup mode."""
    mode_instruction = _rewrite_post_draft_instruction(mode)
    cleaned_draft = _strip_source_lines(draft_text)
    style = _load_style_prompt() + "\n\n" + SIMPLIFY_AI_STYLE_GUIDE + "\n\n" + SIMPLIFY_AI_EMOJI_ALIAS_GUIDE
    user_prompt = (
        "Перепиши существующий черновик поста для @simplify_ai в указанном режиме. "
        "Это точечный cleanup-pass, а не генерация нового поста. "
        "Соблюдай стиль-гайд и humanizer-правила ниже. "
        "Сохраняй факты и смысл. Не добавляй новые факты, даты, цифры, выводы или ссылки. "
        "Сохраняй только полезные маркеры ссылок вида [[LINK:text|url]] для тестируемых сервисов/инструментов/репозиториев/демо/гайдов. Не сохраняй CTA 'Подробнее' на новости или блоги. "
        "Сохраняй существующие [[EMOJI:alias]] маркеры, когда это возможно и уместно. "
        "Не добавляй строку Источник: или Source:. Ссылка хранится отдельно в модерации. "
        "Верни только финальный текст поста без комментариев, без отчёта, без markdown-блоков и без HTML. "
        "Если в тексте есть raw URL, не выдумывай новые ссылки; полезный URL можно оставить только как [[LINK:text|url]], если это тестируемый сервис/инструмент/репозиторий/демо/гайд, а не новость или блог. "
        "Не используй markdown blockquote. Для списков можно оставить строки с ➖ и существующие [[QUOTE]]...[[/QUOTE]], если они уже помогают структуре; финальный рендер превратит ведущий ➖ в custom emoji. "
        "Без AI-клише, без эм-даша, без кавычек-ёлочек. "
        "Избегай штампов: 'не про..., а про...', 'главный вывод простой', 'важно отметить', 'давайте разберем', 'в заключение'. "
        "Пост должен быть цельным и не обрываться посередине. "
        f"{mode_instruction} "
        f"Желательная длина до {soft_chars} символов. Жёсткий максимум {max_chars} символов. "
        "Перед возвратом молча проверь, что текст не пустой, не содержит строки Источник:, не содержит raw emoji, сохраняет полезные [[LINK:...]] и [[EMOJI:...]] маркеры.\n\n"
        f"Дополнительные humanizer-правила:\n{HUMANIZER_RULES_FOR_SIMPLIFY_AI}\n\n"
        f"Источник (только контекст модерации и фактчекинга, не добавлять в пост и не использовать как CTA без тестируемой пользы): {source_url or 'не указан'}. Не добавляй CTA только потому, что source_url существует; не добавляй Подробнее-ссылки на новости/блоги.\n\n"
        f"Текущий черновик:\n{cleaned_draft}"
    )
    logger.info("Переписывание черновика: mode=%s model=%s", mode, model)
    result = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_safely(_strip_source_lines(result.content), limit=max_chars)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    result.content = _finalize_generated_content(final_text, source_url=source_url, title=None)
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


def _normalize_preview_image_url(source_url: str, image_url: str | None) -> str | None:
    candidate = (image_url or "").strip()
    if not candidate:
        return None
    lower = candidate.lower()
    if lower.startswith(("data:", "javascript:", "blob:")):
        return None
    try:
        absolute_url = urljoin(source_url, candidate)
        parsed = urlsplit(absolute_url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return absolute_url


def _extract_preview_image_url(soup: BeautifulSoup, source_url: str) -> str | None:
    selectors = [
        {"property": "og:image"},
        {"name": "twitter:image"},
        {"name": "twitter:image:src"},
    ]
    for attrs in selectors:
        tag = soup.find("meta", attrs=attrs)
        normalized = _normalize_preview_image_url(source_url, tag.get("content") if tag else None)
        if normalized:
            return normalized
    return None


def fetch_page_content_details(source_url: str, timeout_seconds: int = 12) -> PageContent:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; simplify-ai-autopilot/1.0; +https://t.me/simplify_ai)"}
    response = requests.get(source_url, timeout=timeout_seconds, headers=headers)
    response.raise_for_status()
    if "text/html" not in response.headers.get("Content-Type", ""):
        raise ValueError("URL не содержит HTML-страницу.")
    soup = BeautifulSoup(response.text, "html.parser")
    preview_image_url = _extract_preview_image_url(soup, source_url)
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
    return PageContent(title=title, text=text, preview_image_url=preview_image_url)


def fetch_page_content(source_url: str, timeout_seconds: int = 12) -> tuple[str, str]:
    details = fetch_page_content_details(source_url, timeout_seconds=timeout_seconds)
    return details.title, details.text


def generate_post_draft_from_page(api_key: str, model: str, source_url: str, title: str, page_text: str, max_chars: int = 1400, soft_chars: int = 1100, base_url: str | None = None, extra_headers: dict[str, str] | None = None) -> GenerationResult:
    style = _load_style_prompt() + "\n\n" + SIMPLIFY_AI_STYLE_GUIDE + "\n\n" + SIMPLIFY_AI_EMOJI_ALIAS_GUIDE
    user_prompt = (
        "Ниже ссылка и извлечённый текст страницы. Опирайся только на этот текст страницы. "
        "Соблюдай стиль-гайд ниже как основные правила. "
        "Не выдумывай факты, если их нет в тексте. "
        "Сделай один готовый пост в стиле @simplify_ai. "
        "Структура: короткий заголовок, 1-2 простых вводных предложения, практический смысл простыми словами, короткая финальная мысль (когда уместно). "
        "Для заголовка, CTA и финальной мысли используй custom emoji aliases через маркеры [[EMOJI:alias]], а не raw emoji. "
        "Никогда не выводи raw emoji в финальном черновике. Если нужен emoji, используй только [[EMOJI:alias]] маркеры. Для финальной мысли используй [[EMOJI:thought]], для CTA-строки используй [[EMOJI:link]]. "
        "Для generic AI/tool/model news используй [[EMOJI:screen_card]] или [[EMOJI:fire]]. Для MiniMax, Mistral, Qwen, Llama и других без отдельного alias используй [[EMOJI:screen_card]]. "
        "Используй [[EMOJI:claude]] для Claude/Anthropic, [[EMOJI:chatgpt]] для ChatGPT/OpenAI/GPT, [[EMOJI:deepseek]] для DeepSeek, [[EMOJI:google]] для Google/Gemini/DeepMind. "
        "Для GitHub используй [[EMOJI:github]], для Photoshop/Adobe [[EMOJI:photoshop]], для Windows [[EMOJI:windows]], для Telegram [[EMOJI:telegram]], для privacy/security/VPN [[EMOJI:lock]], для web/services [[EMOJI:web]]. "
        "Если alias явно не подходит, не используй emoji. "
        "Для перечислений используй обычные строки с ➖, если это не ломает структуру; финальный рендер превратит ведущий маркер в custom emoji. Не используй raw emoji в других местах. "
        "Можно использовать [[QUOTE]]...[[/QUOTE]], но не делай это обязательным и не завязывай на этом структуру. "
        "Не используй \"▌\", markdown blockquote или HTML. Не используй больше одного quote block, если только тексту действительно это не нужно. "
        "Пиши живо и по-человечески: без сухого пресс-релизного стиля, без корпоративного тона, без AI-клише. "
        "Не используй фразы: 'не про..., а про...', 'главный вывод простой', 'важно отметить', 'давайте разберем', 'в заключение'. "
        "Не используй эм-даш и кавычки-ёлочки. "
        "Не оставляй ответ пустым: даже если статья слабая, сделай осторожный короткий пост только по подтверждённым фактам. "
        "Не добавляй строку Источник в сам пост. Ссылка хранится отдельно в модерации. "
        "source_url нужен в первую очередь для модерации и фактчекинга. Не добавляй CTA только потому, что source_url существует. Если пост про инструмент/сервис/репозиторий/приложение/демо/гайд и URL ведёт на тестируемую страницу, добавь в конце CTA-ссылку. Не добавляй CTA просто на новость, блог или статью. "
        "Используй только маркеры [[LINK:text|url]] и никогда не выводи голые URL. "
        "Не добавляй строку 'Источник:'. Не выдумывай ссылки. "
        f"Желательная длина до {soft_chars} символов. Жёсткий максимум {max_chars} символов. Не обрывай мысль. "
        "Не используй markdown blockquote и не используй HTML. "
        "Верни только финальный текст поста без комментариев и без markdown-блоков.\n\n"
        f"Источник (контекст модерации и фактчекинга, не повод для CTA): {source_url}\nЗаголовок: {title}\n\nТекст страницы:\n{page_text}"
    )
    logger.info("Генерация по URL: model=%s", model)
    result = _generate_with_chat_completion(api_key, model, user_prompt, style, base_url, extra_headers)
    final_text = _limit_text_safely(_strip_source_lines(result.content), limit=max_chars)
    if not _has_meaningful_body(final_text, source_url=source_url):
        raise EmptyAIResponseError("AI model returned empty content")
    result.content = _finalize_generated_content(final_text, source_url=source_url, title=title)
    return result
