from bot.config import _parse_custom_emoji_aliases, _parse_custom_emoji_map, _parse_daily_post_slots


def run() -> None:
    assert _parse_daily_post_slots('10:00,14:30') == ['10:00', '14:30']
    assert _parse_daily_post_slots('foo, 25:99') == ['10:00', '14:00', '18:00', '21:00']
    assert _parse_daily_post_slots(' 10:00 , 14:00 ') == ['10:00', '14:00']
    assert _parse_custom_emoji_map("🔥|123;bad;💭|456a;🧠|789") == {"🔥": "123", "🧠": "789"}

    aliases = _parse_custom_emoji_aliases('claude|🤖|520;bad alias|🔥|111;chatgpt|🤖|abc;claude|🤖|521')
    assert aliases == {'claude': ('🤖', '521')}
    assert _parse_custom_emoji_aliases('chatgpt|🤖|123;deepseek|🤖|124') == {
        'chatgpt': ('🤖', '123'),
        'deepseek': ('🤖', '124'),
    }
    print('OK: DAILY_POST_SLOTS and custom emoji parsers')


if __name__ == '__main__':
    run()
