"""``web_search``: provider seam and tool factory."""

# Model-facing Chinese prose is kept as authored for readability.
# ruff: noqa: RUF001

from __future__ import annotations

import os
from typing import Protocol

import httpx

from lumen.config import WebSearchConfig
from lumen.tools.spec import EffectKind, Risk, ToolOutputSpec, ToolSpec
from lumen.tools.web.http import DEFAULT_TIMEOUT_SECONDS
from lumen.tools.web.models import SearchResultItem, WebSearchResult
from lumen.tools.web.search.brave import BraveSearchProvider
from lumen.tools.web.search.searxng import SEARXNG_TIMEOUT_SECONDS, SearxngSearchProvider
from lumen.tools.web.search.tavily import TavilySearchProvider

#: Tool-internal bound on total snippet volume returned to the model.
MAX_CONTENT_CHARS = 64_000


class SearchProvider(Protocol):
    """Internal seam: one implementation per configured search provider."""

    name: str

    def search(self, client: httpx.Client, query: str, max_results: int) -> WebSearchResult: ...


def _bound_snippet_volume(result: WebSearchResult) -> WebSearchResult:
    """Cap total snippet volume; the canonical dict is what the model sees."""

    budget = MAX_CONTENT_CHARS
    items: list[SearchResultItem] = []
    for item in result.results:
        if budget <= 0:
            break
        if len(item.snippet) > budget:
            item = item.model_copy(update={"snippet": item.snippet[:budget]})
        budget -= len(item.snippet)
        items.append(item)
    return result.model_copy(update={"results": items})


def _build_provider(config: WebSearchConfig) -> SearchProvider:
    token: str | None = None
    if config.api_key_env is not None:
        token = os.environ.get(config.api_key_env)
        if not token:
            raise ValueError(
                f"web search is configured for provider '{config.provider}' but the "
                f"environment variable {config.api_key_env} is not set"
            )
    if config.provider == "searxng":
        # The config validator guarantees base_url for searxng.
        return SearxngSearchProvider(
            base_url=config.base_url or "",
            engines=config.engines,
            language=config.language,
            token=token,
        )
    if token is None:  # the config validator requires api_key_env for tavily/brave
        raise ValueError(f"web search provider '{config.provider}' requires api_key_env")
    if config.provider == "tavily":
        return TavilySearchProvider(token)
    return BraveSearchProvider(token)


def build_web_search_spec(
    config: WebSearchConfig,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> ToolSpec:
    """Build the ``web_search`` tool spec; requires a configured search provider."""

    provider = _build_provider(config)
    # SearXNG fans out to upstream engines and waits for the slowest one, so
    # it gets a dedicated client timeout independent of the fetch timeout.
    request_timeout = SEARXNG_TIMEOUT_SECONDS if isinstance(provider, SearxngSearchProvider) else timeout

    def web_search(query: str, max_results: int = config.max_results) -> WebSearchResult:
        """Search the web and return structured results.

        Results carry the original ``query``, the ``provider`` that answered,
        an optional synthesized ``answer`` (Tavily only), ranked ``results``
        (each with ``title``, ``url``, ``snippet``, optional ISO 8601
        ``published`` date and relevance ``score``) and ``engine_errors``
        diagnostics for suspended or unresponsive search engines.
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20")
        with httpx.Client(timeout=request_timeout, transport=transport) as client:
            result = provider.search(client, query, max_results)
        return _bound_snippet_volume(result)

    return ToolSpec(
        web_search,
        description=(
            "搜索互联网，返回结构化结果：query（原始查询）、provider（搜索来源）、answer（如有，"
            "为搜索引擎给出的综合答案）、results（按相关性排序的结果列表，每项含 title/url/snippet，"
            "以及可选的 published 发布时间与 score 相关度得分）、engine_errors（引擎异常诊断；"
            "非空时表示部分引擎不可用，不等于没有结果）。"
        ),
        risk=Risk.EXTERNAL,
        effect_kind=EffectKind.OBSERVE,
        timeout=request_timeout,
        output=ToolOutputSpec(WebSearchResult),
    )
