"""Safe Telegram HTML rendering for internal quote markers."""

from __future__ import annotations

import html
import re

QUOTE_OPEN = "[[QUOTE]]"
QUOTE_CLOSE = "[[/QUOTE]]"
QUOTE_BLOCK_PATTERN = re.compile(r"\[\[QUOTE\]\](.*?)\[\[/QUOTE\]\]", re.DOTALL)


LIST_PREFIXES = ("➖", "- ", "– ", "— ", "• ", "▌ ➖", "▌ -", "▌")


def _is_list_line(line: str) -> bool:
    stripped = line.strip()
    return any(stripped.startswith(prefix) for prefix in LIST_PREFIXES)


def _normalize_list_line(line: str) -> str:
    normalized = line.strip()
    if normalized.startswith("▌"):
        normalized = normalized[1:].strip()

    if normalized.startswith("➖"):
        content = normalized[1:].strip()
        return f"➖ {content}" if content else "➖"

    for marker in ("- ", "– ", "— ", "• "):
        if normalized.startswith(marker):
            content = normalized[len(marker):].strip()
            return f"➖ {content}" if content else "➖"

    return normalized


def _auto_quote_list_blocks(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    idx = 0
    in_explicit_quote = False

    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()

        if stripped == QUOTE_OPEN:
            in_explicit_quote = True
            output.append(line)
            idx += 1
            continue

        if stripped == QUOTE_CLOSE:
            in_explicit_quote = False
            output.append(line)
            idx += 1
            continue

        if in_explicit_quote or not _is_list_line(line):
            output.append(line)
            idx += 1
            continue

        run: list[str] = []
        while idx < len(lines):
            probe = lines[idx]
            probe_stripped = probe.strip()
            if probe_stripped in (QUOTE_OPEN, QUOTE_CLOSE) or not _is_list_line(probe):
                break
            run.append(_normalize_list_line(probe))
            idx += 1

        if len(run) >= 2:
            output.append(QUOTE_OPEN)
            output.extend(run)
            output.append(QUOTE_CLOSE)
        else:
            output.extend(run)

    return "\n".join(output)


def strip_quote_markers(text: str) -> str:
    """Remove internal quote markers, preserving inner text as plain text."""

    prepared = _auto_quote_list_blocks(text)
    cleaned_lines: list[str] = []
    for line in prepared.splitlines():
        stripped = line.strip()
        if stripped in (QUOTE_OPEN, QUOTE_CLOSE):
            continue
        if _is_list_line(line):
            cleaned_lines.append(_normalize_list_line(line))
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def render_post_html(text: str) -> str:
    """Render safe HTML for Telegram with blockquote support via internal markers."""

    prepared = _auto_quote_list_blocks(text)
    rendered: list[str] = []
    last_end = 0

    for match in QUOTE_BLOCK_PATTERN.finditer(prepared):
        before = prepared[last_end:match.start()]
        if before:
            rendered.append(html.escape(before))

        inner = match.group(1).strip()
        if inner:
            rendered.append(f"<blockquote>{html.escape(inner)}</blockquote>")
        last_end = match.end()

    tail = prepared[last_end:]
    if tail:
        rendered.append(html.escape(tail))

    # If there are unmatched markers, strip them and keep escaped plain text.
    return strip_quote_markers("".join(rendered))
