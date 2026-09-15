"""SearXNG JSON search API provider.

The JSON API is disabled by default upstream (``search.formats`` in
``settings.yml`` must include ``json``, otherwise every request is a 403).
``number_of_results`` is unreliable/removed upstream and is never read here;
``len(results)`` wins. ``unresponsive_engines`` is a per-request engine
health signal and is surfaced as ``engine_errors`` diagnostics.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from lumen.tools.web.models import SearchResultItem, WebSearchResult

#: SearXNG fans out to upstream engines and waits for the slowest one
#: (default per-engine timeout is 3s), so the client waits longer than a
#: plain page fetch; independent of ``fetch_timeout_seconds``.
SEARXNG_TIMEOUT_SECONDS = 12.0


class SearxngSearchProvider:
    name = "searxng"

    def __init__(
        self,
        *,
        base_url: str,
        engines: list[str] | None,
        language: str | None,
        token: str | None,
    ) -> None:
        # SSRF exception: base_url comes from the operator's own configuration
        # (explicit config = trust), so the public-host check is skipped for
        # this endpoint only — SearXNG typically runs on localhost or a
        # private network, where is_public_host would refuse the request.
        # Every other network path keeps full per-hop SSRF validation.
        self._endpoint = f"{base_url.rstrip('/')}/search"
        self._engines = engines
        self._language = language
        self._token = token

    def search(self, client: httpx.Client, query: str, max_results: int) -> WebSearchResult:
        params: dict[str, Any] = {"q": query, "format": "json"}
        if self._engines:
            params["engines"] = ",".join(self._engines)
        if self._language:
            params["language"] = self._language
        headers = {"Accept": "application/json"}
        if self._token:
            # Optional reverse-proxy auth (Basic Auth gateways accept Bearer too).
            headers["Authorization"] = f"Bearer {self._token}"
        response = client.get(self._endpoint, params=params, headers=headers)
        if response.status_code == 403:
            raise ValueError(
                "searxng refused the request (HTTP 403): the JSON API is disabled by default. "
                "Add json to search.formats in the instance's settings.yml "
                "(for example `formats: [html, json]`) and restart SearXNG"
            )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        results_raw: list[Any] = payload.get("results", [])
        results = [
            _map_item(cast(dict[str, Any], item)) for item in results_raw if isinstance(item, dict)
        ]
        return WebSearchResult(
            query=query,
            provider=self.name,
            results=results[:max_results],
            engine_errors=_engine_errors(payload.get("unresponsive_engines")),
        )


def _engine_errors(raw: Any) -> list[str]:
    """Map ``unresponsive_engines`` entries (``[engine, reason]`` pairs)."""

    if not isinstance(raw, list):
        return []
    entries = cast(list[Any], raw)
    errors: list[str] = []
    for entry in entries:
        if isinstance(entry, (list, tuple)) and entry:
            parts = [str(part) for part in cast(list[Any], entry)]
            errors.append(": ".join(parts))
        else:
            errors.append(str(cast(Any, entry)))
    return errors


def _map_item(item: dict[str, Any]) -> SearchResultItem:
    published = item.get("publishedDate")
    score = item.get("score")
    return SearchResultItem(
        title=str(item.get("title") or "").strip(),
        url=str(item.get("url") or "").strip(),
        snippet=str(item.get("content") or "").strip(),
        published=published.strip() if isinstance(published, str) and published.strip() else None,
        score=float(score)
        if isinstance(score, (int, float)) and not isinstance(score, bool) and score >= 0
        else None,
    )
