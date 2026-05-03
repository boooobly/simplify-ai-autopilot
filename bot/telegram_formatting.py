"""Safe Telegram HTML rendering for internal quote markers."""

from __future__ import annotations

import html
import re

QUOTE_OPEN = "[[QUOTE]]"
QUOTE_CLOSE = "[[/QUOTE]]"
QUOTE_BLOCK_PATTERN = re.compile(r"\[\[QUOTE\]\](.*?)\[\[/QUOTE\]\]", re.DOTALL)


def strip_quote_markers(text: str) -> str:
    """Remove internal quote markers, preserving inner text as plain text."""

    return text.replace(QUOTE_OPEN, "").replace(QUOTE_CLOSE, "")


def render_post_html(text: str) -> str:
    """Render safe HTML for Telegram with blockquote support via internal markers."""

    rendered: list[str] = []
    last_end = 0

    for match in QUOTE_BLOCK_PATTERN.finditer(text):
        before = text[last_end:match.start()]
        if before:
            rendered.append(html.escape(before))

        inner = match.group(1).strip()
        if inner:
            rendered.append(f"<blockquote>{html.escape(inner)}</blockquote>")
        last_end = match.end()

    tail = text[last_end:]
    if tail:
        rendered.append(html.escape(tail))

    # If there are unmatched markers, strip them and keep escaped plain text.
    return strip_quote_markers("".join(rendered))
