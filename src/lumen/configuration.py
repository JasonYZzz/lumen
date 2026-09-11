from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml

from lumen.config import AgentSection, ConfigLoadError, ModelSettingsConfig
from lumen.config_resolver import ConfigResolution, ConfigResolver
from lumen.models import native_web_search_enabled

_MANAGED_HEADER = "# Managed by Lumen Web settings. Edit through the UI or remove this file.\n"
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class ConfigurationEditError(ValueError):
    """The requested configuration mutation is unsafe or invalid."""


class ConfigurationConflictError(ConfigurationEditError):
    """The configuration changed after the client loaded its snapshot."""


@dataclass(frozen=True, slots=True)
class ModelConfigurationView:
    name: str
    id: str
    api: str | None
    base_url: str | None
    api_key_env: str | None
    settings: dict[str, Any]
    context: dict[str, Any]
    input_modalities: tuple[str, ...]
    is_default: bool
    source: dict[str, str] | None
    auth_kind: str
    auth_available: bool
    reasoning_effort: str | None = None
    reasoning_levels: tuple[str, ...] | None = None
    reasoning_profile: str | None = None
    native_web_search: dict[str, Any] | None = None
    native_web_search_enabled: bool = False


@dataclass(frozen=True, slots=True)
class McpServerConfigurationView:
    name: str
    enabled: bool
    source: dict[str, str] | None


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    revision: str
    target_path: str
    editable: bool
    edit_reason: str | None
    exclusive: bool
    sources: list[dict[str, str]]
    warnings: list[str]
    default_model: str
    models: list[ModelConfigurationView]
    mcp_servers: list[McpServerConfigurationView]

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "models": [asdict(model) for model in self.models],
            "mcp_servers": [asdict(server) for server in self.mcp_servers],
        }


class WorkspaceConfiguration:
    """Inspect and safely mutate the workspace's managed configuration layer.

    The browser never writes YAML and never receives resolved secrets. This
    Module owns optimistic concurrency, strict merged validation and atomic
    publication behind a small Interface.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        explicit_path: str | Path | None = None,
        project_trusted: bool = True,
        environ: Mapping[str, str] | None = None,
        home: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.explicit_path = (
            Path(explicit_path).expanduser().resolve() if explicit_path is not None else None
        )
        self.project_trusted = project_trusted
        self.environ = dict(environ) if environ is not None else None
        self.home = Path(home).expanduser() if home is not None else None
        self.target_path = self.workspace / ".lumen" / "agent.web.yaml"

    def inspect(self) -> ConfigurationSnapshot:
        resolution = self._resolver().resolve(project_trusted=self.project_trusted)
        return self._snapshot(resolution)

    def resolved_agent(self) -> AgentSection:
        """Return the effective Agent configuration for runtime publication.

        Secrets stay inside the application layer: Web clients continue to
        receive only :class:`ConfigurationSnapshot`, while ``WorkspaceHost``
        can publish the exact, environment-resolved model registry after a
        managed mutation.
        """

        return self._resolver().resolve(project_trusted=self.project_trusted).config.agent

    def upsert_model(
        self,
        *,
        expected_revision: str,
        name: str,
        definition: dict[str, Any],
        set_default: bool,
    ) -> ConfigurationSnapshot:
        self._require_editable(expected_revision)
        if _MODEL_NAME.fullmatch(name) is None:
            raise ConfigurationEditError(
                "model name must use letters, numbers, dots, underscores or hyphens"
            )
        try:
            model = ModelSettingsConfig.model_validate(definition)
        except ValueError as error:
            raise ConfigurationEditError(str(error)) from error

        current = self._resolver().resolve(project_trusted=self.project_trusted)
        existing = current.config.agent.model_registry().get(name)
        if (
            existing is not None
            and existing.api_key is not None
            and existing.api_key_env is None
            and model.api_key_env is None
        ):
            raise ConfigurationEditError(
                "this model uses an inline API key; set api_key_env before editing it in Web settings"
            )

        raw = self._read_managed()
        agent = self._mapping(raw, "agent")
        models = self._mapping(agent, "models")
        if current.config.agent.model is not None:
            previous_name = current.config.agent.default_model_name()
            previous_model = current.config.agent.model
            if name != previous_name:
                if previous_model.api_key is not None and previous_model.api_key_env is None:
                    raise ConfigurationEditError(
                        "the existing single model uses an inline API key; edit it to use "
                        "api_key_env before adding another model"
                    )
                models[previous_name] = previous_model.model_dump(
                    mode="json",
                    exclude_none=True,
                )
        models[name] = model.model_dump(mode="json", exclude_none=True)
        agent.pop("model", None)
        if current.config.agent.model is not None:
            agent["default_model"] = name if set_default else current.config.agent.default_model_name()
        elif set_default:
            agent["default_model"] = name
        return self._validate_write_and_snapshot(raw)

    def remove_model(self, *, expected_revision: str, name: str) -> ConfigurationSnapshot:
        self._require_editable(expected_revision)
        current = self._resolver().resolve(project_trusted=self.project_trusted)
        if current.config.agent.model is not None:
            raise ConfigurationEditError(
                "convert the single-model configuration by adding a model before removing it"
            )
        registry = current.config.agent.model_registry()
        if name not in registry:
            raise ConfigurationEditError(f"unknown model: {name}")
        remaining = [item for item in registry if item != name]
        if not remaining:
            raise ConfigurationEditError("at least one model must remain configured")

        raw = self._read_managed()
        agent = self._mapping(raw, "agent")
        models = self._mapping(agent, "models")
        models[name] = None
        agent.pop("model", None)
        if current.config.agent.default_model_name() == name:
            agent["default_model"] = remaining[0]
        return self._validate_write_and_snapshot(raw)

    def set_mcp_server_enabled(
        self,
        *,
        expected_revision: str,
        name: str,
        enabled: bool,
    ) -> ConfigurationSnapshot:
        """Persist a non-secret activation override without copying server credentials."""

        self._require_editable(expected_revision)
        current = self._resolver().resolve(project_trusted=self.project_trusted)
        if name not in current.config.mcp_servers:
            raise ConfigurationEditError(f"unknown MCP server: {name}")
        raw = self._read_managed()
        mcp = self._mapping(raw, "mcp")
        enabled_servers = self._mapping(mcp, "enabled")
        enabled_servers[name] = enabled
        return self._validate_write_and_snapshot(raw)

    def _resolver(self) -> ConfigResolver:
        return ConfigResolver(
            self.workspace,
            explicit_path=self.explicit_path,
            environ=self.environ,
            home=self.home,
        )

    def _snapshot(self, resolution: ConfigResolution) -> ConfigurationSnapshot:
        report = resolution.report().as_dict()
        provenance = cast(dict[str, dict[str, str]], report["provenance"])
        effective = cast(dict[str, Any], report["effective_config"])
        reported_agent = cast(dict[str, Any], effective["agent"])
        registry = resolution.config.agent.model_registry()
        default_model = resolution.config.agent.default_model_name()
        multi = bool(resolution.config.agent.models)
        if multi:
            reported_registry = cast(dict[str, dict[str, Any]], reported_agent.get("models", {}))
        else:
            reported_registry = {default_model: cast(dict[str, Any], reported_agent["model"])}
        models: list[ModelConfigurationView] = []
        for name, model in registry.items():
            source_key = f"agent.models.{name}.id" if multi else "agent.model.id"
            reported_model = reported_registry[name]
            if model.api_key_env is not None:
                auth_kind = "environment"
            elif model.api_key is not None:
                auth_kind = "inline"
            else:
                auth_kind = "none"
            models.append(
                ModelConfigurationView(
                    reasoning_effort=model.reasoning_effort,
                    reasoning_levels=model.reasoning_levels,
                    reasoning_profile=model.reasoning_profile,
                    name=name,
                    id=str(reported_model["id"]),
                    api=cast(str | None, reported_model.get("api")),
                    base_url=cast(str | None, reported_model.get("base_url")),
                    api_key_env=cast(str | None, reported_model.get("api_key_env")),
                    settings=cast(dict[str, Any], reported_model.get("settings", {})),
                    context=cast(dict[str, Any], reported_model.get("context", {})),
                    input_modalities=tuple(
                        str(item)
                        for item in cast(
                            list[str] | tuple[str, ...],
                            reported_model.get("input_modalities", ("text",)),
                        )
                    ),
                    is_default=name == default_model,
                    source=provenance.get(source_key),
                    auth_kind=auth_kind,
                    auth_available=model.api_key is not None,
                    native_web_search=model.native_web_search.model_dump(mode="json"),
                    native_web_search_enabled=native_web_search_enabled(model),
                )
            )
        editable, reason = self._editability()
        return ConfigurationSnapshot(
            revision=self._revision(),
            target_path=str(self.target_path),
            editable=editable,
            edit_reason=reason,
            exclusive=resolution.exclusive,
            sources=cast(list[dict[str, str]], report["sources"]),
            warnings=cast(list[str], report["warnings"]),
            default_model=default_model,
            models=models,
            mcp_servers=[
                McpServerConfigurationView(
                    name=name,
                    enabled=resolution.config.mcp.enabled.get(name, True),
                    source=(
                        provenance.get(f"mcp.enabled.{name}")
                        or provenance.get(f"mcp_servers.{name}.transport")
                    ),
                )
                for name in sorted(resolution.config.mcp_servers)
            ],
        )

    def _editability(self) -> tuple[bool, str | None]:
        if self.explicit_path is not None:
            return False, "explicit --config mode is read-only in Web settings"
        if self.target_path.is_file():
            try:
                text = self.target_path.read_text(encoding="utf-8")
            except OSError as error:
                return False, f"cannot read managed configuration: {error}"
            if not text.startswith(_MANAGED_HEADER):
                return False, "agent.web.yaml is not owned by Lumen Web settings"
        return True, None

    def _require_editable(self, expected_revision: str) -> None:
        editable, reason = self._editability()
        if not editable:
            raise ConfigurationEditError(reason or "configuration is read-only")
        current = self._revision()
        if expected_revision != current:
            raise ConfigurationConflictError(
                "configuration changed after this settings view was opened; reload and try again"
            )

    def _read_managed(self) -> dict[str, Any]:
        if not self.target_path.is_file():
            return {"version": 2}
        text = self.target_path.read_text(encoding="utf-8")
        if not text.startswith(_MANAGED_HEADER):
            raise ConfigurationEditError("refusing to overwrite an unmanaged agent.web.yaml")
        try:
            loaded = yaml.safe_load(text[len(_MANAGED_HEADER) :])
        except yaml.YAMLError as error:
            raise ConfigurationEditError(f"invalid managed configuration: {error}") from error
        if not isinstance(loaded, dict):
            raise ConfigurationEditError("managed configuration root must be a mapping")
        return cast(dict[str, Any], loaded)

    @staticmethod
    def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
        value = parent.get(key)
        if value is None:
            result: dict[str, Any] = {}
            parent[key] = result
            return result
        if not isinstance(value, dict):
            raise ConfigurationEditError(f"managed configuration field {key} must be a mapping")
        return cast(dict[str, Any], value)

    def _validate_write_and_snapshot(self, raw: dict[str, Any]) -> ConfigurationSnapshot:
        resolver = self._resolver()
        try:
            resolver.resolve(
                project_trusted=self.project_trusted,
                source_overrides={self.target_path: raw},
            )
        except ConfigLoadError as error:
            raise ConfigurationEditError(str(error)) from error
        body = _MANAGED_HEADER + yaml.safe_dump(
            raw,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        self._atomic_write(body)
        return self.inspect()

    def _revision(self) -> str:
        digest = hashlib.sha256()
        for source in self._resolver().candidate_sources():
            digest.update(source.scope.value.encode())
            digest.update(b"\0")
            digest.update(str(source.path).encode())
            digest.update(b"\0")
            try:
                digest.update(source.path.read_bytes())
            except FileNotFoundError:
                digest.update(b"<missing>")
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def _atomic_write(self, text: str) -> None:
        self.target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.target_path.with_name(
            f".{self.target_path.name}.{uuid4().hex}.tmp"
        )
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                data = text.encode("utf-8")
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("configuration write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.target_path)
            directory = os.open(self.target_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "ConfigurationConflictError",
    "ConfigurationEditError",
    "ConfigurationSnapshot",
    "McpServerConfigurationView",
    "ModelConfigurationView",
    "WorkspaceConfiguration",
]
