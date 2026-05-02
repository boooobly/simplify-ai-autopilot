"""Draft generation and rewrite helpers."""

from __future__ import annotations


def create_test_draft() -> str:
    """Return a simple starter draft used by /draft command."""

    return (
        "🚀 Test post for our AI Telegram channel\n\n"
        "Today we're testing our new moderation flow. "
        "Soon this bot will help us review and publish content faster!"
    )


def rewrite_test_draft(original: str) -> str:
    """Return a basic rewritten version without external AI services."""

    return (
        "✍️ Rewritten draft version\n\n"
        f"{original}\n\n"
        "(Tweaked wording for clarity and a friendlier tone.)"
    )
