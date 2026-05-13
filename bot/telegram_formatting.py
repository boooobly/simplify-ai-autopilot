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
EMOJI_ALIAS_PATTERN = re.compile(r"\[\[EMOJI:([a-zA-Z0-9_-]+)\]\]")
TG_EMOJI_TAG_PATTERN = re.compile(r'<tg-emoji\s+emoji-id="\d+">.*?</tg-emoji>', re.DOTALL)
OBVIOUS_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\u2600-\u27BF"
    "]\ufe0f?",
)
SAFE_EMOJI_ALIAS_FALLBACKS = {
    "screen_card": "🖥",
    "lock": "🔒",
    "web": "🌐",
    "check": "✅",
    "claude": "🤖",
    "chatgpt": "🤖",
    "deepseek": "🤖",
    "edit_tool": "✏️",
    "fire": "🔥",
    "idea": "💡",
    "link": "🔗",
    "alert": "❗",
    "bullet": "➖",
    "thought": "💭",
    "wow": "😮",
    "google": "🔎",
    "github": "🐙",
    "photoshop": "🖼",
    "windows": "🪟",
    "telegram": "✈️",
}
ALIAS_RAW_FALLBACK_VARIANTS = {
    "check": ("✔️", "✔", "✅"),
    "alert": ("❗️", "❗"),
    "edit_tool": ("✏️", "✏"),
    "github": ("📱", "🐙"),
    "photoshop": ("📱", "🖼"),
    "windows": ("📱", "🪟"),
    "telegram": ("✍️", "✈️"),
}
KNOWN_RAW_EMOJI_FALLBACKS = {
    fallback
    for fallback in SAFE_EMOJI_ALIAS_FALLBACKS.values()
    if fallback
} | {
    fallback
    for variants in ALIAS_RAW_FALLBACK_VARIANTS.values()
    for fallback in variants
}


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
        if not raw_text:
            return ""
        if not _is_safe_url(raw_url):
            return html.escape(raw_text)
        return f'<a href="{html.escape(raw_url, quote=True)}">{html.escape(raw_text)}</a>'

    rendered = LINK_MARKER_PATTERN.sub(_replace_internal, escaped)

    def _replace_md(match: re.Match[str]) -> str:
        text_value = html.unescape(match.group(1).strip())
        url_value = html.unescape(match.group(2).strip())
        if not text_value:
            return ""
        if not _is_safe_url(url_value):
            return html.escape(text_value)
        return f'<a href="{html.escape(url_value, quote=True)}">{html.escape(text_value)}</a>'

    return MARKDOWN_LINK_PATTERN.sub(_replace_md, rendered)


def _safe_emoji_alias_fallback(alias: str) -> str:
    """Return a plain emoji fallback for a known internal alias, or remove it."""

    return SAFE_EMOJI_ALIAS_FALLBACKS.get(alias, "")


def _custom_emoji_tag(fallback: str, emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{html.escape(fallback)}</tg-emoji>'


def _valid_alias_data(emoji_data: tuple[str, str] | None) -> tuple[str, str] | None:
    if not emoji_data:
        return None
    fallback, emoji_id = emoji_data
    emoji_id = str(emoji_id)
    if fallback and emoji_id.isdigit():
        return fallback, emoji_id
    return None


def _apply_custom_emoji_aliases(
    text: str,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None,
    *,
    strict_custom_emoji: bool = False,
) -> str:
    def _replace(match: re.Match[str]) -> str:
        alias = match.group(1)
        emoji_data = _valid_alias_data(custom_emoji_aliases.get(alias) if custom_emoji_aliases else None)
        if emoji_data:
            fallback, emoji_id = emoji_data
            return _custom_emoji_tag(fallback, emoji_id)
        if strict_custom_emoji:
            return ""
        return html.escape(_safe_emoji_alias_fallback(alias))

    return EMOJI_ALIAS_PATTERN.sub(_replace, text)


def _alias_fallback_variants(alias: str, configured_fallback: str | None = None) -> tuple[str, ...]:
    values: list[str] = []
    if configured_fallback:
        values.append(configured_fallback)
    values.extend(ALIAS_RAW_FALLBACK_VARIANTS.get(alias, ()))
    safe_fallback = _safe_emoji_alias_fallback(alias)
    if safe_fallback:
        values.append(safe_fallback)

    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
    return tuple(deduped)


def _strict_raw_fallback_replacements(custom_emoji_aliases: dict[str, tuple[str, str]] | None) -> dict[str, str]:
    raw_to_tags: dict[str, set[str]] = {}
    if not custom_emoji_aliases:
        return {}

    for alias, emoji_data in custom_emoji_aliases.items():
        valid_data = _valid_alias_data(emoji_data)
        if not valid_data:
            continue
        fallback, emoji_id = valid_data
        tag = _custom_emoji_tag(fallback, emoji_id)
        for raw_fallback in _alias_fallback_variants(alias, fallback):
            raw_to_tags.setdefault(html.escape(raw_fallback), set()).add(tag)

    return {raw_fallback: next(iter(tags)) for raw_fallback, tags in raw_to_tags.items() if len(tags) == 1}


def _replace_outside_tg_emoji_tags(text: str, callback) -> str:
    parts: list[str] = []
    last_end = 0
    for match in TG_EMOJI_TAG_PATTERN.finditer(text):
        if match.start() > last_end:
            parts.append(callback(text[last_end:match.start()]))
        parts.append(match.group(0))
        last_end = match.end()
    if last_end < len(text):
        parts.append(callback(text[last_end:]))
    return "".join(parts)


def _apply_strict_raw_custom_emoji(text: str, custom_emoji_aliases: dict[str, tuple[str, str]] | None) -> str:
    replacements = _strict_raw_fallback_replacements(custom_emoji_aliases)
    known_fallbacks = {html.escape(fallback) for fallback in KNOWN_RAW_EMOJI_FALLBACKS if fallback}
    known_fallbacks.update(replacements)
    ordered_known = sorted(known_fallbacks, key=len, reverse=True)
    ordered_replacements = sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)

    def _convert_segment(segment: str) -> str:
        result = segment
        placeholders: dict[str, str] = {}
        for index, (raw_fallback, tag) in enumerate(ordered_replacements):
            placeholder = f"\uE000TGEMOJI{index}\uE000"
            placeholders[placeholder] = tag
            result = result.replace(raw_fallback, placeholder)
        for raw_fallback in ordered_known:
            if raw_fallback not in replacements:
                result = result.replace(raw_fallback, "")
        result = OBVIOUS_EMOJI_PATTERN.sub("", result)
        for placeholder, tag in placeholders.items():
            result = result.replace(placeholder, tag)
        return result

    return _replace_outside_tg_emoji_tags(text, _convert_segment)


def _apply_custom_emoji(text: str, custom_emoji_map: dict[str, str] | None) -> str:
    if not custom_emoji_map:
        return text
    result = text
    for fallback, emoji_id in custom_emoji_map.items():
        if not fallback or not emoji_id.isdigit():
            continue
        safe_fallback = html.escape(fallback)
        result = result.replace(safe_fallback, _custom_emoji_tag(fallback, emoji_id))
    return result


def _strip_link_markers_for_preview(text: str) -> str:
    text = LINK_MARKER_PATTERN.sub(lambda m: m.group(1).strip(), text)
    text = MARKDOWN_LINK_PATTERN.sub(lambda m: m.group(1).strip(), text)
    return text


def _strip_emoji_aliases_for_preview(
    text: str,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None = None,
    *,
    strict_custom_emoji: bool = False,
) -> str:
    def _replace(match: re.Match[str]) -> str:
        alias = match.group(1)
        if strict_custom_emoji:
            return ""
        if custom_emoji_aliases and alias in custom_emoji_aliases:
            return custom_emoji_aliases[alias][0] or _safe_emoji_alias_fallback(alias)
        return _safe_emoji_alias_fallback(alias)

    stripped = EMOJI_ALIAS_PATTERN.sub(_replace, text)
    if strict_custom_emoji:
        stripped = _apply_strict_raw_custom_emoji(html.escape(stripped), custom_emoji_aliases)
        stripped = re.sub(r"</?tg-emoji[^>]*>", "", stripped)
        stripped = html.unescape(stripped)
    return stripped


def _strip_quote_markers_render_only(text: str) -> str:
    """Remove internal quote markers without touching emoji alias markers."""

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


def strip_quote_markers(
    text: str,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None = None,
    *,
    strict_custom_emoji: bool = False,
) -> str:
    """Remove internal quote markers, preserving inner text as plain text."""

    preview = _strip_emoji_aliases_for_preview(text, custom_emoji_aliases=custom_emoji_aliases, strict_custom_emoji=strict_custom_emoji)
    prepared = _auto_quote_list_blocks(_strip_link_markers_for_preview(preview))
    cleaned_lines: list[str] = []
    for line in prepared.splitlines():
        stripped = line.strip()
        if stripped in (QUOTE_OPEN, QUOTE_CLOSE):
            continue
        if _is_list_line(line):
            cleaned_lines.append(_normalize_list_line(line))
        else:
            cleaned_lines.append(line.lstrip() if strict_custom_emoji else line)
    return "\n".join(cleaned_lines)


def render_post_html(
    text: str,
    custom_emoji_map: dict[str, str] | None = None,
    custom_emoji_aliases: dict[str, tuple[str, str]] | None = None,
    *,
    strict_custom_emoji: bool = False,
) -> str:
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

    # If there are unmatched markers, strip only quote markers in render path.
    output = _strip_quote_markers_render_only("".join(rendered))
    if strict_custom_emoji:
        output = _apply_strict_raw_custom_emoji(output, custom_emoji_aliases)
    else:
        output = _apply_custom_emoji(output, custom_emoji_map)
    output = _apply_custom_emoji_aliases(output, custom_emoji_aliases, strict_custom_emoji=strict_custom_emoji)
    if strict_custom_emoji:
        output = "\n".join(line.lstrip() for line in output.splitlines())
    return output
