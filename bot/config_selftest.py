from bot.config import _parse_daily_post_slots


def run() -> None:
    assert _parse_daily_post_slots('10:00,14:30') == ['10:00', '14:30']
    assert _parse_daily_post_slots('foo, 25:99') == ['10:00', '14:00', '18:00', '21:00']
    assert _parse_daily_post_slots(' 10:00 , 14:00 ') == ['10:00', '14:00']
    print('OK: DAILY_POST_SLOTS parser')


if __name__ == '__main__':
    run()
