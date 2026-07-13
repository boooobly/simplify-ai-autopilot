import asyncio
from types import SimpleNamespace

import pytest

from bot import writer
from bot import handlers
from bot.handlers import _resolve_ai_request
from bot.writer import CompletionFallback, EmptyAIResponseError, GenerationResult


def _settings(**overrides):
    values = {
        "openrouter_api_key": "or-key",
        "openai_api_key": "oa-key",
        "openrouter_app_name": "Simplify AI Autopilot",
        "openrouter_site_url": "https://example.com",
        "model_draft": "openrouter/draft-model",
        "model_topic_enrich": "openrouter/topic-model",
        "model_polish": "openrouter/polish-model",
        "openai_model_draft": "openai-draft-model",
        "openai_model_topic_enrich": "openai-topic-model",
        "openai_model_polish": "openai-polish-model",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_openrouter_route_has_provider_compatible_openai_fallback():
    route = _resolve_ai_request(_settings(), "topic_enrich")

    assert route.provider == "openrouter"
    assert route.model == "openrouter/topic-model"
    assert route.base_url == "https://openrouter.ai/api/v1"
    assert route.extra_headers == {
        "X-Title": "Simplify AI Autopilot",
        "HTTP-Referer": "https://example.com",
    }
    assert route.fallback is not None
    assert route.fallback.provider == "openai"
    assert route.fallback.model == "openai-topic-model"
    assert "or-key" not in repr(route)
    assert "oa-key" not in repr(route.fallback)


def test_openai_only_route_never_reuses_openrouter_model_id():
    route = _resolve_ai_request(_settings(openrouter_api_key=""), "draft")

    assert route.provider == "openai"
    assert route.model == "openai-draft-model"
    assert route.base_url is None
    assert route.fallback is None


def test_completion_retries_independent_provider_after_recoverable_failure(monkeypatch):
    calls = []

    def fake_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise EmptyAIResponseError("empty primary response")
        return GenerationResult(
            content="готовый ответ",
            model=kwargs["model"],
            provider=kwargs["provider"],
        )

    monkeypatch.setattr(writer, "_generate_chat_completion_once", fake_once)
    result = writer._generate_with_chat_completion(
        api_key="or-key",
        model="openrouter-model",
        user_prompt="user",
        system_prompt="system",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        fallback=CompletionFallback(
            api_key="oa-key",
            model="openai-model",
            provider="openai",
        ),
    )

    assert [call["provider"] for call in calls] == ["openrouter", "openai"]
    assert [call["model"] for call in calls] == ["openrouter-model", "openai-model"]
    assert result.provider == "openai"
    assert result.model == "openai-model"


def test_completion_does_not_hide_non_recoverable_request_bug(monkeypatch):
    calls = []

    def fake_once(**kwargs):
        calls.append(kwargs)
        raise ValueError("invalid request built by application")

    monkeypatch.setattr(writer, "_generate_chat_completion_once", fake_once)
    with pytest.raises(ValueError, match="invalid request"):
        writer._generate_with_chat_completion(
            api_key="or-key",
            model="openrouter-model",
            user_prompt="user",
            system_prompt="system",
            provider="openrouter",
            fallback=CompletionFallback(
                api_key="oa-key",
                model="openai-model",
                provider="openai",
            ),
        )

    assert len(calls) == 1


def test_generation_usage_is_charged_to_provider_that_actually_succeeded(monkeypatch):
    settings = _settings(
        has_ai_provider=True,
        post_max_chars=1400,
        post_soft_chars=1100,
        openrouter_input_cost_per_1m=1.0,
        openrouter_output_cost_per_1m=1.0,
        openai_input_cost_per_1m=2.0,
        openai_output_cost_per_1m=3.0,
        admin_id=42,
    )
    usage_rows = []

    class FakeDB:
        def create_draft(self, content, **kwargs):
            assert content == "готовый черновик"
            return 7

        def record_ai_usage(self, **kwargs):
            usage_rows.append(kwargs)

    class FakeMessage:
        async def reply_text(self, *_args, **_kwargs):
            return None

    async def fake_generate(*_args, **_kwargs):
        return GenerationResult(
            content="готовый черновик",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="openai-draft-model",
            provider="openai",
        )

    async def fake_preview(*_args, **_kwargs):
        return None

    monkeypatch.setattr(handlers, "_run_generate_post_draft", fake_generate)
    monkeypatch.setattr(handlers, "_send_moderation_preview", fake_preview)

    asyncio.run(
        handlers._generate_from_command(
            SimpleNamespace(),
            settings,
            FakeDB(),
            None,
            FakeMessage(),
        )
    )

    assert usage_rows[0]["provider"] == "openai"
    assert usage_rows[0]["model"] == "openai-draft-model"
    assert usage_rows[0]["estimated_cost_usd"] == pytest.approx(0.00035)
