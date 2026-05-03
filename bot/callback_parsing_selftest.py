from bot.handlers import _parse_callback_data, _status_guard_message


def run() -> None:
    assert _parse_callback_data("schedule_slot:23:18:00") == ("schedule_slot", 23, "18:00")
    assert _parse_callback_data("schedule:23") == ("schedule", 23, None)
    assert _parse_callback_data("queue_today:0") == ("queue_today", 0, None)
    assert _parse_callback_data("queue_tomorrow:0") == ("queue_tomorrow", 0, None)
    assert _parse_callback_data("unschedule:42") == ("unschedule", 42, None)
    assert _parse_callback_data("restore_draft:42") == ("restore_draft", 42, None)
    assert _parse_callback_data("reject_topic:42") == ("reject_topic", 42, None)
    assert _status_guard_message("schedule", "published") == "Опубликованный черновик уже нельзя планировать."
    assert _status_guard_message("schedule", "rejected") == "Отклонённый черновик нельзя планировать."


if __name__ == "__main__":
    run()
