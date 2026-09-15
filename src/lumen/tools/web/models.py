"""Structured output models for the built-in web tools.

``extra="ignore"`` is a deliberate deviation from the repo's ``extra="forbid"``
norm for persisted contracts: these models are forward-compatible projections
of provider wire responses, not authoritative records. A provider adding
fields must never break validation of outputs already journaled in a Session.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Which tier of the layered fetch chain produced the content.
FetchStrategy = Literal["fast", "browser", "text_fallback"]


class SearchResultItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    url: str
    snippet: str = ""
    #: Normalized to ISO 8601 at the provider seam (Tavily emits RFC-2822).
    published: str | None = None
    #: SearXNG ranking scores exceed 1.0, so only a lower bound is enforced.
    score: float | None = Field(default=None, ge=0)


class WebSearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str
    #: Which provider produced the result (traceability/debugging).
    provider: str
    #: Tavily's synthesized answer; Brave and SearXNG have none.
    answer: str | None = None
    results: list[SearchResultItem]
    #: Per-request engine diagnostics (SearXNG ``unresponsive_engines``) so a
    #: suspended engine is not misread as "no results".
    engine_errors: list[str] = Field(default_factory=list)


class WebFetchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    #: Final URL after redirects.
    url: str
    content_type: str
    #: Trafilatura document metadata, populated on the fast path only.
    title: str | None = None
    author: str | None = None
    date: str | None = None
    sitename: str | None = None
    #: Markdown / plain-text page slice.
    content: str
    # -- Frozen pagination contract: field names and semantics unchanged. --
    start_char: int
    end_char: int | None
    has_more: bool
    next_start_char: int | None
    total_chars: int | None
    truncated: bool
    # -- Observability for the layered fetch chain. --
    fetch_strategy: FetchStrategy
    #: Why the fast path was abandoned: ``extract_failed`` | ``thin_content``
    #: | ``js_shell`` (also set when the browser tier answered);
    #: ``blocked`` when the browser tier was refused.
    fallback_reason: str | None = None


__all__ = ["FetchStrategy", "SearchResultItem", "WebFetchResult", "WebSearchResult"]
