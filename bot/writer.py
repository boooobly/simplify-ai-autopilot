"""OpenAI-powered draft generation for Telegram posts."""

from __future__ import annotations

from pathlib import Path

from openai import OpenAI

STYLE_PATH = Path("prompts/post_style.md")


def _load_style_prompt() -> str:
    return STYLE_PATH.read_text(encoding="utf-8").strip()


def generate_post_draft(api_key: str, source_url: str | None = None) -> str:
    """Generate a Russian Telegram post draft for @simplify_ai."""

    client = OpenAI(api_key=api_key)
    style = _load_style_prompt()

    source_line = f"Источник: {source_url}" if source_url else "Источник: не указан"
    user_prompt = (
        "Создай один черновик поста для Telegram-канала @simplify_ai. "
        "Верни только готовый текст поста, без пояснений, без markdown-блока и без служебных комментариев. "
        f"{source_line}"
    )

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": style},
            {"role": "user", "content": user_prompt},
        ],
        max_output_tokens=700,
    )

    text = response.output_text.strip()
    if len(text) > 900:
        text = text[:897].rstrip() + "..."
    return text
