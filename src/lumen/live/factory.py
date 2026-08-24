"""Construct the configured Live Provider Router without leaking Provider logic to Host startup."""

from __future__ import annotations

from lumen.config import LiveConfig, OpenAILiveRouteConfig
from lumen.live.bailian_realtime import BailianRealtimeAdapter
from lumen.live.openai_realtime import OpenAIRealtimeAdapter
from lumen.live.protocol import LiveCompletionControl, RealtimeProviderAdapter
from lumen.live.router import LiveProviderRouter, LiveRouteProfile


def build_live_router(config: LiveConfig) -> LiveProviderRouter:
    routes: dict[str, tuple[LiveRouteProfile, RealtimeProviderAdapter]] = {}
    if config.routes:
        for name, route in config.routes.items():
            if not route.api_key:
                raise ValueError(f"Live route {name} does not have a resolved API key")
            if isinstance(route, OpenAILiveRouteConfig):
                adapter = OpenAIRealtimeAdapter(
                    api_key=route.api_key,
                    base_url=route.base_url,
                    model=route.model,
                )
                region = None
            else:
                if not route.workspace_id:
                    raise ValueError(f"Bailian Live route {name} does not have a resolved workspace ID")
                adapter = BailianRealtimeAdapter(
                    api_key=route.api_key,
                    workspace_id=route.workspace_id,
                    model=route.model,
                    region=route.region,
                    base_url=route.base_url,
                    completion_control=LiveCompletionControl(route.completion_control),
                )
                region = route.region
            routes[name] = (
                LiveRouteProfile(
                    name=name,
                    provider=route.provider,
                    model=route.model,
                    voice=route.voice,
                    region=region,
                ),
                adapter,
            )
        assert config.default_route is not None
        return LiveProviderRouter(
            routes=routes,
            default_route=config.default_route,
            fallback_routes=tuple(config.fallback_routes),
        )

    if not config.api_key:
        raise ValueError("legacy OpenAI Live configuration does not have a resolved API key")
    adapter = OpenAIRealtimeAdapter(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
    )
    return LiveProviderRouter(
        routes={
            "legacy-openai": (
                LiveRouteProfile(
                    name="legacy-openai",
                    provider="openai",
                    model=config.model,
                    voice=config.voice,
                ),
                adapter,
            )
        },
        default_route="legacy-openai",
    )


__all__ = ["build_live_router"]
