"""Bounded HTTP text downloads with basic SSRF protection."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
import socket
from typing import Callable
from urllib.parse import urljoin, urlparse


DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    pass


@dataclass(frozen=True)
class PublicTextResponse:
    text: str
    headers: dict[str, str]
    url: str


def _is_public_ip(raw_ip: str) -> bool:
    address = ipaddress.ip_address(raw_ip)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_public_http_url(url: str) -> str:
    """Validate scheme, credentials, host and all currently resolved addresses."""
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("URL должен начинаться с http/https и содержать домен")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URL со встроенными логином или паролем запрещён")

    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrlError("Локальные адреса запрещены")
    try:
        addresses = [str(ipaddress.ip_address(hostname))]
    except ValueError:
        try:
            addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(hostname, parsed.port or 443)})
        except OSError as exc:
            raise UnsafeUrlError(f"Не удалось определить адрес домена: {exc}") from exc
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeUrlError("Локальные и служебные IP-адреса запрещены")
    return value


def _response_text_bounded(response, max_bytes: int) -> str:
    content_length = str((getattr(response, "headers", {}) or {}).get("content-length", "") or "")
    if content_length.isdigit() and int(content_length) > max_bytes:
        raise ValueError(f"Ответ слишком большой: больше {max_bytes} байт")

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        chunks: list[bytes] = []
        size = 0
        for chunk in iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"Ответ слишком большой: больше {max_bytes} байт")
            chunks.append(chunk)
        content = b"".join(chunks)
        xml_encoding = re.search(br"<\?xml[^>]+encoding=[\"']([A-Za-z0-9._-]+)[\"']", content[:300], re.IGNORECASE)
        encoding = (
            xml_encoding.group(1).decode("ascii", errors="ignore")
            if xml_encoding
            else (getattr(response, "encoding", None) or "utf-8")
        )
        return content.decode(encoding, errors="replace")

    text = str(getattr(response, "text", "") or "")
    if len(text.encode("utf-8", errors="replace")) > max_bytes:
        raise ValueError(f"Ответ слишком большой: больше {max_bytes} байт")
    return text


def get_public_text(
    url: str,
    *,
    request_get: Callable,
    timeout: int = 12,
    headers: dict[str, str] | None = None,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> PublicTextResponse:
    """Download public HTTP text, validating every redirect and limiting size."""
    current_url = validate_public_http_url(url)
    for _redirect in range(MAX_REDIRECTS + 1):
        response = request_get(
            current_url,
            timeout=timeout,
            headers=headers or {},
            allow_redirects=False,
            stream=True,
        )
        status_code = int(getattr(response, "status_code", 200) or 200)
        response_headers = dict(getattr(response, "headers", {}) or {})
        location = response_headers.get("location") or response_headers.get("Location")
        if 300 <= status_code < 400 and location:
            current_url = validate_public_http_url(urljoin(current_url, location))
            continue
        response.raise_for_status()
        final_url = str(getattr(response, "url", current_url) or current_url)
        validate_public_http_url(final_url)
        return PublicTextResponse(
            text=_response_text_bounded(response, max_bytes=max_bytes),
            headers=response_headers,
            url=final_url,
        )
    raise ValueError(f"Слишком много перенаправлений: больше {MAX_REDIRECTS}")
