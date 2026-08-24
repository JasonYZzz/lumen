"""Bounded web access tools: ``web_fetch`` and config-gated ``web_search``.

Both tools are declared ``Risk.EXTERNAL`` (the operator approves an outbound
network action) with ``EffectKind.OBSERVE`` (they are read-only and idempotent,
so they may run in the parallel-safe class). SSRF hardening: only public http(s)
hosts are reachable, redirects are re-validated per hop, and responses are
size-capped before parsing.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from lumen.config import WebSearchConfig
from lumen.tools.spec import EffectKind, Risk, ToolSpec

MAX_CONTENT_CHARS = 64_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_PAGE_CHARS = 20_000

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})


class _TextExtractor(HTMLParser):
    """Convert HTML to readable plain text without third-party dependencies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [line.strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def _is_public_host(host: str) -> bool:
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


def _validate_url(raw: str, host_guard: Callable[[str], bool]) -> httpx.URL:
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"web_fetch only supports http/https URLs: {raw}")
    host = parsed.hostname
    if not host or not host_guard(host):
        raise ValueError(f"refusing to fetch a non-public host: {raw}")
    return httpx.URL(raw)


@dataclass(frozen=True, slots=True)
class _FetchResult:
    content: str
    content_type: str
    url: str
    truncated: bool


def _fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    transport: httpx.BaseTransport | None,
    host_guard: Callable[[str], bool],
) -> _FetchResult:
    current = _validate_url(url, host_guard)
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
        for _ in range(MAX_REDIRECTS + 1):
            response = client.get(current)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError(f"redirect without location header from {current}")
                # Resolve relative redirects, then re-run the full public-host
                # check: a redirect is a fresh attack surface for SSRF.
                current = _validate_url(str(current.join(location)), host_guard)
                continue
            response.raise_for_status()
            bounded = response.content[:max_bytes] if len(response.content) > max_bytes else response.content
            truncated = len(bounded) < len(response.content)
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type in {"text/html", "application/xhtml+xml"}:
                extractor = _TextExtractor()
                extractor.feed(bounded.decode("utf-8", errors="replace"))
                return _FetchResult(extractor.text(), content_type, str(current), truncated)
            if content_type.startswith("text/") or content_type in {
                "application/json",
                "application/xml",
                "text/xml",
                "application/yaml",
                "application/x-yaml",
            }:
                body = bounded.decode("utf-8", errors="replace")
                return _FetchResult(body, content_type, str(current), truncated)
            raise ValueError(
                f"unsupported content type for web_fetch: {content_type or 'unknown'} "
                "(only HTML, plain text, JSON, XML and YAML are supported)"
            )
    raise ValueError(f"too many redirects (>{MAX_REDIRECTS}) starting from {url}")


def build_web_fetch_spec(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    transport: httpx.BaseTransport | None = None,
    host_guard: Callable[[str], bool] = _is_public_host,
) -> ToolSpec:
    """Build the ``web_fetch`` tool spec.

    ``transport`` and ``host_guard`` are injection seams for tests; production
    callers rely on the default httpx transport and the DNS-based public-host
    check.
    """

    def web_fetch(url: str, start_char: int = 1, max_chars: int = DEFAULT_PAGE_CHARS) -> dict[str, Any]:
        """Fetch one bounded page of a public http(s) URL as readable text.

        HTML pages are converted to plain text; JSON/XML/YAML and plain text
        are returned verbatim. Results carry ``content``, ``content_type``,
        ``url`` (after redirects), ``start_char``, ``end_char``,
        ``total_chars`` (when fully consumed), ``has_more``,
        ``next_start_char`` and ``truncated``. Continue a partial result by
        calling ``web_fetch`` again with ``start_char`` set to
        ``next_start_char``.
        """
        if start_char < 1 or max_chars < 1:
            raise ValueError("start_char and max_chars must be positive")
        fetched = _fetch(
            url, timeout=timeout, max_bytes=max_bytes, transport=transport, host_guard=host_guard
        )
        page = fetched.content[start_char - 1 : start_char - 1 + max_chars]
        has_more = start_char - 1 + max_chars < len(fetched.content)
        end_char = start_char + len(page) - 1 if page else None
        return {
            "url": fetched.url,
            "content_type": fetched.content_type,
            "content": page,
            "start_char": start_char,
            "end_char": end_char,
            "has_more": has_more,
            "next_start_char": start_char + max_chars if has_more else None,
            "total_chars": None if has_more or fetched.truncated else len(fetched.content),
            "truncated": fetched.truncated,
        }

    return ToolSpec(
        web_fetch,
        risk=Risk.EXTERNAL,
        effect_kind=EffectKind.OBSERVE,
        timeout=timeout,
    )


def _format_result(item: dict[str, Any], keys: tuple[str, str, str]) -> str:
    """Render one search hit as ``title — url — snippet``."""

    title, url, snippet = (str(item.get(key, "") or "").strip() for key in keys)
    return f"{title} — {url} — {snippet}"


def build_web_search_spec(
    config: WebSearchConfig,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> ToolSpec:
    """Build the ``web_search`` tool spec; requires a configured search provider."""

    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        raise ValueError(
            f"web search is configured for provider '{config.provider}' but the "
            f"environment variable {config.api_key_env} is not set"
        )

    def web_search(query: str, max_results: int = config.max_results) -> str:
        """Search the web and return the top results as ``title — url — snippet`` lines."""
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20")
        with httpx.Client(timeout=timeout, transport=transport) as client:
            if config.provider == "tavily":
                response = client.post(
                    "https://api.tavily.com/search",
                    json={"query": query, "max_results": max_results},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
                results_raw: list[Any] = payload.get("results", [])
                results: list[dict[str, Any]] = [item for item in results_raw if isinstance(item, dict)]
                rows = [_format_result(item, ("title", "url", "content")) for item in results]
            else:
                response = client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": max_results},
                    headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
                )
                response.raise_for_status()
                payload_brave: dict[str, Any] = response.json()
                web = cast(dict[str, Any], payload_brave.get("web", {}))
                results_raw: list[Any] = web.get("results", [])
                results = [item for item in results_raw if isinstance(item, dict)]
                rows = [_format_result(item, ("title", "url", "description")) for item in results]
        body = "\n".join(row for row in rows if row.strip())
        if not body:
            return json.dumps({"query": query, "results": []})
        if len(body) > MAX_CONTENT_CHARS:
            body = body[:MAX_CONTENT_CHARS] + "\n[results truncated]"
        return body

    return ToolSpec(
        web_search,
        risk=Risk.EXTERNAL,
        effect_kind=EffectKind.OBSERVE,
        timeout=timeout,
    )


__all__ = ["build_web_fetch_spec", "build_web_search_spec"]
