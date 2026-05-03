from __future__ import annotations

import re
import string

_CLEAN_RE = re.compile(r"\s+")
_STOP_WORDS = {
    "the", "a", "an", "and", "for", "with", "new", "новый", "новая", "запустил", "запустила"
}


def normalize_topic_title(title: str) -> str:
    lowered = title.lower()
    table = str.maketrans({ch: " " for ch in string.punctuation + "«»—–…"})
    cleaned = lowered.translate(table)
    words = [w for w in _CLEAN_RE.sub(" ", cleaned).strip().split(" ") if w and w not in _STOP_WORDS]
    return " ".join(words)


def score_topic(title: str, source: str, url: str) -> tuple[int, str, str]:
    text = f"{title} {source} {url}".lower()
    score = 50
    reason_parts: list[str] = []

    category = "other"
    if any(k in text for k in ["agent", "agents", "ai agent", "browser agent", "computer use", "operator"]):
        score += 30
        category = "agent"
        reason_parts.append("агенты")
    elif any(k in text for k in ["gpt", "claude", "gemini", "llama", "mistral", "qwen", "kimi", "grok", "model"]):
        score += 24
        category = "model"
        reason_parts.append("модель")
    elif any(k in text for k in ["image", "video", "generation", "editor", "avatar", "voice", "speech", "tool", "plugin", "extension", "api"]):
        score += 20
        category = "tool"
        reason_parts.append("инструмент")
    elif any(k in text for k in ["benchmark", "reasoning", "coding", "developer", "open source", "github", "repo", "release", "launch"]):
        score += 18
        category = "dev"
        reason_parts.append("девелоперская польза")
    elif any(k in text for k in ["paper", "research", "arxiv"]):
        score += 12
        category = "research"
        reason_parts.append("ресерч")

    if any(k in text for k in ["policy", "regulation", "funding", "enterprise", "earnings"]):
        score -= 25
        category = "business"
        reason_parts.append("финансы/регуляторика")

    if len(title) > 130:
        score -= 10
        reason_parts.append("слишком длинный заголовок")

    if not any(k in text for k in ["ai", "gpt", "claude", "model", "agent", "llm", "tool", "developer", "coding", "github"]):
        score -= 20
        reason_parts.append("мало сигналов практической пользы")

    score = max(0, min(100, score))
    if "claude" in text:
        reason_parts.append("Claude")
    if not reason_parts:
        reason_parts.append("нейтральная тема")
    return score, category, ", ".join(reason_parts)
