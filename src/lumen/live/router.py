"""Capability-aware route selection for realtime Provider adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from lumen.live.protocol import (
    LiveCompletionControl,
    LiveMediaKind,
    LiveProviderConnection,
    LiveProviderEventSink,
    LiveProviderOpenRequest,
    RealtimeProviderAdapter,
)


class LiveRoutingError(RuntimeError):
    pass


class LiveRouteProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    provider: str
    model: str
    voice: str | None = None
    region: str | None = None


class LiveRouteRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    media: LiveMediaKind | None = None
    function_calling: bool = False
    interruption: bool = False
    input_transcription: bool = False
    server_tool_authority: bool = False
    strict_completion: bool = False


class LiveRouteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: str
    provider: str
    model: str
    voice: str | None = None
    region: str | None = None
    media: LiveMediaKind
    completion_control: LiveCompletionControl


@dataclass(frozen=True, slots=True)
class RoutedLiveConnection:
    snapshot: LiveRouteSnapshot
    connection: LiveProviderConnection


@dataclass(frozen=True, slots=True)
class _Route:
    profile: LiveRouteProfile
    adapter: RealtimeProviderAdapter


class LiveProviderRouter:
    """Select once before media establishment; an opened call never changes Route."""

    def __init__(
        self,
        *,
        routes: dict[str, tuple[LiveRouteProfile, RealtimeProviderAdapter]],
        default_route: str,
        fallback_routes: tuple[str, ...] = (),
    ) -> None:
        if default_route not in routes:
            raise ValueError(f"default Live route is not configured: {default_route}")
        missing = [name for name in fallback_routes if name not in routes]
        if missing:
            raise ValueError(f"fallback Live routes are not configured: {', '.join(missing)}")
        self._routes = {
            name: _Route(profile=profile, adapter=adapter)
            for name, (profile, adapter) in routes.items()
        }
        self.default_route = default_route
        self.fallback_routes = fallback_routes

    async def open(
        self,
        request: LiveProviderOpenRequest,
        event_sink: LiveProviderEventSink,
        *,
        route_name: str | None = None,
        requirements: LiveRouteRequirements | None = None,
    ) -> RoutedLiveConnection:
        required = requirements or LiveRouteRequirements()
        first = route_name or self.default_route
        if first not in self._routes:
            raise LiveRoutingError(f"Live route is not configured: {first}")
        candidates = tuple(dict.fromkeys((first, *self.fallback_routes)))
        errors: list[str] = []
        for name in candidates:
            route = self._routes[name]
            issue = self._capability_issue(route.adapter, required)
            if issue:
                errors.append(f"{name}: {issue}")
                continue
            try:
                routed_request = request.model_copy(
                    update={
                        "spec": request.spec.model_copy(update={"voice": route.profile.voice})
                        if route.profile.voice is not None
                        else request.spec
                    }
                )
                connection = await route.adapter.open(routed_request, event_sink)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                errors.append(f"{name}: {type(error).__name__}: {error}")
                continue
            media = connection.handshake.kind
            if media not in route.adapter.capabilities.media:
                await connection.close()
                errors.append(f"{name}: adapter returned undeclared media mode {media.value}")
                continue
            return RoutedLiveConnection(
                snapshot=LiveRouteSnapshot(
                    route=name,
                    provider=route.profile.provider,
                    model=route.profile.model,
                    voice=route.profile.voice,
                    region=route.profile.region,
                    media=media,
                    completion_control=route.adapter.capabilities.completion_control,
                ),
                connection=connection,
            )
        raise LiveRoutingError("no usable Live route; " + "; ".join(errors))

    async def close(self) -> None:
        adapters = {id(route.adapter): route.adapter for route in self._routes.values()}
        for adapter in adapters.values():
            await adapter.close()

    @staticmethod
    def _capability_issue(
        adapter: RealtimeProviderAdapter,
        required: LiveRouteRequirements,
    ) -> str | None:
        capabilities = adapter.capabilities
        if required.media is not None and required.media not in capabilities.media:
            return f"media mode {required.media.value} is not supported"
        for field in (
            "function_calling",
            "interruption",
            "input_transcription",
            "server_tool_authority",
        ):
            if getattr(required, field) and not getattr(capabilities, field):
                return f"required capability {field} is not supported"
        if (
            required.strict_completion
            and capabilities.completion_control is LiveCompletionControl.ADVISORY_ONLY
        ):
            return "strict completion is not supported"
        return None


__all__ = [
    "LiveProviderRouter",
    "LiveRouteProfile",
    "LiveRouteRequirements",
    "LiveRouteSnapshot",
    "LiveRoutingError",
    "RoutedLiveConnection",
]
