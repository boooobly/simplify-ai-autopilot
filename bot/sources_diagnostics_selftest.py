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
            SourceReport(name="Reddit community RSS", url="https://reddit", source_group="community", status="skipped", error="Reddit sources disabled by config"),
            SourceReport(name="X API", url="https://api.x.com/2", source_group="x", status="skipped", error="X sources disabled"),
        ]
    )
    assert "Работают" in text
    assert "Ошибки" in text
    assert "B" in text
    assert "Отключены/пропущены" in text
    assert "Reddit sources disabled" in text
    assert "X API" in text


if __name__ == "__main__":
    run()
    print("OK")
