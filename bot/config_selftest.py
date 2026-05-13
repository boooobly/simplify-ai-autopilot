import os

from bot.config import (
    _detect_railway_with_local_db_path,
    _parse_bool_env,
    _parse_csv_env,
    _parse_custom_emoji_aliases,
    _parse_custom_emoji_map,
    _parse_daily_post_slots,
    _parse_int_range_env,
    _validate_channel_id,
    load_settings,
    startup_diagnostics,
)


def _with_env(env: dict[str, str], fn) -> None:
    saved = dict(os.environ)
    try:
        os.environ.update(env)
        fn()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def run() -> None:
    assert _parse_daily_post_slots('10:00,14:30') == ['10:00', '14:30']
    assert _parse_daily_post_slots('foo, 25:99') == ['10:00', '14:00', '18:00', '21:00']
    assert _parse_daily_post_slots(' 10:00 , 14:00 ') == ['10:00', '14:00']
    assert _parse_custom_emoji_map("🔥|123;bad;💭|456a;🧠|789") == {"🔥": "123", "🧠": "789"}

    def _max_topic_age_parser() -> None:
        os.environ["MAX_TOPIC_AGE_DAYS"] = "21"
        assert _parse_int_range_env("MAX_TOPIC_AGE_DAYS", 14, 1, 60) == 21
        os.environ["MAX_TOPIC_AGE_DAYS"] = "0"
        assert _parse_int_range_env("MAX_TOPIC_AGE_DAYS", 14, 1, 60) == 14
        os.environ["MAX_TOPIC_AGE_DAYS"] = "61"
        assert _parse_int_range_env("MAX_TOPIC_AGE_DAYS", 14, 1, 60) == 14
        os.environ["MAX_TOPIC_AGE_DAYS"] = "bad"
        assert _parse_int_range_env("MAX_TOPIC_AGE_DAYS", 14, 1, 60) == 14
        os.environ["TOPIC_AI_ENRICH_LIMIT"] = "0"
        assert _parse_int_range_env("TOPIC_AI_ENRICH_LIMIT", 8, 0, 30) == 0
        os.environ["TOPIC_AI_ENRICH_LIMIT"] = "31"
        assert _parse_int_range_env("TOPIC_AI_ENRICH_LIMIT", 8, 0, 30) == 8

    _with_env({}, _max_topic_age_parser)

    def _new_source_env_parsers() -> None:
        for value in ["1", "true", "yes", "on", " TRUE "]:
            os.environ["ENABLE_REDDIT_SOURCES"] = value
            assert _parse_bool_env("ENABLE_REDDIT_SOURCES") is True
        for value in ["", "0", "false", "no", "off", "anything"]:
            os.environ["ENABLE_REDDIT_SOURCES"] = value
            assert _parse_bool_env("ENABLE_REDDIT_SOURCES") is False
        os.environ["X_ACCOUNTS"] = "@openai, anthropic, OpenAI, , @karpathy"
        assert _parse_csv_env("X_ACCOUNTS") == ["openai", "anthropic", "karpathy"]
        os.environ["X_MAX_POSTS_PER_ACCOUNT"] = "20"
        assert _parse_int_range_env("X_MAX_POSTS_PER_ACCOUNT", 5, 1, 20) == 20
        os.environ["X_MAX_POSTS_PER_ACCOUNT"] = "21"
        assert _parse_int_range_env("X_MAX_POSTS_PER_ACCOUNT", 5, 1, 20) == 5

    _with_env({}, _new_source_env_parsers)

    aliases = _parse_custom_emoji_aliases('claude|🤖|520;bad alias|🔥|111;chatgpt|🤖|abc;claude|🤖|521')
    assert aliases == {'claude': ('🤖', '521')}
    assert _parse_custom_emoji_aliases('chatgpt|🤖|123;deepseek|🤖|124') == {
        'chatgpt': ('🤖', '123'),
        'deepseek': ('🤖', '124'),
    }

    assert _validate_channel_id("@simplify_ai") == "@simplify_ai"
    assert _validate_channel_id("-1001234567890") == "-1001234567890"
    for invalid_channel in ["https://t.me/+abcdef", "t.me/joinchat/abcdef", "random plain text"]:
        try:
            _validate_channel_id(invalid_channel)
            raise AssertionError(f"Expected invalid CHANNEL_ID: {invalid_channel}")
        except ValueError as exc:
            assert "invite-ссылкой" in str(exc)

    def _admin_id_must_be_int() -> None:
        os.environ["BOT_TOKEN"] = "token"
        os.environ["ADMIN_ID"] = "abc"
        os.environ["CHANNEL_ID"] = "@simplify_ai"
        try:
            load_settings()
            raise AssertionError("Expected ValueError for ADMIN_ID")
        except ValueError as exc:
            assert "ADMIN_ID должен быть целым числом" in str(exc)

    _with_env({"BOT_TOKEN": "", "ADMIN_ID": "", "CHANNEL_ID": ""}, _admin_id_must_be_int)

    def _diagnostics_no_secrets() -> None:
        os.environ["BOT_TOKEN"] = "bot-secret"
        os.environ["ADMIN_ID"] = "123"
        os.environ["CHANNEL_ID"] = "@simplify_ai"
        os.environ["OPENROUTER_API_KEY"] = "or-secret"
        os.environ["OPENAI_API_KEY"] = "oa-secret"
        os.environ["MAX_TOPIC_AGE_DAYS"] = "7"
        os.environ["TOPIC_AI_ENRICH_LIMIT"] = "3"
        os.environ["TOPIC_AI_TRANSLATE_LIMIT"] = "4"
        os.environ["ENABLE_REDDIT_SOURCES"] = "yes"
        os.environ["ENABLE_X_SOURCES"] = "on"
        os.environ["X_API_BEARER_TOKEN"] = "x-secret"
        os.environ["X_ACCOUNTS"] = "openai,anthropic"
        os.environ["X_MAX_POSTS_PER_ACCOUNT"] = "3"
        settings = load_settings()
        assert settings.max_topic_age_days == 7
        assert settings.topic_ai_enrich_limit == 3
        assert settings.topic_ai_translate_limit == 4
        assert settings.enable_reddit_sources is True
        assert settings.enable_x_sources is True
        assert settings.x_api_bearer_token == "x-secret"
        assert settings.x_accounts == ["openai", "anthropic"]
        assert settings.x_max_posts_per_account == 3
        lines = startup_diagnostics(settings)
        text = "\n".join(lines)
        assert "bot-secret" not in text
        assert "or-secret" not in text
        assert "oa-secret" not in text
        assert "x-secret" not in text

    _with_env({}, _diagnostics_no_secrets)

    def _railway_warning_detected() -> None:
        os.environ["RAILWAY_ENVIRONMENT"] = "prod"
        assert _detect_railway_with_local_db_path("data/drafts.db")
        assert _detect_railway_with_local_db_path("./data/drafts.db")

    _with_env({"RAILWAY_ENVIRONMENT": ""}, _railway_warning_detected)
    print('OK: DAILY_POST_SLOTS and custom emoji parsers')


if __name__ == '__main__':
    run()
