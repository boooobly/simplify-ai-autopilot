"""Лёгкие self-test проверки для форматирования Telegram."""

from bot.telegram_formatting import render_post_html, strip_quote_markers


def run() -> None:
    aliases = {
        'claude': ('🤖', '5208880957280522189'),
        'chatgpt': ('🤖', '5208880957280522190'),
        'deepseek': ('🤖', '5208880957280522191'),
        'github': ('📱', '6208880957280522191'),
        'photoshop': ('📱', '6208880957280522192'),
        'windows': ('📱', '6208880957280522193'),
    }

    case1 = 'Title\n\n➖ one\n➖ two\n\nEnd'
    out1 = render_post_html(case1)
    assert '<blockquote>' in out1
    assert '➖ one' in out1

    case2 = 'Title\n\n➖ one\n\nEnd'
    out2 = render_post_html(case2)
    assert '<blockquote>' not in out2

    case3 = 'Плохо [[LINK:клик|javascript:alert(1)]]'
    out3 = render_post_html(case3)
    assert '<a href=' not in out3

    case4 = 'Тест [тут](https://example.com)'
    out4 = render_post_html(case4)
    assert '<a href="https://example.com">тут</a>' in out4

    case5 = strip_quote_markers('Проверка [[LINK:тут|https://example.com]] и [здесь](https://example.com)')
    assert 'тут' in case5 and 'здесь' in case5 and 'https://example.com' not in case5

    out = render_post_html('[[EMOJI:claude]] [[EMOJI:chatgpt]] [[EMOJI:deepseek]]', custom_emoji_aliases=aliases)
    assert '5208880957280522189' in out and '5208880957280522190' in out and '5208880957280522191' in out

    out2 = render_post_html('[[EMOJI:github]] [[EMOJI:photoshop]] [[EMOJI:windows]]', custom_emoji_aliases=aliases)
    assert '6208880957280522191' in out2 and '6208880957280522192' in out2 and '6208880957280522193' in out2

    plain = render_post_html('plain 🤖 plain 📱', custom_emoji_aliases=aliases)
    assert '<tg-emoji emoji-id="5208880957280522189">🤖</tg-emoji>' not in plain
    assert '<tg-emoji emoji-id="6208880957280522191">📱</tg-emoji>' not in plain

    unknown = render_post_html('[[EMOJI:unknown]]<b>x</b>', custom_emoji_aliases=aliases)
    assert '<b>x</b>' not in unknown

    preview = strip_quote_markers('[[EMOJI:claude]] Claude update', custom_emoji_aliases=aliases)
    assert preview.startswith('🤖')

    map_out = render_post_html('Огонь 🔥', custom_emoji_map={'🔥': '123456'})
    assert '<tg-emoji emoji-id="123456">🔥</tg-emoji>' in map_out


if __name__ == '__main__':
    run()
    print('ok')
