"""Bounded web access tools: ``web_fetch`` and config-gated ``web_search``.

Both tools are declared ``Risk.EXTERNAL`` (the operator approves an outbound
network action) with ``EffectKind.OBSERVE`` for effect tracking. They currently
use the default exclusive concurrency policy; read risk/effect does not itself
grant parallel execution. SSRF hardening: only public http(s)
hosts are reachable, redirects are re-validated per hop, and responses are
size-capped before parsing.
"""

# Model-facing Chinese prose is kept as authored for readability.
# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin, urlparse

import httpx

from lumen.config import WebSearchConfig
from lumen.tools.spec import EffectKind, Risk, ToolSpec
from lumen.tools.workspace import Workspace

if TYPE_CHECKING:
    from lumen.work_products import TaskWorkspace

MAX_CONTENT_CHARS = 64_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_PAGE_CHARS = 20_000
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 10.0

_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_REQUEST_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/json,application/xml,text/plain;q=0.9,*/*;q=0.1"
    ),
    "Accept-Encoding": "gzip, deflate",
    "User-Agent": "Mozilla/5.0 (compatible; Lumen-Agent/1.0; web-fetch)",
}

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})


class _TextExtractor(HTMLParser):
    """Convert HTML to readable plain text without third-party dependencies."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._links: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        if tag == "a" and not self._skip_depth:
            href = next((value for name, value in attrs if name == "href" and value), None)
            if href:
                target = urljoin(self._base_url, href)
                if urlparse(target).scheme in {"http", "https"}:
                    self._links.append((target, len(self._chunks)))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "a" and self._links and not self._skip_depth:
            target, start = self._links.pop()
            label = "".join(self._chunks[start:]).strip()
            if label and target not in label:
                self._chunks.append(f" ({target})")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [line.strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


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
class _FetchResult:
    content: str
    content_type: str
    url: str
    truncated: bool


def _retry_delay(error: httpx.HTTPError, attempt: int, backoff: float) -> float:
    if isinstance(error, httpx.HTTPStatusError):
        retry_after = error.response.headers.get("retry-after", "").strip()
        try:
            return min(max(float(retry_after), 0.0), MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return min(backoff * (2**attempt), MAX_RETRY_DELAY_SECONDS)


def _retryable(error: httpx.HTTPError) -> bool:
    return isinstance(error, httpx.TransportError) or (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code in _RETRYABLE_STATUS_CODES
    )


def _retry_exhausted(tool: str, url: str, attempts: int, error: httpx.HTTPError) -> RuntimeError:
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        detail = f"upstream returned {response.status_code} {response.reason_phrase}"
    else:
        detail = f"network transport failed: {error}"
    return RuntimeError(f"{tool} failed after {attempts} attempts for {url}: {detail}")


def _tool_timeout(timeout: float, attempts: int, backoff: float) -> float:
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


def _is_text_content_type(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    } or content_type.endswith(("+json", "+xml"))


def _fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    transport: httpx.BaseTransport | None,
    host_guard: Callable[[str], bool],
    retry_attempts: int,
    retry_backoff_seconds: float,
) -> _FetchResult:
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
        headers=_REQUEST_HEADERS,
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
                        if content_type in {"text/html", "application/xhtml+xml"}:
                            extractor = _TextExtractor(str(current))
                            extractor.feed(bounded.decode("utf-8", errors="replace"))
                            return _FetchResult(
                                extractor.text(), content_type, str(current), truncated
                            )
                        if _is_text_content_type(content_type):
                            body = bounded.decode("utf-8", errors="replace")
                            return _FetchResult(body, content_type, str(current), truncated)
                        raise ValueError(
                            f"unsupported content type for web_fetch: {content_type or 'unknown'} "
                            "(only HTML, plain text, JSON, XML and YAML are supported)"
                        )
                raise ValueError(f"too many redirects (>{MAX_REDIRECTS}) starting from {url}")
            except httpx.HTTPError as error:
                if not _retryable(error):
                    raise
                if attempt + 1 >= retry_attempts:
                    raise _retry_exhausted("web_fetch", url, retry_attempts, error) from error
                time.sleep(_retry_delay(error, attempt, retry_backoff_seconds))
    raise RuntimeError("web_fetch exhausted retries without a result")


def build_web_fetch_spec(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    transport: httpx.BaseTransport | None = None,
    host_guard: Callable[[str], bool] = is_public_host,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> ToolSpec:
    """Build the ``web_fetch`` tool spec.

    ``transport`` and ``host_guard`` are injection seams for tests; production
    callers rely on the default httpx transport and the DNS-based public-host
    check.
    """

    if retry_attempts < 1 or retry_backoff_seconds < 0:
        raise ValueError("retry_attempts must be positive and retry_backoff_seconds cannot be negative")

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
            url,
            timeout=timeout,
            max_bytes=max_bytes,
            transport=transport,
            host_guard=host_guard,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
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
        description=(
            "读取网页、API、JSON 或 RSS/XML 内容时优先使用本工具；它不会写入工作区。分页读取"
            "公开 HTTP(S) URL，HTML 转为带链接的可读文本，JSON/XML/YAML 和纯文本保持原文。"
            "临时网络错误会自动重试；若 has_more=true，请用 next_start_char 继续读取。"
        ),
        risk=Risk.EXTERNAL,
        effect_kind=EffectKind.OBSERVE,
        timeout=_tool_timeout(timeout, retry_attempts, retry_backoff_seconds),
    )


def build_download_file_spec(
    root: str | Path,
    *,
    task_workspace: TaskWorkspace | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    transport: httpx.AsyncBaseTransport | None = None,
    host_guard: Callable[[str], bool] = is_public_host,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> ToolSpec:
    """Transfer source text without sending its body through model output.

    Network reads use the web permission path, independent of subprocess
    networking. Local publication uses TaskWorkspace's existing journal.
    """
    workspace = Workspace(root)
    if retry_attempts < 1 or retry_backoff_seconds < 0:
        raise ValueError("retry_attempts must be positive and retry_backoff_seconds cannot be negative")
    total_timeout = _tool_timeout(timeout, retry_attempts, retry_backoff_seconds)

    async def download_file(
        url: str, path: str, overwrite: bool = False, sha256: str | None = None,
    ) -> dict[str, Any]:
        """Download a public URL's exact UTF-8 source bytes to a workspace file.

        Use this only when the resource must be saved exactly. Use web_fetch
        to read webpages, APIs, JSON or RSS/XML without creating a file.
        For complete skill installation use install_skill instead.
        Use raw.githubusercontent.com URLs, not GitHub
        HTML views. Never transcribe large files or base64 through the model.
        Parent directories are created; existing files require overwrite=True.
        Returns only path, byte size and SHA-256, not the downloaded body.
        Optionally require an expected sha256. Binary files are rejected.
        This tool cannot write global home paths. Use list_skills to refresh
        the catalog after manually downloading a skill's complete resources.
        """
        resolved = workspace.resolve_for_mutation(path)
        expected_revision = await asyncio.to_thread(workspace.revision, resolved)
        if expected_revision != "missing" and not overwrite:
            raise FileExistsError(f"file already exists; pass overwrite=True to replace: {path}")
        if sha256 is not None:
            if len(sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in sha256):
                raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        encoded: bytes | None = None
        async with asyncio.timeout(total_timeout):
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=transport,
                headers=_REQUEST_HEADERS,
            ) as client:
                for attempt in range(retry_attempts):
                    current = await asyncio.to_thread(validate_public_url, url, host_guard)
                    body = bytearray()
                    try:
                        for _ in range(MAX_REDIRECTS + 1):
                            async with client.stream("GET", current) as response:
                                if response.is_redirect:
                                    location = response.headers.get("location")
                                    if not location:
                                        raise ValueError(
                                            f"redirect without location header from {current}"
                                        )
                                    current = await asyncio.to_thread(
                                        validate_public_url,
                                        str(current.join(location)),
                                        host_guard,
                                    )
                                    continue
                                response.raise_for_status()
                                content_type = (
                                    response.headers.get("content-type", "")
                                    .split(";", 1)[0]
                                    .lower()
                                )
                                if content_type in {"text/html", "application/xhtml+xml"}:
                                    raise ValueError(
                                        "download_file requires a raw source URL, not an HTML page"
                                    )
                                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                                    if len(body) + len(chunk) > max_bytes:
                                        raise ValueError(
                                            f"download exceeds {max_bytes} bytes; no file was written"
                                        )
                                    body.extend(chunk)
                                encoded = bytes(body)
                                break
                        else:
                            raise ValueError(
                                f"too many redirects (>{MAX_REDIRECTS}) starting from {url}"
                            )
                        break
                    except httpx.HTTPError as error:
                        if not _retryable(error):
                            raise
                        if attempt + 1 >= retry_attempts:
                            raise _retry_exhausted(
                                "download_file", url, retry_attempts, error
                            ) from error
                        await asyncio.sleep(
                            _retry_delay(error, attempt, retry_backoff_seconds)
                        )
        if encoded is None:
            raise RuntimeError("download_file exhausted retries without a result")
        digest = hashlib.sha256(encoded).hexdigest()
        if sha256 is not None and digest != sha256.lower():
            raise ValueError("download SHA-256 mismatch; no file was written")
        try:
            content = encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("download_file supports UTF-8 text only; no file was written") from error
        if "\x00" in content:
            raise ValueError("download_file does not support binary files; no file was written")

        def publish() -> dict[str, Any]:
            result = {"path": path, "bytes": len(encoded), "sha256": digest}

            def write() -> dict[str, Any]:
                workspace.atomic_write(resolved, encoded, expected_revision=expected_revision)
                return result

            if task_workspace is None:
                return write()
            return task_workspace.perform_text_mutation(
                path, operation="download_file", selector="whole", change=content, action=write,
            )

        # Finish the atomic publication/journal before propagating cancellation;
        # no worker may mutate the workspace after the invocation has returned.
        publication = asyncio.create_task(asyncio.to_thread(publish))
        try:
            return await asyncio.shield(publication)
        except asyncio.CancelledError:
            await publication
            raise

    return ToolSpec(
        download_file,
        description=(
            "仅当用户需要把已知原始 URL 的完整 UTF-8 文件保存到工作区时使用。不要用它阅读"
            "网页、API、JSON 或 RSS/XML；这些任务应使用 web_fetch。HTML 页面和二进制文件会被"
            "拒绝；临时网络错误会自动重试。已有文件只有 overwrite=true 时才覆盖，可用 sha256 "
            "校验内容。结果只返回路径、字节数和 SHA-256。"
        ),
        risk=Risk.EXTERNAL,
        effect_kind=EffectKind.MUTATION,
        timeout=total_timeout,
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
        description="搜索互联网，并按“标题 — URL — 摘要”返回最相关的结果。",
        risk=Risk.EXTERNAL,
        effect_kind=EffectKind.OBSERVE,
        timeout=timeout,
    )


__all__ = ["build_download_file_spec", "build_web_fetch_spec", "build_web_search_spec"]
