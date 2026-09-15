"""Optional Crawl4AI browser-rendering tier for ``web_fetch``.

Crawl4AI ships in the ``browser`` extra and is imported lazily; when it is
absent the tier is unavailable and callers degrade to the stdlib fallback
exactly as before. One process-level ``AsyncWebCrawler`` singleton is created
on first use (manual ``start()``/``close()``, upstream's recommended pattern
for long-lived applications) and closed with the resource lifecycle.

SSRF boundary: the Crawl4AI SDK has no built-in SSRF protection, so it is
enforced here — the target URL is validated before rendering, a
``before_goto`` hook re-validates every top-level navigation, and a route
interception guard re-validates every sub-request (covering in-browser
redirects to private hosts). Residual risks, accepted and documented: DNS
rebinding between validation and connect (TOCTOU), and upstream hook coverage
gaps inside the browser engine itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Protocol
from urllib.parse import urlparse

from lumen.tools.web.extract import MIN_EXTRACT_CHARS
from lumen.tools.web.http import is_public_host, validate_public_url


class BrowserRenderError(Exception):
    """A render failure that degrades to the text fallback tier.

    ``reason`` maps onto ``WebFetchResult.fallback_reason``:
    ``unavailable`` (crawl4ai missing), ``blocked`` (anti-bot/challenge or
    ``success=False``), ``thin_content`` (rendered but no usable body),
    ``extract_failed`` (unexpected renderer exception).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """One rendered page's Markdown body and its final URL."""

    url: str
    markdown: str


class BrowserCrawler(Protocol):
    """The surface of ``crawl4ai.AsyncWebCrawler`` this module uses (test seam)."""

    crawler_strategy: Any

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def arun(self, *, url: str, config: Any) -> Any: ...


# crawl4ai is an optional dependency (the ``browser`` extra); it is imported
# lazily on first use so importing this module always works.
_crawl4ai: ModuleType | None = None
_crawl4ai_checked = False

_crawler: BrowserCrawler | None = None
_crawler_lock = asyncio.Lock()


def _load_crawl4ai() -> ModuleType | None:
    global _crawl4ai, _crawl4ai_checked
    if not _crawl4ai_checked:
        _crawl4ai_checked = True
        try:
            import crawl4ai  # pyright: ignore[reportMissingImports] - optional extra
        except ImportError:
            pass
        else:
            _crawl4ai = crawl4ai
    return _crawl4ai


async def _get_crawler(crawl4ai: ModuleType) -> BrowserCrawler:
    """Return the shared started crawler, creating it on first use."""

    global _crawler
    async with _crawler_lock:
        crawler = _crawler
        if crawler is None:
            new_crawler = crawl4ai.AsyncWebCrawler(  # type: ignore[attr-defined]
                config=crawl4ai.BrowserConfig(  # type: ignore[attr-defined]
                    headless=True,
                    text_mode=True,
                    light_mode=True,
                    avoid_ads=True,
                    verbose=False,
                )
            )
            try:
                await new_crawler.start()
            except Exception:
                await new_crawler.close()
                raise
            _crawler = new_crawler
            crawler = new_crawler
        return crawler


async def close_browser() -> None:
    """Close the shared crawler; registered with the resource lifecycle."""

    global _crawler
    async with _crawler_lock:
        crawler, _crawler = _crawler, None
    if crawler is not None:
        await crawler.close()


def _route_guard(host_guard: Callable[[str], bool]) -> Callable[[Any, Any], Any]:
    """Intercept every browser sub-request and abort non-public targets.

    This is the defense-in-depth layer that covers in-browser redirects
    (including server 302s after the initial goto, which ``before_goto``
    cannot see) and subresource requests to private hosts. Non-HTTP schemes
    (``data:``, ``blob:``, ``about:``) are page-internal and pass through.
    """

    verdicts: dict[str, bool] = {}

    async def guard(route: Any, request: Any) -> None:
        url = str(request.url)
        if urlparse(url).scheme not in {"http", "https"}:
            await route.continue_()
            return
        host = urlparse(url).hostname or ""
        if host not in verdicts:
            verdicts[host] = await asyncio.to_thread(host_guard, host)
        if verdicts[host]:
            await route.continue_()
        else:
            await route.abort()

    return guard


def _install_ssrf_hooks(crawler: BrowserCrawler, host_guard: Callable[[str], bool]) -> None:
    """(Re)install navigation guards so every render uses its own host guard."""

    async def before_goto(
        page: Any, *, context: Any = None, url: str = "", config: Any = None, **_kwargs: Any
    ) -> Any:
        # Re-validate every top-level navigation (including client-side
        # redirects that go through page.goto) before it happens.
        await asyncio.to_thread(validate_public_url, url, host_guard)
        return page

    async def on_page_context_created(
        page: Any, *, context: Any = None, config: Any = None, **_kwargs: Any
    ) -> Any:
        if context is not None:
            await context.route("**/*", _route_guard(host_guard))
        return page

    crawler.crawler_strategy.set_hook("before_goto", before_goto)
    crawler.crawler_strategy.set_hook("on_page_context_created", on_page_context_created)


async def render_markdown(
    url: str,
    *,
    timeout_seconds: float,
    host_guard: Callable[[str], bool] = is_public_host,
    min_chars: int = MIN_EXTRACT_CHARS,
) -> RenderedPage:
    """Render ``url`` in the shared browser and return its Markdown body.

    Every expected failure mode raises :class:`BrowserRenderError` with a
    reason the caller maps onto the fallback tier; there are no retries here —
    the HTTP fast path already retried transient network failures upstream.
    """

    crawl4ai = _load_crawl4ai()
    if crawl4ai is None:
        raise BrowserRenderError(
            "unavailable", "crawl4ai is not installed (the 'browser' extra)"
        )
    # SSRF: validate the target before rendering; the browser tier has no
    # upstream protection of its own.
    await asyncio.to_thread(validate_public_url, url, host_guard)
    crawler = await _get_crawler(crawl4ai)
    _install_ssrf_hooks(crawler, host_guard)
    config = crawl4ai.CrawlerRunConfig(  # type: ignore[attr-defined]
        markdown_generator=crawl4ai.DefaultMarkdownGenerator(  # type: ignore[attr-defined]
            content_filter=crawl4ai.PruningContentFilter()  # type: ignore[attr-defined]
        ),
        cache_mode=crawl4ai.CacheMode.BYPASS,  # type: ignore[attr-defined]
        page_timeout=int(timeout_seconds * 1000),  # crawl4ai uses milliseconds
    )
    try:
        result = await crawler.arun(url=url, config=config)
    except BrowserRenderError:
        raise
    except Exception as error:
        raise BrowserRenderError("extract_failed", f"browser render failed: {error}") from error
    if not result.success:
        # Crawl4AI reports anti-bot challenges and thin/blocked responses as
        # success=False with an error_message; degrade, never retry endlessly.
        raise BrowserRenderError(
            "blocked", result.error_message or "browser render was blocked"
        )
    markdown = getattr(result.markdown, "fit_markdown", None) or getattr(
        result.markdown, "raw_markdown", ""
    )
    if not markdown or len(markdown.strip()) < min_chars:
        raise BrowserRenderError("thin_content", "rendered page produced no usable body")
    return RenderedPage(url=result.redirected_url or result.url or url, markdown=markdown)


__all__ = [
    "BrowserCrawler",
    "BrowserRenderError",
    "RenderedPage",
    "close_browser",
    "render_markdown",
]
