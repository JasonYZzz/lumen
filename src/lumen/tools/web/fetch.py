"""``web_fetch``: bounded paginated reads of public http(s) URLs."""

# Model-facing Chinese prose is kept as authored for readability.
# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from lumen.tools.spec import EffectKind, Risk, ToolOutputSpec, ToolSpec
from lumen.tools.web.browser import BrowserRenderError, render_markdown
from lumen.tools.web.extract import extract_page_text
from lumen.tools.web.http import (
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    fetch_raw,
    is_public_host,
    tool_timeout,
)
from lumen.tools.web.models import FetchStrategy, WebFetchResult

DEFAULT_PAGE_CHARS = 20_000

_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def _is_text_content_type(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    } or content_type.endswith(("+json", "+xml"))


def build_web_fetch_spec(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    transport: httpx.BaseTransport | None = None,
    host_guard: Callable[[str], bool] = is_public_host,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    browser_enabled: bool = True,
) -> ToolSpec:
    """Build the ``web_fetch`` tool spec.

    ``transport`` and ``host_guard`` are injection seams for tests; production
    callers rely on the default httpx transport and the DNS-based public-host
    check. ``browser_enabled`` controls the optional Crawl4AI rendering tier
    (config ``tools.web.fetch_strategy``); when the ``browser`` extra is not
    installed the tier degrades silently to the text fallback.
    """

    if retry_attempts < 1 or retry_backoff_seconds < 0:
        raise ValueError("retry_attempts must be positive and retry_backoff_seconds cannot be negative")

    async def web_fetch(
        url: str, start_char: int = 1, max_chars: int = DEFAULT_PAGE_CHARS
    ) -> WebFetchResult:
        """Fetch one bounded page of a public http(s) URL as a structured result.

        HTML pages go through the extraction chain (Trafilatura Markdown fast
        path, optional browser rendering for JS pages, stdlib plain-text
        fallback); JSON/XML/YAML and plain text are returned verbatim. Results
        carry ``content``, ``content_type``, ``url`` (after redirects),
        document metadata (``title``/``author``/``date``/``sitename`` on the
        fast path), the pagination fields ``start_char``, ``end_char``,
        ``total_chars`` (when fully consumed), ``has_more``,
        ``next_start_char`` and ``truncated``, plus
        ``fetch_strategy``/``fallback_reason`` describing which extraction
        tier produced the content. Continue a partial result by calling
        ``web_fetch`` again with ``start_char`` set to ``next_start_char``.
        """
        if start_char < 1 or max_chars < 1:
            raise ValueError("start_char and max_chars must be positive")
        # The sync httpx fast path stays the single HTTP implementation; it
        # runs in a thread so the event loop is never blocked (same pattern
        # as download_file's blocking calls).
        fetched = await asyncio.to_thread(
            fetch_raw,
            url,
            timeout=timeout,
            max_bytes=max_bytes,
            transport=transport,
            host_guard=host_guard,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        title = author = date = sitename = None
        fetch_strategy: FetchStrategy = "fast"
        fallback_reason: str | None = None
        final_url = fetched.url
        if fetched.content_type in _HTML_CONTENT_TYPES:
            page_data = await asyncio.to_thread(
                extract_page_text,
                fetched.body.decode("utf-8", errors="replace"),
                url=fetched.url,
            )
            text = page_data.text
            fetch_strategy = page_data.fetch_strategy
            fallback_reason = page_data.fallback_reason
            title = page_data.title
            author = page_data.author
            date = page_data.date
            sitename = page_data.sitename
            if fetch_strategy == "text_fallback" and browser_enabled:
                try:
                    rendered = await render_markdown(
                        fetched.url, timeout_seconds=timeout, host_guard=host_guard
                    )
                except BrowserRenderError as error:
                    # An unavailable tier keeps the fast path's reason; a
                    # tier that answered with a failure reports its own.
                    if error.reason != "unavailable":
                        fallback_reason = error.reason
                else:
                    text = rendered.markdown
                    fetch_strategy = "browser"
                    final_url = rendered.url
        elif _is_text_content_type(fetched.content_type):
            text = fetched.body.decode("utf-8", errors="replace")
        else:
            raise ValueError(
                f"unsupported content type for web_fetch: {fetched.content_type or 'unknown'} "
                "(only HTML, plain text, JSON, XML and YAML are supported)"
            )
        page = text[start_char - 1 : start_char - 1 + max_chars]
        has_more = start_char - 1 + max_chars < len(text)
        end_char = start_char + len(page) - 1 if page else None
        return WebFetchResult(
            url=final_url,
            content_type=fetched.content_type,
            title=title,
            author=author,
            date=date,
            sitename=sitename,
            content=page,
            start_char=start_char,
            end_char=end_char,
            has_more=has_more,
            next_start_char=start_char + max_chars if has_more else None,
            total_chars=None if has_more or fetched.truncated else len(text),
            truncated=fetched.truncated,
            fetch_strategy=fetch_strategy,
            fallback_reason=fallback_reason,
        )

    return ToolSpec(
        web_fetch,
        description=(
            "读取网页、API、JSON 或 RSS/XML 内容时优先使用本工具；它不会写入工作区。分页读取"
            "公开 HTTP(S) URL：HTML 经正文抽取链转为带链接的 Markdown（快速路径失败时，若已安装"
            " browser extra 且 fetch_strategy=auto 会尝试浏览器渲染 JS 页面，最终回退为纯文本），"
            "JSON/XML/YAML 和纯文本保持原文。返回结构化结果：content 分页切片、最终 url、"
            "content_type、文档元数据（title/author/date/sitename，仅快速路径）、分页字段"
            "（start_char/end_char/has_more/next_start_char/total_chars/truncated）以及"
            "fetch_strategy/fallback_reason 提取路径诊断。临时网络错误会自动重试；"
            "若 has_more=true，请用 next_start_char 继续读取。"
        ),
        risk=Risk.EXTERNAL,
        effect_kind=EffectKind.OBSERVE,
        timeout=tool_timeout(timeout, retry_attempts, retry_backoff_seconds)
        + (timeout if browser_enabled else 0.0),
        output=ToolOutputSpec(WebFetchResult),
    )
