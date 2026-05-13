from pathlib import Path


REQUIRED_KEYS = {
    'BOT_TOKEN',
    'ADMIN_ID',
    'CHANNEL_ID',
    'OPENROUTER_API_KEY',
    'OPENAI_API_KEY',
    'MODEL_DRAFT',
    'MODEL_TOPIC_ENRICH',
    'MODEL_POLISH',
    'OPENROUTER_SITE_URL',
    'OPENROUTER_APP_NAME',
    'SCHEDULE_TIMEZONE',
    'DB_PATH',
    'POST_MAX_CHARS',
    'POST_SOFT_CHARS',
    'DAILY_POST_SLOTS',
    'OPENROUTER_INPUT_COST_PER_1M',
    'OPENROUTER_OUTPUT_COST_PER_1M',
    'OPENAI_INPUT_COST_PER_1M',
    'OPENAI_OUTPUT_COST_PER_1M',
    'CUSTOM_EMOJI_MAP',
    'CUSTOM_EMOJI_ALIASES',
    'CUSTOM_TOPIC_FEEDS',
    'ENABLE_REDDIT_SOURCES',
    'ENABLE_X_SOURCES',
    'X_API_BEARER_TOKEN',
    'X_ACCOUNTS',
    'X_MAX_POSTS_PER_ACCOUNT',
    'MAX_TOPIC_AGE_DAYS',
    'TOPIC_AI_ENRICH_LIMIT',
    'TOPIC_AI_TRANSLATE_LIMIT',
}


REQUIRED_HINTS = [
    'CHANNEL_ID must be @channel_username or numeric Telegram chat/channel id.',
    'Do NOT use invite links like https://t.me/+...',
    'CUSTOM_EMOJI_MAP format: fallback_emoji|custom_emoji_id;...',
    'CUSTOM_EMOJI_ALIASES format: alias|fallback_emoji|custom_emoji_id;...',
    'CUSTOM_TOPIC_FEEDS format: name|group|url,name|group|url',
    'Reddit community RSS feeds are disabled by default because Reddit often blocks server-side reads.',
    'Optional X source. Uses the official X API only; no x.com HTML scraping.',
    'X API may incur usage-based costs. Configure usernames manually without @.',
    'MAX_TOPIC_AGE_DAYS controls freshness filter for RSS topics (1..60 days).',
    'AI enrichment limits for topic metadata during /collect (0..30). Lower values make collection faster.',
    'Topic title translation/enrichment/scoring metadata model',
    'Daily schedule slots, format: HH:MM,HH:MM,HH:MM',
]


def run() -> None:
    content = Path('.env.example').read_text(encoding='utf-8')

    defined_keys = {
        line.split('=', 1)[0].strip()
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith('#') and '=' in line
    }

    missing = sorted(REQUIRED_KEYS - defined_keys)
    assert not missing, f'Missing env keys in .env.example: {missing}'

    for hint in REQUIRED_HINTS:
        assert hint in content, f'Missing documentation hint in .env.example: {hint}'

    print('env_example_selftest: ok')


if __name__ == '__main__':
    run()
