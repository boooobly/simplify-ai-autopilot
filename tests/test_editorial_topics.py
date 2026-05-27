from bot.topic_scoring import editorial_lane_for_topic, content_format_for_lane
from bot.topic_display import topic_display_reason
from bot.database import DraftDatabase
import tempfile


def test_editorial_lane_classification():
    assert editorial_lane_for_topic('New AI app for notes', 'Tech', 'https://a', 'tools', 'useful tool', 'tool', 80)[0] == 'tool'
    assert editorial_lane_for_topic('AI video avatar generator', 'Tech', 'https://a', 'tools', 'video creator', 'creator', 82)[0] == 'creator'
    assert editorial_lane_for_topic('Weird viral AI demo on TikTok', 'X', 'https://a', 'x', 'viral weird demo', 'meme', 78)[0] in {'short_video', 'meme'}
    assert editorial_lane_for_topic('Startup raises Series B', 'News', 'https://a', 'tech_media', 'funding round', 'business', 55)[0] in {'business', 'low_value'}
    assert editorial_lane_for_topic('GitHub Trending: llm-sdk', 'GitHub', 'https://a', 'github', 'sdk bindings', 'dev', 62)[0] == 'dev'
    assert editorial_lane_for_topic('Prompt guide for ChatGPT', 'Blog', 'https://a', 'community', 'tutorial', 'guide', 72)[0] == 'guide'


def test_content_format_mapping():
    assert content_format_for_lane('tool', 80) == 'tool_review'
    assert content_format_for_lane('short_video', 70) == 'short_video'
    assert content_format_for_lane('meme', 70) == 'meme'


def test_balanced_shortlist_limits_duplicates():
    with tempfile.TemporaryDirectory() as tmp:
        db = DraftDatabase(f"{tmp}/t.db")
        for i in range(20):
            db.upsert_topic_candidate_with_reason(
                title=f'Tool {i}', url=f'https://x{i}.com', source='SameSource', published_at=None,
                category='tool', score=90-i, reason='ok', normalized_title=f'tool {i}', source_group='tools', original_description='tool app'
            )
        items = db.get_balanced_topic_shortlist(limit=12, hours=48, min_score=10)
        assert len(items) <= 12
        assert sum(1 for t in items if t.get('source') == 'SameSource') <= 3


def test_old_rows_no_editorial_break():
    with tempfile.TemporaryDirectory() as tmp:
        db = DraftDatabase(f"{tmp}/t.db")
        db.create_topic_candidate('Title', 'https://old.com', 'src', None)
        row = db.find_topic_candidate_by_url('https://old.com')
        assert row is not None
        topic_display_reason(row)
