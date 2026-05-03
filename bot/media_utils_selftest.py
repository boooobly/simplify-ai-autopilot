from bot.media_utils import decode_media_items, encode_media_group


def run() -> None:
    single = decode_media_items("file_1", "photo")
    assert single == [{"type": "photo", "file_id": "file_1"}]

    raw = encode_media_group(
        [
            {"type": "photo", "file_id": "p1"},
            {"type": "video", "file_id": "v1"},
            {"type": "bad", "file_id": "x"},
        ]
    )
    decoded = decode_media_items(raw, "media_group")
    assert decoded == [{"type": "photo", "file_id": "p1"}, {"type": "video", "file_id": "v1"}]

    assert decode_media_items("{bad", "media_group") == []
    assert decode_media_items('[{"type":"photo","file_id":""}]', "media_group") == []
    print("ok")


if __name__ == "__main__":
    run()
