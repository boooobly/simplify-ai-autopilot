from bot.sources import _parse_rss
from bot.topic_scoring import score_topic


def test_rss_item_uses_description_html_cleaning():
    xml = """<rss><channel><item>
    <title>Weekly AI round-up</title>
    <link>https://example.com/1</link>
    <description><![CDATA[<p>Read more: <b>New free AI tool</b> for image editing and automation.</p>]]></description>
    </item></channel></rss>"""
    items = _parse_rss(xml, "Feed", "tech_media")
    assert len(items) == 1
    assert items[0].original_description is not None
    assert "<" not in items[0].original_description
    assert "free AI tool" in items[0].original_description


def test_atom_entry_uses_summary():
    xml = """<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>
    <entry>
      <title>Small update</title>
      <link href='https://example.com/atom'/>
      <summary>Useful automation agent for creators and mobile app teams.</summary>
    </entry></feed>"""
    items = _parse_rss(xml, "Atom", "community")
    assert len(items) == 1
    assert items[0].original_description == "Useful automation agent for creators and mobile app teams."


def test_description_can_change_score_when_title_is_weak():
    title = "Weekly roundup"
    weak_score, _, _ = score_topic(title, "Feed", "https://example.com/x", "tech_media", description=None)
    strong_score, category, _ = score_topic(
        title,
        "Feed",
        "https://example.com/x",
        "tech_media",
        description="Free open-source AI tool and automation app with launch demo for creators",
    )
    assert strong_score > weak_score
    assert category in {"tool", "creator", "news", "mobile", "agent"}


def test_empty_or_malformed_description_fallback():
    xml = """<rss><channel><item>
    <title>AI launch</title><link>https://example.com/2</link>
    <description><![CDATA[<div><span>   </span></div>]]></description>
    </item></channel></rss>"""
    items = _parse_rss(xml, "Feed", "tech_media")
    assert len(items) == 1
    assert items[0].original_description is None

    valid_no_desc = """<rss><channel><item><title>AI launch</title><link>https://example.com/3</link></item></channel></rss>"""
    parsed = _parse_rss(valid_no_desc, "Feed", "tech_media")
    assert len(parsed) == 1
    assert parsed[0].original_description is None
