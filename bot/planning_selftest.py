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


if __name__ == "__main__":
    run()
    print("OK")
