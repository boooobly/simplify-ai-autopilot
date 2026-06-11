from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
import re
import string

_CLEAN_RE = re.compile(r"\s+")
_STOP_WORDS = {
    "the", "a", "an", "and", "for", "with", "new", "update", "latest",
    "announces", "releases", "release", "launch", "launched", "launches", "introduces", "unveils",
    "новый", "новая", "новое", "новые", "запустил", "запустила", "представила", "представил", "выпустила", "выпустил",
}

_SOURCE_PREFIX_RE = re.compile(
    r"^\s*(?:github\s+trending|openai|anthropic|google|microsoft|meta|mistral|perplexity)\s*[:\-–—]+\s*",
    re.IGNORECASE,
)
_TOPIC_KEY_TOKEN_RE = re.compile(r"\d+(?:\.\d+)+|[a-zа-яё0-9]+", re.IGNORECASE)
_MIN_FUZZY_KEY_LENGTH = 16
_MIN_FUZZY_TOKEN_COUNT = 3
_TOPIC_KEY_SIMILARITY_THRESHOLD = 0.86

# Score scale: 85-100 = very strong, 70-84 = good, 50-69 = maybe useful, <50 = usually skip.
_SOURCE_GROUP_BOOST = {
    "official_ai": 0,
    "tech_media": 4,
    "ru_tech": 8,
    "tools": 14,
    "community": 14,
    "github": 8,
    "x": 10,
    "custom": 8,
    "telegram": 12,
}

_UNRELIABLE_DATE_GROUPS = {"github", "tools", "community", "x"}
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
_NARROW_DEV_KEYWORDS = ["library", "framework", "sdk", "api wrapper", "bindings", "kernel", "compiler", "runtime", "orm", "kubernetes", "ansible", "devops", "pgvector", "vector database", "vector search", "semantic search", "hybrid search", "sparse", "quantized", "terraform"]
_USER_IMPACT_KEYWORDS = ["privacy", "leak", "hack", "lawsuit", "ban", "tracking", "data breach", "утеч", "взлом", "бан", "слеж"]
_MAJOR_AI_BRANDS = ["openai", "chatgpt", "anthropic", "claude", "google", "gemini", "meta", "llama", "perplexity", "adobe", "apple", "microsoft", "copilot", "sora", "runway", "midjourney"]
_TECHNICAL_TUTORIAL_KEYWORDS = ["ansible", "kubernetes", "devops", "pgvector", "vector database", "vector search", "semantic search", "hybrid search", "sparse", "quantized", "benchmark", "benchmarks", "infrastructure", "deployment", "postgres", "docker", "terraform", "llmops", "rag pipeline"]



def canonical_topic_key(title: str, source_group: str | None = None) -> str:
    """Return a deterministic lightweight key for grouping the same story across sources."""
    text = (title or "").strip().lower()
    text = _SOURCE_PREFIX_RE.sub("", text)
    if (source_group or "").strip().lower() == "github":
        text = re.sub(r"^\s*github\s+trending\s*[:\-–—]+\s*", "", text, flags=re.IGNORECASE)
    tokens = []
    for match in _TOPIC_KEY_TOKEN_RE.finditer(text):
        token = match.group(0).lower()
        if token in _STOP_WORDS:
            continue
        tokens.append(token)
    return " ".join(tokens)


def is_similar_topic_key(a: str, b: str) -> bool:
    """Return True when two canonical topic keys likely describe the same story."""
    left = (a or "").strip().lower()
    right = (b or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens = left.split()
    right_tokens = right.split()
    if (
        min(len(left), len(right)) < _MIN_FUZZY_KEY_LENGTH
        or min(len(left_tokens), len(right_tokens)) < _MIN_FUZZY_TOKEN_COUNT
    ):
        return False
    # Avoid merging stories that only share a brand/generic AI term. Require at least
    # two overlapping tokens before applying the fuzzy ratio.
    if len(set(left_tokens) & set(right_tokens)) < 2:
        return False
    return SequenceMatcher(None, left, right).ratio() >= _TOPIC_KEY_SIMILARITY_THRESHOLD


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
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
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
        is_technical_tutorial = _has_any(text, _TECHNICAL_TUTORIAL_KEYWORDS) and not _has_any(text, _MAJOR_AI_BRANDS + ["product hunt", "chatgpt", "consumer", "app", "tool", "video", "image", "audio"])
        if _has_any(text, ["guide", "tutorial", "prompt", "course", "курс", "гайд", "how to"]):
            if is_technical_tutorial:
                score -= 22
                category = "dev"
                reason_parts.append("штраф: узкий devops/tutorial")
            else:
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
        if _has_any(text, _MAJOR_AI_BRANDS + ["gpt", "claude", "gemini", "llama", "mistral", "qwen", "model"]):
            if _has_any(text, ["app", "tool", "api", "free", "voice", "image", "video", "faster", "cheaper", "local"]):
                score += 12
                reason_parts.append("модель с практическим смыслом")
            else:
                score += 6
            if category == "other":
                category = "model"
        if _has_any(text, _TECHNICAL_TUTORIAL_KEYWORDS) and not _has_any(text, _MAJOR_AI_BRANDS + ["product hunt", "consumer", "app", "tool", "image", "video", "audio", "agent"]):
            score -= 18
            if category == "other":
                category = "dev"
            reason_parts.append("штраф: слишком техническая тема")
        if "marktechpost" in text and _has_any(text, _TECHNICAL_TUTORIAL_KEYWORDS + ["tutorial", "how to design", "how to build"]):
            score -= 16
            reason_parts.append("штраф: MarkTechPost tutorial/devops")
        if _has_any(text, _MAJOR_AI_BRANDS) and _has_any(text, ["launch", "launched", "release", "released", "introduces", "unveils", "new", "update", "tool", "feature"]):
            score += 16
            reason_parts.append("крупный релиз AI-продукта")
        if source_group == "tools" and _has_any(text, ["product hunt", "app", "tool", "service", "workflow", "image", "video", "audio", "assistant"]):
            score += 10
            if category == "other":
                category = "tool"
            reason_parts.append("практичный AI-инструмент")
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
        if source_group == "telegram":
            if _has_any(text, ["demo", "workflow", "use case", "кейс", "инструмент", "сервис", "гайд", "ссылка", "thread"]):
                score += 6
            if _has_any(text, ["подписывай", "join", "реклама", "promo", "розыгрыш", "airdrop", "casino", "ваканси", "webinar", "вебинар"]):
                score -= 18
                reason_parts.append("штраф: telegram promo/bait")

    parsed_dt = _parse_score_datetime(published_at)
    if parsed_dt:
        age_days = (datetime.now(timezone.utc) - parsed_dt).days
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




def editorial_lane_for_topic(
    title: str,
    source: str,
    url: str,
    source_group: str,
    description: str | None,
    category: str | None,
    score: int,
) -> tuple[str, str]:
    text = f"{title} {source} {url} {description or ''}".lower()
    category = (category or "other").strip().lower()
    source_group = (source_group or "other").strip().lower()

    if _has_any(text, ["meme", "юмор", "мем", "shitpost", "weird", "strange", "viral"]):
        return "meme", "вирусный или необычный формат, хорошо зайдет как развлекательный пост"
    if _has_any(text, ["shorts", "reels", "tiktok", "demo", "before/after", "до/после", "кейс"]) and score >= 58:
        return "short_video", "можно быстро показать в коротком ролике"
    if category == "guide" or _has_any(text, ["guide", "tutorial", "prompt", "workflow", "гайд", "инструкция"]):
        return "guide", "практический формат: можно дать понятные шаги"
    if _has_any(text, ["image", "video", "audio", "voice", "avatar", "design", "music", "creator"]):
        return "creator", "инструмент для креаторов, легко показать результат"
    if category in {"tool", "mobile"} or _has_any(text, ["tool", "service", "app", "extension", "plugin", "сервис", "инструмент"]):
        return "tool", "полезный сервис для новичков"
    if source_group == "github":
        if _has_any(text, ["api", "sdk", "library", "framework", "benchmark", "cli"]) and not _has_any(text, _GITHUB_PRACTICAL_KEYWORDS):
            return "dev", "в основном для разработчиков, узкая прикладная ценность"
        return "tool", "можно разобрать как полезный open-source инструмент"
    if category in {"business"} or _has_any(text, _CORPORATE_KEYWORDS):
        if _has_any(text, _USER_IMPACT_KEYWORDS + ["price", "pricing", "free", "chatgpt", "gemini", "claude"]):
            return "breaking_news", "новость влияет на повседневное использование AI-сервисов"
        return "business", "похоже на корпоративную новость, низкий приоритет"
    if category in {"research", "model"} or _has_any(text, _RESEARCH_KEYWORDS + ["paper", "benchmark"]):
        return "research", "исследовательская тема, нужна адаптация под массовую аудиторию"
    if _has_any(text, ["launch", "release", "model", "chatgpt", "openai", "claude", "gemini", "llama"]) and score >= 70:
        return "breaking_news", "важное обновление AI-продукта с пользовательским эффектом"
    if category in {"agent", "dev"}:
        return "dev", "скорее техническая тема для разработчиков"
    if score < 55:
        return "low_value", "слабый сигнал: не видно явной пользы для @simplify_ai"
    return "short_video", "можно подать как короткий практический разбор"


def content_format_for_lane(lane: str, score: int) -> str:
    lane = (lane or "").strip().lower()
    if lane in {"tool", "creator"}:
        return "tool_review"
    if lane == "short_video":
        return "short_video"
    if lane == "meme":
        return "meme"
    if lane == "guide":
        return "guide"
    if lane == "breaking_news":
        return "news"
    if lane in {"business", "dev", "research", "low_value"}:
        return "post"
    return "post"

def hybrid_topic_score(deterministic_score: int, ai_score: int | None) -> int:
    """Blend deterministic and AI topic value scores conservatively.

    The deterministic score remains the baseline. AI may move it only through
    the weighted blend and never by more than 15 points in either direction.
    """
    baseline = max(0, min(100, int(deterministic_score or 0)))
    if ai_score is None:
        return baseline
    try:
        ai_value = int(ai_score)
    except (TypeError, ValueError):
        return baseline
    ai_value = max(0, min(100, ai_value))
    weighted = round(baseline * 0.65 + ai_value * 0.35)
    clamped_delta = max(-15, min(15, weighted - baseline))
    return max(0, min(100, baseline + clamped_delta))

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
