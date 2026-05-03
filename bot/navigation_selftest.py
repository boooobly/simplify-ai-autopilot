from bot.handlers import (
    NAV_DRAFTS,
    NAV_GENERATE_PLAN,
    NAV_HELP,
    NAV_PLAN_DAY,
    NAV_QUEUE,
    NAV_SETTINGS,
    NAV_SOURCES,
    NAV_STYLE,
    NAV_TOPICS,
    NAV_USAGE,
    _admin_reply_keyboard,
)
from telegram import ReplyKeyboardMarkup


def run() -> None:
    keyboard = _admin_reply_keyboard()
    assert isinstance(keyboard, ReplyKeyboardMarkup)

    labels = [
        NAV_PLAN_DAY,
        NAV_GENERATE_PLAN,
        NAV_QUEUE,
        NAV_DRAFTS,
        NAV_TOPICS,
        NAV_SOURCES,
        NAV_USAGE,
        NAV_STYLE,
        NAV_SETTINGS,
        NAV_HELP,
    ]
    assert len(labels) == len(set(labels))
    assert all(label.strip() for label in labels)
    assert NAV_PLAN_DAY == "🗓 План"
    assert NAV_GENERATE_PLAN == "🧩 Черновики из плана"
    assert keyboard.resize_keyboard is True
    assert keyboard.is_persistent is True
    assert keyboard.input_field_placeholder == "Выбери действие или пришли ссылку"

    rows = keyboard.keyboard
    keyboard_labels = [btn.text for row in rows for btn in row]
    for label in labels:
        assert label in keyboard_labels


if __name__ == "__main__":
    run()
    print("navigation_selftest: ok")
