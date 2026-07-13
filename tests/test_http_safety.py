import pytest

from bot.http_safety import UnsafeUrlError, get_public_text, validate_public_http_url


class _Response:
    def __init__(self, text="ok", *, status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/plain"}

    def raise_for_status(self):
        return None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://[::1]/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/private",
        "http://localhost:8080/",
    ],
)
def test_private_and_metadata_urls_are_rejected(url):
    with pytest.raises(UnsafeUrlError):
        validate_public_http_url(url)


def test_redirect_to_private_network_is_rejected_before_second_request():
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        return _Response(status_code=302, headers={"Location": "http://127.0.0.1/private"})

    with pytest.raises(UnsafeUrlError):
        get_public_text("https://example.com/start", request_get=fake_get)
    assert calls == ["https://example.com/start"]


def test_oversized_text_response_is_rejected():
    def fake_get(_url, **_kwargs):
        return _Response(text="x" * 101)

    with pytest.raises(ValueError, match="слишком большой"):
        get_public_text("https://example.com/feed", request_get=fake_get, max_bytes=100)


def test_xml_declaration_controls_stream_decoding():
    payload = '<?xml version="1.0" encoding="UTF-8"?><title>AI — update</title>'.encode("utf-8")

    class _StreamResponse(_Response):
        encoding = "ISO-8859-1"

        def iter_content(self, chunk_size):
            yield payload

    result = get_public_text(
        "https://example.com/feed.xml",
        request_get=lambda *_args, **_kwargs: _StreamResponse(),
    )
    assert "AI — update" in result.text
