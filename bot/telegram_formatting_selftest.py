"""Лёгкие self-test проверки для форматирования Telegram."""

import re

from bot.telegram_formatting import render_post_html, strip_quote_markers


def run() -> None:
    aliases = {
        'claude': ('🤖', '5208880957280522189'),
        'chatgpt': ('🤖', '5208880957280522190'),
        'deepseek': ('🤖', '5208880957280522191'),
        'github': ('📱', '6208880957280522191'),
        'photoshop': ('📱', '6208880957280522192'),
        'windows': ('📱', '6208880957280522193'),
        'link': ('🔗', '5271604874419647061'),
        'thought': ('💭', '5467538555158943525'),
        'bullet': ('➖', '5382261056078881010'),
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
    assert '<tg-emoji emoji-id="5208880957280522189">🤖</tg-emoji>' in out
    assert '[[EMOJI:' not in out
    explicit_chatgpt = render_post_html('[[EMOJI:chatgpt]] ChatGPT', custom_emoji_aliases=aliases, strict_custom_emoji=True)
    assert '<tg-emoji emoji-id="5208880957280522190">🤖</tg-emoji>' in explicit_chatgpt

    explicit_deepseek = render_post_html('[[EMOJI:deepseek]] DeepSeek', custom_emoji_aliases=aliases, strict_custom_emoji=True)
    assert '<tg-emoji emoji-id="5208880957280522191">🤖</tg-emoji>' in explicit_deepseek

    explicit_github = render_post_html('[[EMOJI:github]] GitHub', custom_emoji_aliases=aliases, strict_custom_emoji=True)
    assert '<tg-emoji emoji-id="6208880957280522191">📱</tg-emoji>' in explicit_github


    strict_alias = render_post_html('[[EMOJI:claude]] Claude', custom_emoji_aliases={'claude': ('🤖', '5208880957280522189')}, strict_custom_emoji=True)
    assert '<tg-emoji emoji-id="5208880957280522189">🤖</tg-emoji>' in strict_alias
    assert '[[EMOJI:claude]]' not in strict_alias

    strict_missing = render_post_html('[[EMOJI:claude]] Claude', custom_emoji_aliases={}, strict_custom_emoji=True)
    assert strict_missing == 'Claude'
    assert '🤖' not in strict_missing

    non_strict_missing = render_post_html('[[EMOJI:claude]] Claude', custom_emoji_aliases={}, strict_custom_emoji=False)
    assert non_strict_missing.startswith('🤖 Claude')

    out2 = render_post_html('[[EMOJI:github]] [[EMOJI:photoshop]] [[EMOJI:windows]]', custom_emoji_aliases=aliases)
    assert '6208880957280522191' in out2 and '6208880957280522192' in out2 and '6208880957280522193' in out2

    plain = render_post_html('plain 🤖 plain 📱', custom_emoji_aliases=aliases)
    assert '<tg-emoji emoji-id="5208880957280522189">🤖</tg-emoji>' not in plain
    assert '<tg-emoji emoji-id="6208880957280522191">📱</tg-emoji>' not in plain

    strict_ambiguous_bot = render_post_html('🤖 Claude', custom_emoji_aliases=aliases, strict_custom_emoji=True)
    assert '<tg-emoji emoji-id="5208880957280522189">🤖</tg-emoji>' not in strict_ambiguous_bot
    assert '🤖' not in strict_ambiguous_bot
    assert strict_ambiguous_bot == 'Claude'

    strict_ambiguous_phone = render_post_html('📱 GitHub', custom_emoji_aliases=aliases, strict_custom_emoji=True)
    assert '<tg-emoji emoji-id="6208880957280522191">📱</tg-emoji>' not in strict_ambiguous_phone
    assert '📱' not in strict_ambiguous_phone
    assert strict_ambiguous_phone == 'GitHub'

    strict_raw = render_post_html('🔗 Подробнее\n💭 Мысль', custom_emoji_aliases=aliases, strict_custom_emoji=True)
    assert '<tg-emoji emoji-id="5271604874419647061">🔗</tg-emoji>' in strict_raw
    assert '<tg-emoji emoji-id="5467538555158943525">💭</tg-emoji>' in strict_raw
    without_tags = re.sub(r'<tg-emoji[^>]*>.*?</tg-emoji>', '', strict_raw)
    assert '🔗' not in without_tags and '💭' not in without_tags

    strict_bullets = render_post_html('➖ пункт один\n➖ пункт два', custom_emoji_aliases=aliases, strict_custom_emoji=True)
    assert strict_bullets.count('<tg-emoji emoji-id="5382261056078881010">➖</tg-emoji>') == 2
    assert '<blockquote>' in strict_bullets
    assert '➖ пункт' not in re.sub(r'<tg-emoji[^>]*>.*?</tg-emoji>', '', strict_bullets)

    fallback = render_post_html('[[EMOJI:screen_card]] Tool [[EMOJI:link]]', custom_emoji_aliases={})
    assert '🖥 Tool 🔗' in fallback
    assert '[[EMOJI:' not in fallback
    assert '<tg-emoji' not in fallback

    fallback_without_aliases = render_post_html('[[EMOJI:telegram]] channel')
    assert fallback_without_aliases.startswith('✈️ channel')
    assert '[[EMOJI:' not in fallback_without_aliases

    unknown = render_post_html('[[EMOJI:unknown]]<b>x</b>', custom_emoji_aliases=aliases)
    assert '<b>x</b>' not in unknown
    assert '[[EMOJI:' not in unknown
    assert unknown == '&lt;b&gt;x&lt;/b&gt;'

    quote_list = render_post_html('[[QUOTE]]\n[[EMOJI:idea]] quoted [[EMOJI:nope]]\n[[/QUOTE]]\n- [[EMOJI:check]] one\n- [[EMOJI:mystery]] two')
    assert '<blockquote>' in quote_list
    assert '💡 quoted ' in quote_list
    assert '➖ ✅ one' in quote_list
    assert '[[EMOJI:' not in quote_list

    linked = render_post_html('[[LINK:[[EMOJI:link]] тут|https://example.com]]')
    assert '<a href="https://example.com">🔗 тут</a>' in linked
    assert '[[EMOJI:' not in linked
    assert '[[LINK:' not in linked

    invalid_linked = render_post_html('[[LINK:тут|javascript:alert(1)]]')
    assert '[[LINK:' not in invalid_linked
    assert '<a href=' not in invalid_linked

    preview = strip_quote_markers('[[EMOJI:claude]] Claude update', custom_emoji_aliases=aliases)
    assert preview.startswith('🤖')
    assert '[[EMOJI:' not in preview

    preview_fallback = strip_quote_markers('[[EMOJI:fire]] Hot [[EMOJI:unknown]]')
    assert preview_fallback == '🔥 Hot '
    assert '[[EMOJI:' not in preview_fallback

    preview_link_quote = strip_quote_markers('[[QUOTE]]\n[[EMOJI:idea]] [[LINK:read|https://example.com]] [[EMOJI:nope]]\n[[/QUOTE]]')
    assert preview_link_quote == '💡 read '
    assert '[[EMOJI:' not in preview_link_quote

    map_out = render_post_html('Огонь 🔥', custom_emoji_map={'🔥': '123456'})
    assert '<tg-emoji emoji-id="123456">🔥</tg-emoji>' in map_out


if __name__ == '__main__':
    run()
    print('ok')
