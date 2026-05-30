from bot import writer
from bot.writer import GenerationResult


def test_topic_metadata_length_finish_reason_is_diagnosed_as_truncated(monkeypatch):
    calls = []

    def _fake_generate(**kwargs):
        calls.append(kwargs)
        return GenerationResult(
            content='{"title_ru":"Обрезанный ответ"',
            model=kwargs["model"],
            finish_reason="length",
        )

    monkeypatch.setattr(writer, "_generate_with_chat_completion", _fake_generate)
    diagnostics = {}

    result = writer.enrich_topic_metadata_ru(
        api_key="key",
        model="deepseek/test",
        title="Test topic",
        source="Test",
        diagnostics=diagnostics,
    )

    assert result is None
    assert calls[0]["max_tokens"] == 1200
    assert diagnostics == {"ai_output_truncated": 1}
