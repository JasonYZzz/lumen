"""Contracts for ``web_search`` providers and structured output.

The request wire format (params, headers, body) is byte-identical to the
pre-refactor closures; results are now mapped to ``WebSearchResult``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from lumen.config import PermissionsConfig, WebSearchConfig
from lumen.events import ApprovalRequest
from lumen.tools.gateway import (
    CapabilityApproval,
    CapabilityGateway,
    CapabilityInvocation,
    CapabilityStatus,
)
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import ToolSpec
from lumen.tools.web import MAX_CONTENT_CHARS, build_web_search_spec
from lumen.tools.web.models import WebSearchResult


def _tavily_spec(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> ToolSpec:
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-token")
    return build_web_search_spec(
        WebSearchConfig(provider="tavily", api_key_env="TAVILY_API_KEY"),
        transport=transport,
    )


def test_tavily_search_request_and_result_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "answer": "Tavily synthesized answer",
                "results": [
                    {
                        "title": "One",
                        "url": "https://one.example",
                        "content": "first snippet",
                        "score": 0.91,
                        "published_date": "Tue, 01 Sep 2026 10:00:00 GMT",
                    },
                    {"title": "Two", "url": "https://two.example", "content": "second snippet"},
                ],
            },
        )

    spec = _tavily_spec(monkeypatch, httpx.MockTransport(respond))
    result = spec.function("lumen agent")

    assert isinstance(result, WebSearchResult)
    assert result.query == "lumen agent"
    assert result.provider == "tavily"
    assert result.answer == "Tavily synthesized answer"
    assert result.engine_errors == []
    first, second = result.results
    assert (first.title, first.url, first.snippet) == ("One", "https://one.example", "first snippet")
    assert first.score == 0.91
    # RFC-2822 is normalized to ISO 8601 at the provider seam.
    assert first.published == "2026-09-01T10:00:00+00:00"
    assert second.published is None and second.score is None
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.tavily.com/search"
    assert request.headers["authorization"] == "Bearer tavily-test-token"
    assert json.loads(request.content) == {"query": "lumen agent", "max_results": 8}


def test_brave_search_request_and_result_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-token")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "web": {
                    "results": [
                        {
                            "title": "One",
                            "url": "https://one.example",
                            "description": "first hit",
                            "page_age": "2026-09-10T08:30:00",
                        },
                        {"title": "Two", "url": "https://two.example", "description": "second hit"},
                    ]
                }
            },
        )

    spec = build_web_search_spec(
        WebSearchConfig(provider="brave", api_key_env="BRAVE_API_KEY", max_results=5),
        transport=httpx.MockTransport(respond),
    )
    result = spec.function("lumen agent")

    assert result.provider == "brave"
    assert result.answer is None
    first, second = result.results
    assert (first.title, first.url, first.snippet) == ("One", "https://one.example", "first hit")
    assert first.published == "2026-09-10T08:30:00"
    assert second.published is None
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url == httpx.URL(
        "https://api.search.brave.com/res/v1/web/search", params={"q": "lumen agent", "count": 5}
    )
    assert request.headers["x-subscription-token"] == "brave-test-token"
    assert request.headers["accept"] == "application/json"


def test_web_search_requires_configured_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_TAVILY_KEY", raising=False)

    with pytest.raises(ValueError, match="MISSING_TAVILY_KEY"):
        build_web_search_spec(WebSearchConfig(provider="tavily", api_key_env="MISSING_TAVILY_KEY"))


def test_web_search_validates_query_and_max_results(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _tavily_spec(
        monkeypatch,
        httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, json={"results": []})
        ),
    )

    with pytest.raises(ValueError, match="query must not be empty"):
        spec.function("   ")
    with pytest.raises(ValueError, match="max_results must be between 1 and 20"):
        spec.function("q", max_results=0)
    with pytest.raises(ValueError, match="max_results must be between 1 and 20"):
        spec.function("q", max_results=21)


def test_web_search_empty_results_stay_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _tavily_spec(
        monkeypatch,
        httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, json={"results": ["not-a-dict"]})
        ),
    )

    result = spec.function("nothing")

    assert result.query == "nothing"
    assert result.results == []


def test_web_search_bounds_total_snippet_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [
        {"title": f"R{index}", "url": f"https://e{index}.example", "content": "x" * 40_000}
        for index in range(3)
    ]
    spec = _tavily_spec(
        monkeypatch,
        httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, json={"results": results})
        ),
    )

    result = spec.function("q")

    total_snippet_chars = sum(len(item.snippet) for item in result.results)
    assert 0 < total_snippet_chars <= MAX_CONTENT_CHARS
    # The first snippet passes through whole; later ones are truncated/dropped.
    assert result.results[0].snippet == "x" * 40_000


# --------------------------------------------------------------------------- #
# SearXNG provider
# --------------------------------------------------------------------------- #


def _searxng_spec(
    transport: httpx.MockTransport,
    *,
    engines: list[str] | None = None,
    language: str | None = None,
    api_key_env: str | None = None,
) -> ToolSpec:
    config = WebSearchConfig(
        provider="searxng",
        base_url="http://127.0.0.1:8080",
        engines=engines,
        language=language,
        api_key_env=api_key_env,
    )
    return build_web_search_spec(config, transport=transport)


def test_searxng_happy_path_mapping_and_wire_format() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "query": "lumen",
                "number_of_results": 99,
                "results": [
                    {
                        "url": "https://one.example",
                        "title": "One",
                        "content": "snippet one",
                        "publishedDate": "2026-09-01T10:00:00",
                        "score": 3.5,
                        "engine": "duckduckgo",
                    },
                    {"url": "https://two.example", "title": "Two", "content": "snippet two"},
                ],
                "unresponsive_engines": [],
            },
        )

    spec = _searxng_spec(httpx.MockTransport(respond))
    result = spec.function("lumen")

    assert result.provider == "searxng"
    assert result.answer is None
    assert result.engine_errors == []
    first, second = result.results
    assert (first.title, first.url, first.snippet) == ("One", "https://one.example", "snippet one")
    assert first.published == "2026-09-01T10:00:00"
    # SearXNG ranking scores exceed 1.0 and pass through unmodified.
    assert first.score == 3.5
    assert second.published is None and second.score is None
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    # The localhost base_url is accepted without the public-host SSRF check:
    # explicit operator configuration is treated as trust.
    assert request.url == httpx.URL(
        "http://127.0.0.1:8080/search", params={"q": "lumen", "format": "json"}
    )
    assert "authorization" not in request.headers


def test_searxng_sends_engines_and_language_params() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={"results": []})

    spec = _searxng_spec(
        httpx.MockTransport(respond), engines=["duckduckgo", "brave"], language="all"
    )
    result = spec.function("lumen")

    assert result.results == []
    assert requests[0].url == httpx.URL(
        "http://127.0.0.1:8080/search",
        params={"q": "lumen", "format": "json", "engines": "duckduckgo,brave", "language": "all"},
    )


def test_searxng_empty_results() -> None:
    spec = _searxng_spec(
        httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, json={"results": []})
        )
    )

    result = spec.function("nothing")

    assert result.results == []
    assert result.engine_errors == []


def test_searxng_403_explains_json_format_must_be_enabled() -> None:
    spec = _searxng_spec(
        httpx.MockTransport(lambda request: httpx.Response(403, request=request))
    )

    with pytest.raises(ValueError, match=r"search\.formats.*json"):
        spec.function("lumen")


def test_searxng_unresponsive_engines_become_engine_errors() -> None:
    spec = _searxng_spec(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                json={
                    "results": [{"url": "https://one.example", "title": "One", "content": "s"}],
                    "unresponsive_engines": [
                        ["google", "timeout"],
                        ["bing", "unexpected HTTP status 403"],
                    ],
                },
            )
        )
    )

    result = spec.function("lumen")

    assert len(result.results) == 1
    assert result.engine_errors == ["google: timeout", "bing: unexpected HTTP status 403"]


def test_searxng_trims_results_to_max_results() -> None:
    payload = {
        "results": [
            {"url": f"https://e{index}.example", "title": f"T{index}", "content": "s"}
            for index in range(3)
        ]
    }
    spec = _searxng_spec(
        httpx.MockTransport(lambda request: httpx.Response(200, request=request, json=payload))
    )

    result = spec.function("lumen", max_results=2)

    assert [item.url for item in result.results] == ["https://e0.example", "https://e1.example"]


def test_searxng_optional_token_adds_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_TOKEN", "proxy-token")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={"results": []})

    spec = _searxng_spec(httpx.MockTransport(respond), api_key_env="SEARXNG_TOKEN")
    spec.function("lumen")

    assert requests[0].headers["authorization"] == "Bearer proxy-token"


def test_searxng_configured_but_missing_token_env_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARXNG_TOKEN", raising=False)

    with pytest.raises(ValueError, match="SEARXNG_TOKEN"):
        _searxng_spec(httpx.MockTransport(lambda request: httpx.Response(200)), api_key_env="SEARXNG_TOKEN")


# --------------------------------------------------------------------------- #
# Structured output contract
# --------------------------------------------------------------------------- #


def test_search_output_contract_produces_canonical_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _tavily_spec(
        monkeypatch,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                json={"results": [{"title": "One", "url": "https://one.example", "content": "s"}]},
            )
        ),
    )

    canonical = spec.output_contract.validate(spec.function("q"))

    assert isinstance(canonical, dict)
    assert canonical["provider"] == "tavily"
    assert canonical["results"] == [
        {"title": "One", "url": "https://one.example", "snippet": "s", "published": None, "score": None}
    ]
    with pytest.raises(ValidationError):
        spec.output_contract.validate({"query": "q", "provider": "tavily", "results": [{"url": 1}]})


async def test_web_search_gateway_returns_canonical_dict_and_model_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "token")
    spec = build_web_search_spec(
        WebSearchConfig(provider="tavily", api_key_env="TAVILY_API_KEY"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                json={"results": [{"title": "One", "url": "https://one.example", "content": "s"}]},
            )
        ),
    )
    registry = ToolRegistry(tmp_path)
    registry.add(spec, origin="builtin")
    gateway = CapabilityGateway(
        registry, PermissionPolicy(PermissionsConfig()), default_timeout=2
    )

    async def approve(_: ApprovalRequest) -> CapabilityApproval:
        return CapabilityApproval(approved=True)

    result = await gateway.invoke(
        CapabilityInvocation(
            execution_id="run",
            provider_call_id="search",
            name="web_search",
            arguments={"query": "q"},
        ),
        approve=approve,
    )

    assert result.status is CapabilityStatus.SUCCEEDED
    output = cast(dict[str, Any], result.output)
    assert output["provider"] == "tavily"
    assert output["results"][0]["url"] == "https://one.example"
    assert isinstance(result.model_output, str)
    assert "https://one.example" in result.model_output
