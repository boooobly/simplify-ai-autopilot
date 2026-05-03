from bot.handlers import _select_daily_plan_topics


class FakeDB:
    def __init__(self, topics):
        self._topics = topics

    def list_topic_candidates(self, limit=50, status="new", order_by_score=True):
        assert status == "new"
        return self._topics[:limit]


def run() -> None:
    topics = [
        {"id": 1, "score": 95, "category": "tool", "source_group": "tools", "title": "t1", "source": "s", "url": "u1"},
        {"id": 2, "score": 93, "category": "news", "source_group": "tech_media", "title": "t2", "source": "s", "url": "u2"},
        {"id": 3, "score": 91, "category": "meme", "source_group": "community", "title": "t3", "source": "s", "url": "u3"},
        {"id": 4, "score": 90, "category": "tool", "source_group": "tools", "title": "t4", "source": "s", "url": "u4"},
        {"id": 5, "score": 89, "category": "model", "source_group": "official_ai", "title": "t5", "source": "s", "url": "u5"},
    ]
    db = FakeDB(topics)
    selected = _select_daily_plan_topics(db, limit=4)
    assert len(selected) == 4
    ids = [int(t["id"]) for t in selected]
    assert len(ids) == len(set(ids))

    same_group_topics = [
        {"id": 11, "score": 99, "category": "tool", "source_group": "tools", "title": "a", "source": "s", "url": "ua"},
        {"id": 12, "score": 98, "category": "tool", "source_group": "tools", "title": "b", "source": "s", "url": "ub"},
        {"id": 13, "score": 97, "category": "tool", "source_group": "tools", "title": "c", "source": "s", "url": "uc"},
        {"id": 14, "score": 96, "category": "tool", "source_group": "tools", "title": "d", "source": "s", "url": "ud"},
    ]
    same_group_selected = _select_daily_plan_topics(FakeDB(same_group_topics), limit=4)
    same_group_ids = [int(t["id"]) for t in same_group_selected]
    assert len(same_group_selected) == 4
    assert len(same_group_ids) == len(set(same_group_ids))

    duplicate_topics = [
        {"id": 21, "score": 99, "category": "tool", "source_group": "tools", "title": "a", "source": "s", "url": "ua"},
        {"id": 21, "score": 98, "category": "news", "source_group": "tech_media", "title": "b", "source": "s", "url": "ub"},
        {"id": 22, "score": 97, "category": "model", "source_group": "official_ai", "title": "c", "source": "s", "url": "uc"},
        {"id": 23, "score": 96, "category": "meme", "source_group": "community", "title": "d", "source": "s", "url": "ud"},
    ]
    duplicate_selected = _select_daily_plan_topics(FakeDB(duplicate_topics), limit=3)
    duplicate_ids = [int(t["id"]) for t in duplicate_selected]
    assert len(duplicate_selected) == 3
    assert len(duplicate_ids) == len(set(duplicate_ids))

    relaxed_fill_topics = [
        {"id": 31, "score": 99, "category": "tool", "source_group": "tools", "title": "a", "source": "s", "url": "u1"},
        {"id": 32, "score": 98, "category": "tool", "source_group": "tools", "title": "b", "source": "s", "url": "u2"},
        {"id": 33, "score": 97, "category": "tool", "source_group": "tools", "title": "c", "source": "s", "url": "u3"},
        {"id": 34, "score": 96, "category": "tool", "source_group": "tools", "title": "d", "source": "s", "url": "u4"},
        {"id": 35, "score": 95, "category": "tool", "source_group": "tools", "title": "e", "source": "s", "url": "u5"},
    ]
    relaxed_selected = _select_daily_plan_topics(FakeDB(relaxed_fill_topics), limit=5)
    assert len(relaxed_selected) == 5


if __name__ == "__main__":
    run()
    print("OK")
