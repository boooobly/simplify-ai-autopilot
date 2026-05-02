"""Draft generation and rewrite helpers."""

from __future__ import annotations


def create_test_draft() -> str:
    """Return a simple starter draft used by /draft command."""

    return (
        "⚡️ Тестовый пост для канала\n\n"
        "Проверяем новую систему модерации.\n\n"
        "➖ бот создаёт черновик\n"
        "➖ присылает его на проверку\n"
        "➖ ты нажимаешь кнопку\n"
        "➖ пост уходит в канал\n\n"
        "Пока это тест, но основа уже работает 💭"
    )


def rewrite_test_draft(original: str) -> str:
    """Return a basic rewritten version without external AI services."""

    return (
        "✍️ Обновлённая версия черновика\n\n"
        f"{original}\n\n"
        "Формулировку чуть упростил и сделал ближе к стилю канала."
    )
