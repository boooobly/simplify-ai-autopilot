"""Safe Telegram HTML rendering for internal quote markers."""

from __future__ import annotations

import html
import re

QUOTE_OPEN = "[[QUOTE]]"
QUOTE_CLOSE = "[[/QUOTE]]"
QUOTE_BLOCK_PATTERN = re.compile(r"\[\[QUOTE\]\](.*?)\[\[/QUOTE\]\]", re.DOTALL)
LINK_MARKER_PATTERN = re.compile(r"\[\[LINK:(.+?)\|(.+?)\]\]")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
SAFE_URL_PATTERN = re.compile(r"^(https?://|tg://)", re.IGNORECASE)


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


def _is_safe_url(url: str) -> bool:
    return bool(SAFE_URL_PATTERN.match(url.strip()))


def _render_safe_links(text: str) -> str:
    escaped = html.escape(text)

    def _replace_internal(match: re.Match[str]) -> str:
        raw_text = html.unescape(match.group(1).strip())
        raw_url = html.unescape(match.group(2).strip())
        if not raw_text or not _is_safe_url(raw_url):
            return html.escape(match.group(0))
        return f'<a href="{html.escape(raw_url, quote=True)}">{html.escape(raw_text)}</a>'

    rendered = LINK_MARKER_PATTERN.sub(_replace_internal, escaped)

    def _replace_md(match: re.Match[str]) -> str:
        text_value = html.unescape(match.group(1).strip())
        url_value = html.unescape(match.group(2).strip())
        if not text_value or not _is_safe_url(url_value):
            return html.escape(match.group(0))
        return f'<a href="{html.escape(url_value, quote=True)}">{html.escape(text_value)}</a>'

    return MARKDOWN_LINK_PATTERN.sub(_replace_md, rendered)


def _apply_custom_emoji(text: str, custom_emoji_map: dict[str, str] | None) -> str:
    if not custom_emoji_map:
        return text
    result = text
    for fallback, emoji_id in custom_emoji_map.items():
        if not fallback or not emoji_id.isdigit():
            continue
        safe_fallback = html.escape(fallback)
        result = result.replace(safe_fallback, f'<tg-emoji emoji-id="{emoji_id}">{safe_fallback}</tg-emoji>')
    return result


def _strip_link_markers_for_preview(text: str) -> str:
    text = LINK_MARKER_PATTERN.sub(lambda m: m.group(1).strip(), text)
    text = MARKDOWN_LINK_PATTERN.sub(lambda m: m.group(1).strip(), text)
    return text


def strip_quote_markers(text: str) -> str:
    """Remove internal quote markers, preserving inner text as plain text."""

    prepared = _auto_quote_list_blocks(_strip_link_markers_for_preview(text))
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


def render_post_html(text: str, custom_emoji_map: dict[str, str] | None = None) -> str:
    """Render safe HTML for Telegram with blockquote support via internal markers."""

    prepared = _auto_quote_list_blocks(text)
    rendered: list[str] = []
    last_end = 0

    for match in QUOTE_BLOCK_PATTERN.finditer(prepared):
        before = prepared[last_end:match.start()]
        if before:
            rendered.append(_render_safe_links(before))

        inner = match.group(1).strip()
        if inner:
            rendered.append(f"<blockquote>{_render_safe_links(inner)}</blockquote>")
        last_end = match.end()

    tail = prepared[last_end:]
    if tail:
        rendered.append(_render_safe_links(tail))

    # If there are unmatched markers, strip them and keep escaped plain text.
    return _apply_custom_emoji(strip_quote_markers("".join(rendered)), custom_emoji_map)
