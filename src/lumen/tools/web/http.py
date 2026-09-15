"""HTTP transport for the web tools: SSRF guards, retries, bounded streaming.

Only public http(s) hosts are reachable: DNS results are checked per request
and redirects are re-validated per hop. Response bodies are read with a byte
ceiling so a hostile or runaway endpoint cannot exhaust memory.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 10.0

_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
REQUEST_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/json,application/xml,text/plain;q=0.9,*/*;q=0.1"
    ),
    "Accept-Encoding": "gzip, deflate",
    "User-Agent": "Mozilla/5.0 (compatible; Lumen-Agent/1.0; web-fetch)",
}


def is_public_host(host: str) -> bool:
    """Reject loopback, link-local, private and reserved targets.

    Resolution-based checking is required because a DNS name is just an
    indirection layer over the IP that will actually be connected to.
    """

    try:
        addresses = {str(info[4][0]) for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, OSError):
        return False
    if not addresses:
        return False
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return False
        if not parsed.is_global:
            return False
    return True


def validate_public_url(raw: str, host_guard: Callable[[str], bool]) -> httpx.URL:
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"web tools only support http/https URLs: {raw}")
    host = parsed.hostname
    if not host or not host_guard(host):
        raise ValueError(f"refusing to fetch a non-public host: {raw}")
    return httpx.URL(raw)


@dataclass(frozen=True, slots=True)
class RawFetch:
    """One bounded raw response body plus its final URL and content type."""

    body: bytes
    content_type: str
    url: str
    truncated: bool


def retry_delay(error: httpx.HTTPError, attempt: int, backoff: float) -> float:
    if isinstance(error, httpx.HTTPStatusError):
        retry_after = error.response.headers.get("retry-after", "").strip()
        try:
            return min(max(float(retry_after), 0.0), MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return min(backoff * (2**attempt), MAX_RETRY_DELAY_SECONDS)


def retryable(error: httpx.HTTPError) -> bool:
    return isinstance(error, httpx.TransportError) or (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code in _RETRYABLE_STATUS_CODES
    )


def retry_exhausted(tool: str, url: str, attempts: int, error: httpx.HTTPError) -> RuntimeError:
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        detail = f"upstream returned {response.status_code} {response.reason_phrase}"
    else:
        detail = f"network transport failed: {error}"
    return RuntimeError(f"{tool} failed after {attempts} attempts for {url}: {detail}")


def tool_timeout(timeout: float, attempts: int, backoff: float) -> float:
    return timeout * attempts + sum(
        min(backoff * (2**attempt), MAX_RETRY_DELAY_SECONDS) for attempt in range(attempts - 1)
    )


def _bounded_response(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    body = bytearray()
    truncated = False
    for chunk in response.iter_bytes(chunk_size=64 * 1024):
        available = max_bytes - len(body)
        if len(chunk) > available:
            body.extend(chunk[:available])
            truncated = True
            break
        body.extend(chunk)
    return bytes(body), truncated


def fetch_raw(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    transport: httpx.BaseTransport | None,
    host_guard: Callable[[str], bool],
    retry_attempts: int,
    retry_backoff_seconds: float,
) -> RawFetch:
    """GET ``url`` with per-hop SSRF validation, retries and a byte ceiling."""

    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
        headers=REQUEST_HEADERS,
    ) as client:
        for attempt in range(retry_attempts):
            current = validate_public_url(url, host_guard)
            try:
                for _ in range(MAX_REDIRECTS + 1):
                    with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError(f"redirect without location header from {current}")
                            # A redirect is a fresh SSRF attack surface.
                            current = validate_public_url(str(current.join(location)), host_guard)
                            continue
                        response.raise_for_status()
                        bounded, truncated = _bounded_response(response, max_bytes)
                        content_type = (
                            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        )
                        return RawFetch(bounded, content_type, str(current), truncated)
                raise ValueError(f"too many redirects (>{MAX_REDIRECTS}) starting from {url}")
            except httpx.HTTPError as error:
                if not retryable(error):
                    raise
                if attempt + 1 >= retry_attempts:
                    raise retry_exhausted("web_fetch", url, retry_attempts, error) from error
                time.sleep(retry_delay(error, attempt, retry_backoff_seconds))
    raise RuntimeError("web_fetch exhausted retries without a result")
