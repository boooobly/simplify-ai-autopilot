from bot.handlers import _build_media_preview_caption, _build_moderation_text


def run() -> None:
    aliases = {"claude": ("🤖", "5208880957280522189")}
    text = "Тест [[EMOJI:claude]]"

    moderation = _build_moderation_text(
        draft_id=1,
        content=text,
        source_url="https://example.com",
        custom_emoji_aliases=aliases,
    )
    assert "🤖" in moderation

    caption = _build_media_preview_caption(
        draft_id=2,
        content=text,
        source_url="https://example.com",
        media_type="photo",
        custom_emoji_aliases=aliases,
    )
    assert "🤖" in caption

    print("OK")


if __name__ == "__main__":
    run()
