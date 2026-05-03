from bot.handlers import _render_sources_status
from bot.sources import SourceReport, collect_topics_with_diagnostics


def run() -> None:
    report = SourceReport(name="Test", url="https://example.com", source_group="custom", status="ok", item_count=1)
    assert report.name == "Test"

    assert callable(collect_topics_with_diagnostics)

    text = _render_sources_status(
        [
            SourceReport(name="A", url="https://a", source_group="official_ai", status="ok", item_count=3),
            SourceReport(name="B", url="https://b", source_group="community", status="error", error="timeout"),
            SourceReport(name="C", url="https://c", source_group="tools", status="empty", item_count=0),
        ]
    )
    assert "Работают" in text
    assert "Ошибки" in text
    assert "B" in text


if __name__ == "__main__":
    run()
    print("OK")
