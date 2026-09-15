"""Brave Search API provider (relocated from the legacy if/else closure)."""

from __future__ import annotations

from typing import Any, cast

import httpx

from lumen.tools.web.models import SearchResultItem, WebSearchResult

_API_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchProvider:
    name = "brave"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def search(self, client: httpx.Client, query: str, max_results: int) -> WebSearchResult:
        response = client.get(
            _API_URL,
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        web = cast(dict[str, Any], payload.get("web", {}))
        results_raw: list[Any] = web.get("results", [])
        results = [
            _map_item(cast(dict[str, Any], item)) for item in results_raw if isinstance(item, dict)
        ]
        return WebSearchResult(query=query, provider=self.name, results=results)


def _map_item(item: dict[str, Any]) -> SearchResultItem:
    # Brave emits ``page_age`` as ISO 8601; no per-result score is provided.
    page_age = item.get("page_age")
    return SearchResultItem(
        title=str(item.get("title") or "").strip(),
        url=str(item.get("url") or "").strip(),
        snippet=str(item.get("description") or "").strip(),
        published=page_age.strip() if isinstance(page_age, str) and page_age.strip() else None,
    )
