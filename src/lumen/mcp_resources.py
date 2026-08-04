"""Explicit MCP resource and prompt discovery/loading."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import BaseModel

from lumen.config import McpServerConfig
from lumen.mcp_tools import McpToolsetBundle


def _dump(value: object) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    return {"value": str(value)}


def _text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        items = cast(list[object] | tuple[object, ...], value)
        return "\n".join(_text_content(item) for item in items)
    data = _dump(value)
    if isinstance(data.get("text"), str):
        return cast(str, data["text"])
    content = data.get("content")
    if content is not None:
        return _text_content(content)
    messages = data.get("messages")
    if messages is not None:
        return _text_content(messages)
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


@dataclass(frozen=True, slots=True)
class McpResourceDescriptor:
    server: str
    uri: str
    name: str
    description: str = ""
    mime_type: str | None = None

    @property
    def reference(self) -> str:
        return f"{self.server}::{self.uri}"


@dataclass(frozen=True, slots=True)
class McpPromptDescriptor:
    server: str
    name: str
    description: str = ""
    arguments: tuple[str, ...] = ()

    @property
    def reference(self) -> str:
        return f"{self.server}:{self.name}"


@dataclass
class McpContentRegistry:
    resources: list[McpResourceDescriptor] = field(default_factory=list[McpResourceDescriptor])
    prompts: list[McpPromptDescriptor] = field(default_factory=list[McpPromptDescriptor])
    _bundles: dict[str, McpToolsetBundle] = field(default_factory=dict[str, McpToolsetBundle])

    async def add_server(self, bundle: McpToolsetBundle, config: McpServerConfig) -> None:
        self._bundles[bundle.name] = bundle
        if config.load_resources:
            for resource in await bundle.client.list_resources():
                raw = _dump(resource)
                self.resources.append(
                    McpResourceDescriptor(
                        server=bundle.name,
                        uri=str(raw.get("uri", "")),
                        name=str(raw.get("name") or raw.get("uri") or "resource"),
                        description=str(raw.get("description") or ""),
                        mime_type=(
                            str(raw.get("mimeType") or raw.get("mime_type"))
                            if raw.get("mimeType") or raw.get("mime_type")
                            else None
                        ),
                    )
                )
        if config.load_prompts:
            for prompt in await bundle.client.list_prompts():
                raw = _dump(prompt)
                raw_args = raw.get("arguments")
                args = (
                    [
                        cast(dict[str, Any], item)
                        for item in cast(list[object], raw_args)
                        if isinstance(item, dict)
                    ]
                    if isinstance(raw_args, list)
                    else []
                )
                self.prompts.append(
                    McpPromptDescriptor(
                        server=bundle.name,
                        name=str(raw.get("name", "")),
                        description=str(raw.get("description") or ""),
                        arguments=tuple(str(item.get("name")) for item in args if item.get("name")),
                    )
                )

    async def fetch_resource(self, reference: str) -> dict[str, object]:
        """Fetch a resource body without creating workspace-global active state."""

        descriptor = self._resolve_resource(reference)
        bundle = self._bundles[descriptor.server]
        content = await bundle.client.read_resource(descriptor.uri)
        text = _text_content(content)
        document: dict[str, object] = {
            "name": descriptor.name,
            "uri": descriptor.uri,
            "server": descriptor.server,
            "body": text,
        }
        return document

    async def activate_resource(self, reference: str) -> dict[str, object]:
        """Compatibility alias; activation ownership now belongs to a session."""

        return await self.fetch_resource(reference)

    async def render_prompt(self, reference: str, arguments: dict[str, str]) -> str:
        descriptor = self._resolve_prompt(reference)
        result = await self._bundles[descriptor.server].client.get_prompt(descriptor.name, arguments or None)
        return _text_content(result)

    def documents(self) -> tuple[dict[str, object], ...]:
        """Compatibility view: the catalog no longer owns an active working set."""

        return ()

    def _resolve_resource(self, reference: str) -> McpResourceDescriptor:
        if "::" in reference:
            server, uri = reference.split("::", 1)
            matches = [item for item in self.resources if item.server == server and item.uri == uri]
        else:
            matches = [item for item in self.resources if item.uri == reference or item.name == reference]
        if len(matches) != 1:
            raise KeyError(f"MCP resource reference is unknown or ambiguous: {reference!r}")
        return matches[0]

    def _resolve_prompt(self, reference: str) -> McpPromptDescriptor:
        if ":" in reference:
            server, name = reference.split(":", 1)
            matches = [item for item in self.prompts if item.server == server and item.name == name]
        else:
            matches = [item for item in self.prompts if item.name == reference]
        if len(matches) != 1:
            raise KeyError(f"MCP prompt reference is unknown or ambiguous: {reference!r}")
        return matches[0]


__all__ = [
    "McpContentRegistry",
    "McpPromptDescriptor",
    "McpResourceDescriptor",
]
