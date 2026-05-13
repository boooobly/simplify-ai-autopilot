from __future__ import annotations

from datetime import datetime, timezone
import re
import string

_CLEAN_RE = re.compile(r"\s+")
_STOP_WORDS = {
    "the", "a", "an", "and", "for", "with", "new", "новый", "новая", "запустил", "запустила"
}

# Score scale: 85-100 = very strong, 70-84 = good, 50-69 = maybe useful, <50 = usually skip.
_SOURCE_GROUP_BOOST = {
    "official_ai": 0,
    "tech_media": 4,
    "ru_tech": 8,
    "tools": 14,
    "community": 14,
    "github": 8,
    "custom": 8,
}

_UNRELIABLE_DATE_GROUPS = {"github", "tools", "community"}
_PRACTICAL_KEYWORDS = [
    "tool", "service", "app", "extension", "browser", "editor", "image", "video", "audio", "voice",
    "design", "creator", "prompt", "workflow", "automation", "agent", "local", "open-source", "open source",
    "free", "бесплат", "инструмент", "сервис", "приложение", "расширение", "промпт", "автоматизац",
]
_GITHUB_PRACTICAL_KEYWORDS = [
    "tool", "app", "agent", "browser", "extension", "editor", "image", "video", "audio", "prompt",
    "workflow", "automation", "local", "open-source", "open source", "desktop", "cli", "chat", "assistant",
]
_CORPORATE_KEYWORDS = [
    "funding", "raises", "raised", "series a", "series b", "partnership", "enterprise", "earnings",
    "quarterly", "revenue", "acquisition", "acquires", "valuation", "ipo", "startup funding",
    "инвестиции", "раунд", "партнерство", "выручк", "поглощен", "оценен",
]
_SPAM_KEYWORDS = ["casino", "betting", "sportsbook", "porn", "xxx", "viagra", "crypto casino", "airdrop", "token presale"]
_RESEARCH_KEYWORDS = ["paper", "research", "arxiv", "study", "dataset", "eval", "benchmark", "исследован", "датасет"]
_NARROW_DEV_KEYWORDS = ["library", "framework", "sdk", "api wrapper", "bindings", "kernel", "compiler", "runtime", "orm"]
_USER_IMPACT_KEYWORDS = ["privacy", "leak", "hack", "lawsuit", "ban", "tracking", "data breach", "утеч", "взлом", "бан", "слеж"]


def normalize_topic_title(title: str) -> str:
    lowered = title.lower()
    table = str.maketrans({ch: " " for ch in string.punctuation + "«»—–…"})
    cleaned = lowered.translate(table)
    words = [w for w in _CLEAN_RE.sub(" ", cleaned).strip().split(" ") if w and w not in _STOP_WORDS]
    return " ".join(words)


def _has_any(text: str, keywords: list[str]) -> bool:
    return any(k in text for k in keywords)


def _parse_score_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            continue
    return None


def _extract_first_int(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"(\d[\d,\s]*)", value)
    if not match:
        return 0
    try:
        return int(match.group(1).replace(",", "").replace(" ", ""))
    except ValueError:
        return 0


def _score_github_topic(title: str, source: str, url: str, description: str, stars_today: str | None) -> tuple[int, str, list[str]]:
    text = f"{title} {source} {url} {description}".lower()
    score = 48 + _SOURCE_GROUP_BOOST["github"]
    reasons: list[str] = ["GitHub без автотопа"]
    category = "dev"

    if description.strip():
        score += 8
        reasons.append("есть описание")
    else:
        score -= 22
        reasons.append("штраф: GitHub без описания")

    practical_hits = sum(1 for keyword in _GITHUB_PRACTICAL_KEYWORDS if keyword in text)
    if practical_hits:
        score += min(24, 7 + practical_hits * 4)
        reasons.append("понятная практическая польза")
        if _has_any(text, ["image", "video", "audio", "editor", "design"]):
            category = "creator"
        elif _has_any(text, ["agent", "workflow", "automation", "assistant"]):
            category = "agent"
        elif _has_any(text, ["app", "browser", "extension", "desktop", "tool"]):
            category = "tool"

    stars = _extract_first_int(stars_today)
    if stars >= 500:
        score += 10
        reasons.append("много звёзд сегодня")
    elif stars >= 100:
        score += 6
        reasons.append("заметный рост звёзд")
    elif stars >= 25:
        score += 3

    if _has_any(text, _NARROW_DEV_KEYWORDS) and practical_hits < 2:
        score -= 14
        reasons.append("штраф: узко для разработчиков")
    if _has_any(text, ["awesome list", "curated list"]) and practical_hits < 2:
        score -= 8
    if _has_any(text, _SPAM_KEYWORDS):
        score -= 45
        category = "other"
        reasons.append("штраф: спам")
    return score, category, reasons


def score_topic(
    title: str,
    source: str,
    url: str,
    source_group: str = "other",
    description: str | None = None,
    published_at: str | None = None,
    stars_today: str | None = None,
) -> tuple[int, str, str]:
    text = f"{title} {source} {url} {description or ''}".lower()
    source_group = (source_group or "other").strip().lower()
    if source_group == "github":
        score, category, reason_parts = _score_github_topic(title, source, url, description or "", stars_today)
    else:
        score = 45 + _SOURCE_GROUP_BOOST.get(source_group, 0)
        reason_parts: list[str] = []
        category = "other"

        if _has_any(text, _SPAM_KEYWORDS + ["airdrop", "token", "nft"]):
            score -= 45
            reason_parts.append("штраф: спам/крипта")
        if _has_any(text, _USER_IMPACT_KEYWORDS + ["drama", "fail", "bug"]):
            score += 24
            category = "privacy" if _has_any(text, ["privacy", "data", "tracking", "leak", "утеч", "слеж"]) else "drama"
            reason_parts.append("затрагивает пользователей")
        if _has_any(text, ["guide", "tutorial", "prompt", "course", "курс", "гайд", "how to"]):
            score += 18
            category = "guide"
            reason_parts.append("можно сделать мини-гайд")
        if _has_any(text, ["creator", "image", "video", "audio", "voice", "avatar", "music", "design", "editor"]):
            score += 20
            category = "creator"
            reason_parts.append("креаторский инструмент")
        if _has_any(text, ["ios", "android", "mobile", "app", "chrome extension", "browser extension"]):
            score += 16
            category = "mobile"
            reason_parts.append("приложение/расширение")
        if _has_any(text, ["tool", "service", "website", "platform", "plugin", "extension", "automation"]):
            score += 16
            if category == "other":
                category = "tool"
            reason_parts.append("понятный инструмент")
        if _has_any(text, ["launch", "launched", "release", "released", "demo", "beta", "product hunt"]):
            score += 10
            if category == "other":
                category = "news"
            reason_parts.append("релиз/демо")
        if _has_any(text, ["agent", "agents", "computer use", "operator", "workflow"]):
            score += 14
            category = "agent"
            reason_parts.append("AI-агенты/процессы")
        if _has_any(text, ["free", "open-source", "open source", "бесплат"]):
            score += 12
            reason_parts.append("бесплатно/open-source")
        if _has_any(text, ["reddit", "x.com", "twitter", "tiktok", "viral", "meme", "weird", "strange", "необычн", "мем"]):
            score += 14
            category = "meme" if category == "other" else category
            reason_parts.append("соцсети/вирусность")
        if _has_any(text, ["gpt", "claude", "gemini", "llama", "mistral", "qwen", "model"]):
            if _has_any(text, ["app", "tool", "api", "free", "voice", "image", "video", "faster", "cheaper", "local"]):
                score += 12
                reason_parts.append("модель с практическим смыслом")
            else:
                score += 6
            if category == "other":
                category = "model"
        if _has_any(text, _RESEARCH_KEYWORDS):
            if _has_any(text, ["tool", "demo", "app", "benchmark", "faster", "cheaper", "open-source", "open source"]):
                score += 6
            else:
                score -= 12
                reason_parts.append("штраф: исследование без понятной пользы")
            if category == "other":
                category = "research"
        if _has_any(text, _CORPORATE_KEYWORDS):
            score -= 28
            category = "business"
            reason_parts.append("штраф: корпоративная новость")
        if _has_any(text, ["policy", "regulation", "senate", "law", "закон", "регулирован"]):
            if _has_any(text, _USER_IMPACT_KEYWORDS):
                score += 4
            else:
                score -= 16
                reason_parts.append("штраф: политика без прямой пользы")
        if _has_any(text, ["job", "hiring", "webinar", "conference", "event", "ваканси", "вебинар", "конференц"]):
            score -= 18
            reason_parts.append("штраф: вакансия/ивент")

    parsed_dt = _parse_score_datetime(published_at)
    if parsed_dt:
        age_days = (datetime.utcnow() - parsed_dt).days
        if age_days > 30:
            score -= 18
            reason_parts.append("штраф: старая тема")
        elif age_days > 14:
            score -= 8
            reason_parts.append("тема не первой свежести")
    elif source_group not in _UNRELIABLE_DATE_GROUPS:
        score -= 6
        reason_parts.append("мягкий штраф: нет даты")

    if len(title) > 150:
        score -= 12
        reason_parts.append("штраф: длинный/шумный заголовок")
    elif len(title) > 120:
        score -= 6

    score = max(0, min(100, score))
    if not reason_parts:
        reason_parts.append("нейтральная тема")
    return score, category, ", ".join(dict.fromkeys(reason_parts))


def humanize_topic_reason_ru(category: str, score: int, source_group: str, reason: str) -> str:
    """Convert technical scoring fragments into an admin-friendly Russian reason."""
    category = (category or "other").strip().lower()
    reason_text = (reason or "").strip().lower()

    if "спам" in reason_text:
        return "Снижен вес: похоже на спам/крипту/мусор, для канала лучше пропустить."
    if "github без описания" in reason_text:
        return "Снижен вес: GitHub-проект без понятного описания."
    if "корпоратив" in reason_text:
        return "Снижен вес: похоже на корпоративную новость без пользы для обычного пользователя."
    if "исследование без понятной пользы" in reason_text:
        return "Снижен вес: исследование выглядит интересным, но практическая польза для новичков неочевидна."
    if "старая тема" in reason_text or "не первой свежести" in reason_text:
        return "Снижен вес: тема выглядит не самой свежей для короткого поста."
    if score >= 85:
        return "Сильная тема: это инструмент или новость с понятной пользой, подходит для короткого поста."
    if score >= 70:
        if category in {"tool", "creator", "mobile", "guide", "agent"}:
            return "Хорошая тема: есть практическая польза, можно быстро показать читателю, зачем это нужно."
        return "Хорошая тема: новость достаточно сильная, но перед постом стоит проверить простой пользовательский вывод."
    if score >= 50:
        return "Средняя тема: новость есть, но практическая польза неочевидна."
    return "Слабая тема: скорее всего, не стоит брать без дополнительного сильного угла для @simplify_ai."
