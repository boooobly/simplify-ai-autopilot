from dataclasses import dataclass

@dataclass(frozen=True)
class SourceCandidate:
    source_type: str
    group: str
    name: str
    url: str
    priority: str
    note: str = ""

CANDIDATE_SOURCES = [
    SourceCandidate("html", "ru_tech", "vc.ru AI", "https://vc.ru/ai", "high", "Important manual source for admin"),
    SourceCandidate("rss", "ru_tech", "vc.ru all RSS", "https://vc.ru/rss/all", "medium", "Already broad; useful for comparison but noisy"),
    SourceCandidate("rss", "ru_tech", "Habr AI", "https://habr.com/ru/rss/hubs/ai/all/", "medium", "Validate, may be broken or dev-heavy"),
    SourceCandidate("rss", "ru_tech", "Habr Machine Learning", "https://habr.com/ru/rss/hub/machine_learning/", "medium", "Validate, often technical"),
    SourceCandidate("rss", "ru_tech", "Tproger", "https://tproger.ru/feed", "medium"),
    SourceCandidate("rss", "official_ai", "OpenAI blog", "https://openai.com/news/rss.xml", "high"),
    SourceCandidate("rss", "official_ai", "Anthropic news", "https://www.anthropic.com/news/rss.xml", "high", "Validate because it may return 404"),
    SourceCandidate("rss", "official_ai", "Google AI blog", "https://blog.google/technology/ai/rss/", "high"),
    SourceCandidate("rss", "official_ai", "Google DeepMind blog", "https://deepmind.google/discover/blog/rss.xml", "medium", "Validate before adding"),
    SourceCandidate("rss", "official_ai", "Hugging Face blog", "https://huggingface.co/blog/feed.xml", "high"),
    SourceCandidate("rss", "official_ai", "Microsoft AI blog", "https://blogs.microsoft.com/ai/feed/", "medium"),
    SourceCandidate("rss", "official_ai", "NVIDIA AI blog", "https://blogs.nvidia.com/blog/category/ai/feed/", "medium", "Validate because it may return 404"),
    SourceCandidate("rss", "tech_media", "TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", "high"),
    SourceCandidate("rss", "tech_media", "The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "high"),
    SourceCandidate("rss", "tech_media", "MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/", "medium"),
    SourceCandidate("rss", "tech_media", "Ars Technica AI", "https://arstechnica.com/ai/feed/", "medium"),
    SourceCandidate("rss", "tech_media", "The Decoder", "https://the-decoder.com/feed/", "high"),
    SourceCandidate("rss", "tech_media", "VentureBeat AI", "https://venturebeat.com/ai/feed/", "medium", "Validate because it may return 404"),
    SourceCandidate("rss", "tech_media", "MarkTechPost", "https://www.marktechpost.com/feed/", "low", "Validate carefully; may be noisy or broken XML"),
    SourceCandidate("rss", "tools", "Product Hunt", "https://www.producthunt.com/feed", "high"),
    SourceCandidate("github", "github", "GitHub Trending AI", "https://github.com/trending?spoken_language_code=&since=daily", "high", "Already collected separately; keep as current logic"),
    SourceCandidate("rss", "community", "Hacker News AI search", "https://hnrss.org/newest?q=AI", "medium", "Good early signal but dev-heavy"),
    SourceCandidate("rss", "community", "Hacker News LLM search", "https://hnrss.org/newest?q=LLM", "medium"),
    SourceCandidate("rss", "community", "Hacker News OpenAI search", "https://hnrss.org/newest?q=OpenAI", "medium"),
    SourceCandidate("rss_or_html", "tools", "Runway blog", "https://runwayml.com/research/", "medium", "Validate RSS discovery or HTML parser need"),
    SourceCandidate("rss_or_html", "tools", "Luma AI blog/news", "https://lumalabs.ai/", "medium", "Validate only, do not add if no clean feed"),
    SourceCandidate("rss_or_html", "tools", "ElevenLabs blog", "https://elevenlabs.io/blog", "medium", "Validate RSS discovery or HTML parser need"),
    SourceCandidate("rss_or_html", "tools", "Replicate blog", "https://replicate.com/blog", "medium", "Useful for models/tools, validate feed discovery"),
]
