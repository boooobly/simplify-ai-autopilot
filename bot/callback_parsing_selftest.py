from bot.handlers import _parse_callback_data


def run() -> None:
    assert _parse_callback_data("schedule_slot:23:18:00") == ("schedule_slot", 23, "18:00")
    assert _parse_callback_data("schedule:23") == ("schedule", 23, None)


if __name__ == "__main__":
    run()
