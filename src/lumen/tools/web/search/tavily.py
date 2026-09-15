"""Tavily Search API provider (relocated from the legacy if/else closure)."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx

from lumen.tools.web.models import SearchResultItem, WebSearchResult

_API_URL = "https://api.tavily.com/search"


class TavilySearchProvider:
    name = "tavily"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def search(self, client: httpx.Client, query: str, max_results: int) -> WebSearchResult:
        response = client.post(
            _API_URL,
            json={"query": query, "max_results": max_results},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        answer = payload.get("answer")
        results_raw: list[Any] = payload.get("results", [])
        results = [
            _map_item(cast(dict[str, Any], item)) for item in results_raw if isinstance(item, dict)
        ]
        return WebSearchResult(
            query=query,
            provider=self.name,
            answer=answer.strip() if isinstance(answer, str) and answer.strip() else None,
            results=results,
        )


def _iso_date(value: Any) -> str | None:
    """Normalize a provider date to ISO 8601; Tavily emits RFC-2822."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError):
        return text


def _map_item(item: dict[str, Any]) -> SearchResultItem:
    score = item.get("score")
    return SearchResultItem(
        title=str(item.get("title") or "").strip(),
        url=str(item.get("url") or "").strip(),
        snippet=str(item.get("content") or "").strip(),
        published=_iso_date(item.get("published_date")),
        score=float(score)
        if isinstance(score, (int, float)) and not isinstance(score, bool) and score >= 0
        else None,
    )
