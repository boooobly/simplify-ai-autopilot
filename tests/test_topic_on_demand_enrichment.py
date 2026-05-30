import asyncio
import json
from types import SimpleNamespace

from bot import handlers
from bot.database import DraftDatabase
from bot.topic_display import is_weak_topic_metadata, topic_summary_ru
from bot.writer import GenerationResult, enrich_topic_understanding_ru


def _db(tmp_path):
    return DraftDatabase(str(tmp_path / "topics.db"))


def _topic(db):
    db.create_topic_candidate(
        "Fed up with vibe coders, dev sneaks data-nuking prompt injection into their code",
        "https://example.com/story",
        "Ars Technica AI",
        None,
    )
    return db.find_topic_candidate_by_url("https://example.com/story")


def _settings(**overrides):
    values = dict(has_ai_provider=True, openai_api_key="test", openrouter_api_key="", xai_api_key="",
                  openrouter_base_url="", xai_base_url="", ai_provider="openai",
                  model_topic_enrich="test-model", model_draft="draft",
                  openai_input_cost_per_1m=0, openai_output_cost_per_1m=0,
                  openrouter_input_cost_per_1m=0, openrouter_output_cost_per_1m=0)
    values.update(overrides)
    return SimpleNamespace(**values)


def _good_result():
    return GenerationResult(content=json.dumps({
        "title_ru": "Разработчик спрятал опасный промпт в коде",
        "summary_ru": "Разработчик добавил скрытую инструкцию, которая могла заставить AI-ассистента удалить данные. Это пример риска prompt injection.",
        "angle_ru": "Объяснить простыми словами, почему нельзя слепо запускать предложенный AI код.",
    }, ensure_ascii=False), model="test-model")


def test_weak_metadata_detection_rejects_old_wrappers():
    title = "Новость от Ars Technica AI: English title"
    angle = "Сфокусироваться не на пресс-релизе, а на пользе."
    assert is_weak_topic_metadata(title, 'Источник Ars Technica AI пишет про тему: "English title".', angle, original_title="English title")
    assert is_weak_topic_metadata(title, "Нужна проверка деталей", angle, original_title="English title")
    assert is_weak_topic_metadata(title, "Нужна проверка README", angle, original_title="English title")


def test_opening_weak_topic_enriches_on_demand_without_bulk_limit(monkeypatch, tmp_path):
    db = _db(tmp_path)
    topic = _topic(db)
    calls = []
    async def fake(**kwargs):
        calls.append(kwargs)
        return _good_result()
    monkeypatch.setattr(handlers, "_run_enrich_topic_understanding_ru", fake)
    settings = _settings(topic_ai_enrich_limit=0)
    updated = asyncio.run(handlers._ensure_topic_candidate_display_metadata(int(topic["id"]), settings, db))
    assert len(calls) == 1
    assert updated["metadata_source"] == "ai_on_demand"
    assert "скрытую инструкцию" in topic_summary_ru(updated)


def test_manual_on_demand_force_reenriches_good_metadata(monkeypatch, tmp_path):
    db = _db(tmp_path)
    topic = _topic(db)
    db.force_update_topic_candidate_display_fields(int(topic["id"]), title_ru="Понятный русский заголовок", summary_ru="Это подробное русское объяснение важного события для читателя.", angle_ru="Рассказать читателю, что изменилось и как это влияет на работу.", reason_ru="")
    calls = []
    async def fake(**kwargs):
        calls.append(kwargs)
        return _good_result()
    monkeypatch.setattr(handlers, "_run_enrich_topic_understanding_ru", fake)
    updated, error = asyncio.run(handlers._reenrich_topic_candidate_display_metadata(int(topic["id"]), _settings(), db))
    assert error is None
    assert len(calls) == 1
    assert updated["metadata_source"] == "ai_on_demand"
    labels = [button.text for row in handlers._topic_actions_keyboard(int(topic["id"])).inline_keyboard for button in row]
    assert "🧠 Понять тему через AI" in labels


def test_unavailable_ai_uses_honest_fallback(tmp_path):
    db = _db(tmp_path)
    topic = _topic(db)
    updated = asyncio.run(handlers._ensure_topic_candidate_display_metadata(int(topic["id"]), _settings(has_ai_provider=False), db))
    assert updated["_ai_enrichment_attempted"] is True
    assert "Не удалось нормально объяснить тему автоматически" in topic_summary_ru(updated)
    assert "Оригинальный заголовок" in topic_summary_ru(updated)


def test_compact_on_demand_contract_accepts_only_required_fields(monkeypatch):
    monkeypatch.setattr("bot.writer._generate_with_chat_completion", lambda **kwargs: _good_result())
    result = enrich_topic_understanding_ru(api_key="x", model="m", title="English title", source="Source")
    assert result is not None
    assert set(json.loads(result.content)) == {"title_ru", "summary_ru", "angle_ru"}
