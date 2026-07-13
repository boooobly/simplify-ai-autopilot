"""Source audit inventory derived from the runtime source registry."""

from dataclasses import dataclass

from bot.sources import (
    OFFICIAL_AI_RSS,
    RU_TECH_RSS,
    TECH_MEDIA_RSS,
    TOOLS_RSS,
    VC_RU_AI_SOURCE,
)


@dataclass(frozen=True)
class SourceCandidate:
    source_type: str
    group: str
    name: str
    url: str
    priority: str
    note: str = ""


def _runtime_rss_candidates() -> list[SourceCandidate]:
    groups = [
        ("official_ai", OFFICIAL_AI_RSS, "high"),
        ("tech_media", TECH_MEDIA_RSS, "high"),
        ("ru_tech", RU_TECH_RSS, "medium"),
        ("tools", TOOLS_RSS, "high"),
    ]
    return [
        SourceCandidate("rss", group, name, url, priority, "Runtime built-in source")
        for group, feeds, priority in groups
        for name, url in feeds
    ]


_vc_name, _vc_url, _vc_group = VC_RU_AI_SOURCE
CANDIDATE_SOURCES = [
    SourceCandidate("html", _vc_group, _vc_name, _vc_url, "high", "Runtime HTML source"),
    *_runtime_rss_candidates(),
    SourceCandidate(
        "github",
        "github",
        "GitHub Trending AI",
        "https://github.com/trending?spoken_language_code=&since=daily",
        "high",
        "Runtime HTML collector",
    ),
    # Exploratory sources remain separate from runtime until they pass a live
    # health/noise review and are deliberately added to bot.sources.
    SourceCandidate("rss", "community", "Hacker News AI search", "https://hnrss.org/newest?q=AI", "medium", "Exploratory; dev-heavy"),
    SourceCandidate("rss", "community", "Hacker News LLM search", "https://hnrss.org/newest?q=LLM", "medium", "Exploratory; dev-heavy"),
    SourceCandidate("rss_or_html", "tools", "Runway research", "https://runwayml.com/research/", "medium", "Exploratory"),
    SourceCandidate("rss_or_html", "tools", "ElevenLabs blog", "https://elevenlabs.io/blog", "medium", "Exploratory"),
]
