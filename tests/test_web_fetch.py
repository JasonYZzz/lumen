"""Contracts for ``web_fetch`` structured output and the extraction chain."""

from __future__ import annotations

import os
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

import lumen.tools.web.browser as web_browser
import lumen.tools.web.extract as web_extract
from lumen.tools.spec import ToolSpec
from lumen.tools.web import build_web_fetch_spec
from lumen.tools.web.browser import close_browser, render_markdown


@pytest.mark.parametrize("installed", [False, True])
def test_optional_article_extractor_is_loaded_once(
    installed: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("trafilatura")
    imports: list[str] = []

    def load(name: str) -> ModuleType:
        imports.append(name)
        if not installed:
            raise ModuleNotFoundError(name)
        return module

    monkeypatch.setattr(web_extract, "_trafilatura", None)
    monkeypatch.setattr(web_extract, "_trafilatura_checked", False)
    monkeypatch.setattr(web_extract, "import_module", load)

    for _ in range(2):
        loaded = web_extract._load_trafilatura()  # pyright: ignore[reportPrivateUsage]
        assert loaded is (module if installed else None)
    assert imports == ["trafilatura"]


def _fetch_spec(
    body: bytes | str,
    content_type: str = "text/plain",
    *,
    host_guard: Any = None,
    browser_enabled: bool = True,
    **kwargs: object,
) -> ToolSpec:
    payload = body.encode() if isinstance(body, str) else body
    return build_web_fetch_spec(
        host_guard=host_guard or (lambda _: True),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                headers={"content-type": content_type},
                content=payload,
            )
        ),
        browser_enabled=browser_enabled,
        **kwargs,  # type: ignore[arg-type]
    )


ARTICLE_HTML = """<!DOCTYPE html><html><head>
<title>Understanding Agent Loops - Lumen Blog</title>
<meta property="og:site_name" content="Lumen Blog">
<meta name="author" content="Jane Doe">
<meta property="article:published_time" content="2026-09-01T10:00:00Z">
</head><body>
<nav><a href="/">Home</a></nav>
<article>
<h1>Understanding Agent Loops</h1>
<p>An agent loop is the heartbeat of any LLM-driven system. It accepts a model
response, dispatches the requested tool calls, collects their outputs, and
feeds everything back into the next request. Without a disciplined loop,
agents stall, repeat themselves, or silently drop work that looked finished
but never actually ran to completion.</p>
<p>The loop owns retries, cancellation, and termination candidates. Tool
results flow through a gateway that validates outputs before the model ever
sees them. See <a href="https://example.com/docs/loop">the loop guide</a>
for the full contract, including pagination and structured results.</p>
</article>
<footer>Copyright 2026</footer>
</body></html>"""

JS_SHELL_HTML = (
    '<!DOCTYPE html><html><head><title>App</title></head>'
    '<body><div id="root"></div><script src="/bundle.js"></script></body></html>'
)


# --------------------------------------------------------------------------- #
# Frozen pagination contract
# --------------------------------------------------------------------------- #


async def test_web_fetch_pagination_contract_shape() -> None:
    body = "0123456789" * 10
    spec = _fetch_spec(body)

    first = await spec.function("https://example.com/data", max_chars=40)

    assert first.model_dump(mode="json") == {
        "url": "https://example.com/data",
        "content_type": "text/plain",
        "title": None,
        "author": None,
        "date": None,
        "sitename": None,
        "content": body[:40],
        "start_char": 1,
        "end_char": 40,
        "has_more": True,
        "next_start_char": 41,
        "total_chars": None,
        "truncated": False,
        "fetch_strategy": "fast",
        "fallback_reason": None,
    }


async def test_web_fetch_pagination_continuation_reaches_terminal_page() -> None:
    body = "0123456789" * 10
    spec = _fetch_spec(body)

    last = await spec.function("https://example.com/data", start_char=41, max_chars=1000)

    assert last.model_dump(mode="json") == {
        "url": "https://example.com/data",
        "content_type": "text/plain",
        "title": None,
        "author": None,
        "date": None,
        "sitename": None,
        "content": body[40:],
        "start_char": 41,
        "end_char": 100,
        "has_more": False,
        "next_start_char": None,
        "total_chars": 100,
        "truncated": False,
        "fetch_strategy": "fast",
        "fallback_reason": None,
    }


async def test_web_fetch_pagination_rejects_non_positive_bounds() -> None:
    spec = _fetch_spec("body")

    with pytest.raises(ValueError, match="must be positive"):
        await spec.function("https://example.com/data", start_char=0)
    with pytest.raises(ValueError, match="must be positive"):
        await spec.function("https://example.com/data", max_chars=0)


async def test_fetch_output_contract_produces_canonical_dict() -> None:
    spec = _fetch_spec("hello world")

    canonical = spec.output_contract.validate(await spec.function("https://example.com/data"))

    assert isinstance(canonical, dict)
    assert canonical["content"] == "hello world"
    assert canonical["fetch_strategy"] == "fast"
    with pytest.raises(ValidationError):
        spec.output_contract.validate({"url": "https://x.example", "content": 1})


# --------------------------------------------------------------------------- #
# Trafilatura fast path and fallbacks
# --------------------------------------------------------------------------- #


async def test_article_html_uses_trafilatura_fast_path() -> None:
    pytest.importorskip("trafilatura")
    spec = _fetch_spec(ARTICLE_HTML, "text/html; charset=utf-8")

    result = await spec.function("https://blog.example.com/post")

    assert result.fetch_strategy == "fast"
    assert result.fallback_reason is None
    assert "# Understanding Agent Loops" in result.content
    assert "[the loop guide](https://example.com/docs/loop)" in result.content
    assert result.title == "Understanding Agent Loops"
    assert result.author == "Jane Doe"
    assert result.date == "2026-09-01"
    assert result.sitename == "Lumen Blog"


async def test_js_shell_falls_back_to_text_extractor_when_browser_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _simulate_crawl4ai_absent(monkeypatch)
    spec = _fetch_spec(JS_SHELL_HTML, "text/html")

    result = await spec.function("https://app.example.com/")

    assert result.fetch_strategy == "text_fallback"
    assert result.fallback_reason == "js_shell"
    # The stdlib extractor keeps the <title> text (it only skips script/style).
    assert result.content == "App"
    assert result.title is None


async def test_thin_extraction_falls_back_with_reason() -> None:
    pytest.importorskip("trafilatura")
    thin_html = (
        "<html><body><article><p>The committee published its interim report on Tuesday, "
        "outlining three options for the regional water treaty and inviting public comment "
        "through the end of the month.</p></article></body></html>"
    )
    spec = _fetch_spec(thin_html, "text/html")

    result = await spec.function("https://news.example.com/a")

    # Trafilatura extracted a body, but below MIN_EXTRACT_CHARS it is treated
    # as a thin page and the stdlib extractor takes over.
    assert result.fetch_strategy == "text_fallback"
    assert result.fallback_reason == "thin_content"
    assert "interim report" in result.content


async def test_missing_trafilatura_falls_back_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_extract, "_trafilatura", None)
    monkeypatch.setattr(web_extract, "_trafilatura_checked", True)
    _simulate_crawl4ai_absent(monkeypatch)
    spec = _fetch_spec(ARTICLE_HTML, "text/html")

    result = await spec.function("https://blog.example.com/post")

    assert result.fetch_strategy == "text_fallback"
    assert result.fallback_reason == "extract_failed"
    # The stdlib extractor still produces the article text with its link.
    assert "Understanding Agent Loops" in result.content
    assert "(https://example.com/docs/loop)" in result.content
    assert result.title is None


# --------------------------------------------------------------------------- #
# Browser tier (Crawl4AI seam faked; CI never launches a real browser)
# --------------------------------------------------------------------------- #


def _simulate_crawl4ai_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_browser, "_crawl4ai", None)
    monkeypatch.setattr(web_browser, "_crawl4ai_checked", True)
    monkeypatch.setattr(web_browser, "_crawler", None)


class _FakeStrategy:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}

    def set_hook(self, hook_type: str, hook: Any) -> None:
        self.hooks[hook_type] = hook


class _FakeCrawler:
    def __init__(self, result: Any) -> None:
        self.crawler_strategy = _FakeStrategy()
        self._result = result
        self.calls: list[str] = []
        self.closed = False

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def arun(self, *, url: str, config: Any) -> Any:
        self.calls.append(url)
        return self._result


def _render_result(
    *,
    success: bool = True,
    fit: str | None = "# Rendered\n\n" + "Rendered body text. " * 30,
    raw: str = "raw markdown",
    error: str | None = None,
    redirected: str | None = None,
    url: str = "https://app.example.com/",
) -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        error_message=error,
        redirected_url=redirected,
        url=url,
        markdown=SimpleNamespace(fit_markdown=fit, raw_markdown=raw),
    )


def _namespace(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _example_com_only(host: str) -> bool:
    return host == "example.com"


def _install_fake_browser(monkeypatch: pytest.MonkeyPatch, result: Any) -> _FakeCrawler:
    fake_module = SimpleNamespace(
        CrawlerRunConfig=_namespace,
        DefaultMarkdownGenerator=_namespace,
        PruningContentFilter=lambda: object(),
        CacheMode=SimpleNamespace(BYPASS="bypass"),
    )
    fake = _FakeCrawler(result)
    monkeypatch.setattr(web_browser, "_crawl4ai", fake_module)
    monkeypatch.setattr(web_browser, "_crawl4ai_checked", True)
    monkeypatch.setattr(web_browser, "_crawler", fake)
    return fake


async def test_browser_tier_renders_js_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch, _render_result())
    spec = _fetch_spec(JS_SHELL_HTML, "text/html")

    result = await spec.function("https://app.example.com/")

    assert fake.calls == ["https://app.example.com/"]
    assert result.fetch_strategy == "browser"
    # The reason the fast path was abandoned stays observable.
    assert result.fallback_reason == "js_shell"
    assert result.content.startswith("# Rendered")
    # Pagination runs over the rendered markdown like any other content.
    assert result.has_more is False
    assert result.total_chars == len(result.content)


async def test_browser_success_false_degrades_with_blocked_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_browser(
        monkeypatch, _render_result(success=False, error="Cloudflare challenge page detected")
    )
    spec = _fetch_spec(JS_SHELL_HTML, "text/html")

    result = await spec.function("https://app.example.com/")

    assert result.fetch_strategy == "text_fallback"
    assert result.fallback_reason == "blocked"
    assert result.content == "App"


async def test_browser_thin_render_degrades_with_thin_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_browser(monkeypatch, _render_result(fit="tiny", raw="tiny"))
    spec = _fetch_spec(JS_SHELL_HTML, "text/html")

    result = await spec.function("https://app.example.com/")

    assert result.fetch_strategy == "text_fallback"
    assert result.fallback_reason == "thin_content"


async def test_http_only_strategy_never_touches_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch, _render_result())
    spec = _fetch_spec(JS_SHELL_HTML, "text/html", browser_enabled=False)

    result = await spec.function("https://app.example.com/")

    assert fake.calls == []
    assert result.fetch_strategy == "text_fallback"
    assert result.fallback_reason == "js_shell"


async def test_before_goto_hook_revalidates_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch, _render_result())
    spec = _fetch_spec(JS_SHELL_HTML, "text/html", host_guard=_example_com_only)
    await spec.function("https://example.com/app")

    before_goto = fake.crawler_strategy.hooks["before_goto"]
    page = object()
    # A navigation to a public host passes and returns the page.
    assert await before_goto(page, url="https://example.com/ok") is page
    # An in-browser redirect to a private host is refused before it happens.
    with pytest.raises(ValueError, match="non-public host"):
        await before_goto(page, url="http://127.0.0.1/admin")
    with pytest.raises(ValueError, match="non-public host"):
        await before_goto(page, url="http://169.254.169.254/latest/meta-data")


async def test_route_guard_aborts_private_subrequests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_browser(monkeypatch, _render_result())
    spec = _fetch_spec(JS_SHELL_HTML, "text/html", host_guard=_example_com_only)
    await spec.function("https://example.com/app")

    class _FakeContext:
        def __init__(self) -> None:
            self.routes: dict[str, Any] = {}

        async def route(self, pattern: str, handler: Any) -> None:
            self.routes[pattern] = handler

    context = _FakeContext()
    on_page_context_created = fake.crawler_strategy.hooks["on_page_context_created"]
    await on_page_context_created(object(), context=context)
    guard = context.routes["**/*"]
    outcomes: list[str] = []

    class _Route:
        async def continue_(self) -> None:
            outcomes.append("continue")

        async def abort(self) -> None:
            outcomes.append("abort")

    await guard(_Route(), SimpleNamespace(url="https://example.com/app.js"))
    await guard(_Route(), SimpleNamespace(url="http://10.0.0.1/internal"))
    await guard(_Route(), SimpleNamespace(url="data:text/html,<p>x</p>"))

    assert outcomes == ["continue", "abort", "continue"]


async def test_close_browser_closes_singleton_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch, _render_result())

    await close_browser()

    assert fake.closed is True
    # Idempotent: closing again without a live singleton is a no-op.
    await close_browser()


@pytest.mark.skipif(
    os.environ.get("LUMEN_LIVE_BROWSER") != "1",
    reason="live browser render is opt-in (LUMEN_LIVE_BROWSER=1, requires crawl4ai-setup)",
)
async def test_browser_tier_renders_a_real_page() -> None:
    pytest.importorskip("crawl4ai")
    try:
        rendered = await render_markdown("https://example.com", timeout_seconds=30.0)
        assert "Example Domain" in rendered.markdown
    finally:
        await close_browser()
