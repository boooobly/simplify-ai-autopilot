from __future__ import annotations
import argparse
from bot.source_candidates import CANDIDATE_SOURCES
from bot.sources import OFFICIAL_AI_RSS, TECH_MEDIA_RSS, RU_TECH_RSS, TOOLS_RSS, discover_rss_feed_url, fetch_vc_ru_ai_topics
import requests

def check_rss(name: str, url: str):
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        ok = ("<item" in r.text) or ("<entry" in r.text)
        return ("ok" if ok else "empty", 1 if ok else 0, "")
    except Exception as e:
        return ("error", 0, str(e)[:120])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["high", "medium", "low"])
    ap.add_argument("--group")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()
    print("group | priority | type | name | url | status | item_count | error")
    for c in CANDIDATE_SOURCES:
        if args.only and c.priority != args.only:
            continue
        if args.group and c.group != args.group:
            continue
        if c.source_type == "rss":
            status, count, err = check_rss(c.name, c.url)
        elif c.source_type == "html" and "vc.ru/ai" in c.url:
            items, rep = fetch_vc_ru_ai_topics(max_items=args.limit)
            status, count, err = rep.status, len(items), rep.error
        elif c.source_type == "rss_or_html":
            found, derr = discover_rss_feed_url(c.url)
            if found:
                status, count, err = check_rss(c.name, found)
            else:
                status, count, err = "empty", 0, derr
        else:
            status, count, err = "empty", 0, "not checked"
        print(f"{c.group} | {c.priority} | {c.source_type} | {c.name} | {c.url} | {status} | {count} | {err}")
    for group_name, rss_list in [("official_ai", OFFICIAL_AI_RSS), ("tech_media", TECH_MEDIA_RSS), ("ru_tech", RU_TECH_RSS), ("tools", TOOLS_RSS)]:
        for name, url in rss_list:
            status, count, err = check_rss(name, url)
            print(f"{group_name} | builtin | rss | {name} | {url} | {status} | {count} | {err}")

if __name__ == "__main__":
    main()
