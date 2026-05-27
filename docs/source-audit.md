# Built-in source audit (May 27, 2026)

Validation command used:

- `PYTHONPATH=. python scripts/check_builtin_feeds.py`

Environment note: in CI/container this run is currently blocked by outbound proxy tunnel errors for almost all HTTP(S) sources, so statuses below are based on **observed script output in this environment** and are conservative.

| Source | Group | URL | Status | Action | Reason |
|---|---|---|---|---|---|
| OpenAI blog | official_ai | https://openai.com/news/rss.xml | error (proxy blocked in env) | watch | Fetch failed due proxy tunnel in validation environment; no source-level breakage proven. |
| Anthropic news | official_ai | https://www.anthropic.com/news/rss.xml | error (proxy blocked in env) | watch | Same proxy limitation; cannot confirm broken feed itself. |
| Google AI blog | official_ai | https://blog.google/technology/ai/rss/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| Perplexity blog | official_ai | https://www.perplexity.ai/hub/blog/rss.xml | error (proxy blocked in env) | watch | Same proxy limitation. |
| Hugging Face blog | official_ai | https://huggingface.co/blog/feed.xml | error (proxy blocked in env) | watch | Same proxy limitation. |
| Microsoft AI blog | official_ai | https://blogs.microsoft.com/ai/feed/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| NVIDIA blog AI | official_ai | https://blogs.nvidia.com/blog/category/ai/feed/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| VentureBeat AI | tech_media | https://venturebeat.com/ai/feed/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| The Decoder | tech_media | https://the-decoder.com/feed/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| MarkTechPost | tech_media | https://www.marktechpost.com/feed/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| The Verge AI | tech_media | https://www.theverge.com/rss/ai-artificial-intelligence/index.xml | error (proxy blocked in env) | watch | Same proxy limitation. |
| TechCrunch AI | tech_media | https://techcrunch.com/category/artificial-intelligence/feed/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| MIT Technology Review AI | tech_media | https://www.technologyreview.com/topic/artificial-intelligence/feed/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| Ars Technica AI | tech_media | https://arstechnica.com/ai/feed/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| Habr AI | ru_tech | https://habr.com/ru/rss/hubs/ai/all/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| Habr ML | ru_tech | https://habr.com/ru/rss/hub/machine_learning/ | error (proxy blocked in env) | watch | Same proxy limitation. |
| Habr Dev | ru_tech | https://habr.com/ru/rss/all/all/?fl=ru | error (proxy blocked in env) | watch | Same proxy limitation. |
| vc.ru technology | ru_tech | https://vc.ru/rss/all | error (proxy blocked in env) | watch | Broad/noisy source by design; keep under watch pending external run. |
| vc.ru AI | ru_tech | https://vc.ru/ai | error (proxy blocked in env) | keep | Must stay enabled; source was intentionally added in prior PR. |
| Tproger | ru_tech | https://tproger.ru/feed | error (proxy blocked in env) | watch | Same proxy limitation. |
| 3DNews | ru_tech | https://3dnews.ru/news/rss | error (proxy blocked in env) | watch | Same proxy limitation. |
| iXBT | ru_tech | https://www.ixbt.com/export/news.rss | error (proxy blocked in env) | watch | Same proxy limitation. |
| Product Hunt | tools | https://www.producthunt.com/feed | error (proxy blocked in env) | watch | Same proxy limitation. |
| Reddit community RSS (built-in list) | community | https://www.reddit.com/r/*/.rss | skipped by config default | keep | Behavior unchanged; governed by `ENABLE_REDDIT_SOURCES`. |
