from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml

from lumen.config import AppConfig, ConfigLoadError, validate_config_data


class ConfigScope(StrEnum):
    USER = "user"
    LEGACY = "legacy"
    PROJECT = "project"
    LOCAL = "local"
    MANAGED = "managed"
    EXPLICIT = "explicit"


@dataclass(frozen=True, slots=True)
class ConfigSource:
    scope: ConfigScope
    path: Path

    def as_dict(self) -> dict[str, str]:
        return {"scope": self.scope.value, "path": str(self.path)}


@dataclass(frozen=True, slots=True)
class ConfigResolution:
    config: AppConfig
    sources: tuple[ConfigSource, ...]
    searched_paths: tuple[Path, ...]
    warnings: tuple[str, ...]
    exclusive: bool
    provenance: Mapping[str, ConfigSource] = field(default_factory=dict[str, ConfigSource])

    def report(self, *, config: AppConfig | None = None) -> ResolvedConfigReport:
        """Return a read-only, secret-safe projection of the effective config.

        The report is diagnostic data only. Runtime decisions continue to use
        :attr:`config`, keeping configuration ownership in ``ConfigResolver``.
        """

        effective_config = _redacted_config(config or self.config)
        effective_paths = set(_declared_paths(effective_config))
        return ResolvedConfigReport(
            effective_config=effective_config,
            sources=self.sources,
            searched_paths=self.searched_paths,
            warnings=self.warnings,
            exclusive=self.exclusive,
            provenance={
                field_path: source
                for field_path, source in self.provenance.items()
                if field_path in effective_paths
            },
        )


@dataclass(frozen=True, slots=True)
class ResolvedConfigReport:
    effective_config: dict[str, Any]
    sources: tuple[ConfigSource, ...]
    searched_paths: tuple[Path, ...]
    warnings: tuple[str, ...]
    exclusive: bool
    provenance: Mapping[str, ConfigSource]

    def as_dict(self) -> dict[str, Any]:
        return {
            "effective_config": self.effective_config,
            "sources": [source.as_dict() for source in self.sources],
            "searched_paths": [str(path) for path in self.searched_paths],
            "warnings": list(self.warnings),
            "exclusive": self.exclusive,
            "provenance": {
                field_path: source.as_dict()
                for field_path, source in sorted(self.provenance.items())
            },
        }


def _declared_paths(value: Any, prefix: tuple[str, ...] = ()) -> tuple[str, ...]:
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        paths = tuple(
            path
            for key, item in mapping.items()
            for path in _declared_paths(item, (*prefix, str(key)))
        )
        return paths or ((".".join(prefix),) if prefix else ())
    if isinstance(value, list):
        return ((".".join(prefix),) if prefix else ())
    return ((".".join(prefix),) if prefix else ())


def _clear_provenance_prefix(provenance: dict[str, ConfigSource], prefix: str) -> None:
    for field_path in tuple(provenance):
        if field_path == prefix or field_path.startswith(f"{prefix}."):
            provenance.pop(field_path)


def _record_provenance(
    provenance: dict[str, ConfigSource],
    layer: dict[str, Any],
    source: ConfigSource,
) -> None:
    agent = _mapping(layer.get("agent"))
    if agent is not None:
        if "model" in agent:
            _clear_provenance_prefix(provenance, "agent.model")
            _clear_provenance_prefix(provenance, "agent.models")
            _clear_provenance_prefix(provenance, "agent.default_model")
        if "models" in agent:
            _clear_provenance_prefix(provenance, "agent.model")
            models = _mapping(agent.get("models")) or {}
            for name in models:
                _clear_provenance_prefix(provenance, f"agent.models.{name}")
    servers = _mapping(layer.get("mcp_servers")) or {}
    for name in servers:
        _clear_provenance_prefix(provenance, f"mcp_servers.{name}")
    for field_path in _declared_paths(layer):
        provenance[field_path] = source


_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "password",
        "secret",
        "token",
    }
)


def _is_secret_key(key: str) -> bool:
    return key in _SECRET_KEYS or key.endswith(("_api_key", "_password", "_secret", "_token"))


def _is_secret_environment_name(name: str) -> bool:
    normalized = name.lower()
    return _is_secret_key(normalized) or normalized.endswith(
        ("_authorization", "_cookie", "_key")
    )


def _redacted_config(config: AppConfig) -> dict[str, Any]:
    raw = config.model_dump(mode="json")
    secret_values = {
        value
        for name, value in os.environ.items()
        if value and _is_secret_environment_name(name)
    }

    def redact(value: Any, path: tuple[str, ...]) -> Any:
        if len(path) >= 4 and path[0] == "mcp_servers" and path[2] in {"env", "headers"}:
            return "<redacted>"
        if path:
            key = path[-1].lower()
            if key not in {"api_key_env", "workspace_id_env", "credential_file"} and _is_secret_key(
                key
            ):
                return "<redacted>"
        if isinstance(value, dict):
            mapping = cast(dict[str, Any], value)
            return {str(key): redact(item, (*path, str(key))) for key, item in mapping.items()}
        if isinstance(value, list):
            items = cast(list[Any], value)
            return [redact(item, (*path, str(index))) for index, item in enumerate(items)]
        if isinstance(value, str) and any(secret in value for secret in secret_values):
            return "<redacted>"
        return value

    return cast(dict[str, Any], redact(raw, ()))


def _dedupe(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _mapping(value: Any) -> dict[str, Any] | None:
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _list(value: Any) -> list[Any] | None:
    return cast(list[Any], value) if isinstance(value, list) else None


def _fingerprint_mcp_definition(value: Mapping[str, Any]) -> str:
    """Hash the unexpanded, persisted server definition.

    Secret placeholders are intentionally kept as placeholders; environment
    values never enter the approval database.
    """

    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_declared_path(value: Any, source: ConfigSource) -> Any:
    if not isinstance(value, (str, Path)):
        return value
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = source.path.parent / path
    return path.resolve()


def _prepare_layer(raw: dict[str, Any], source: ConfigSource) -> dict[str, Any]:
    prepared = copy.deepcopy(raw)
    agent = _mapping(prepared.get("agent"))
    prompt = _mapping(agent.get("prompt")) if agent is not None else None
    if prompt is not None:
        for key in ("append_file", "replace_file"):
            if prompt.get(key) is not None:
                prompt[key] = _resolve_declared_path(prompt[key], source)

    sessions = _mapping(prepared.get("sessions"))
    if sessions is not None and sessions.get("directory") is not None:
        sessions["directory"] = _resolve_declared_path(sessions["directory"], source)

    tools = _mapping(prepared.get("tools"))
    plugins = _list(tools.get("plugins")) if tools is not None else None
    if plugins is not None:
        for plugin_value in plugins:
            plugin = _mapping(plugin_value)
            if plugin is not None:
                plugin["source_dir"] = source.path.parent

    servers = _mapping(prepared.get("mcp_servers"))
    if servers is not None:
        for definition_value in servers.values():
            definition = _mapping(definition_value)
            if definition is not None:
                fingerprint = _fingerprint_mcp_definition(definition)
                definition["source_scope"] = source.scope.value
                definition["source_path"] = source.path
                definition["definition_fingerprint"] = fingerprint
                definition["approval_status"] = (
                    "pending" if source.scope in {ConfigScope.LEGACY, ConfigScope.PROJECT} else "automatic"
                )
    return prepared


_NAMED_REPLACE_MAPS = {("agent", "models"), ("mcp_servers",)}
_MERGED_LISTS = {
    ("permissions", "always_allow"),
    ("permissions", "always_deny"),
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any], path: tuple[str, ...] = ()) -> None:
    for key, value in overlay.items():
        field_path = (*path, key)
        if field_path in _NAMED_REPLACE_MAPS and isinstance(value, dict):
            value_map = cast(dict[str, Any], value)
            target = _mapping(base.get(key))
            if target is None:
                target = {}
                base[key] = target
            for name, definition in value_map.items():
                if definition is None:
                    target.pop(name, None)
                else:
                    # Same-name definitions replace as a unit; they are not
                    # recursively merged with a lower-scope server/model.
                    target[name] = copy.deepcopy(definition)
            continue
        if field_path == ("tools", "plugins") and isinstance(value, list):
            current = _list(base.get(key)) or []
            value_list = cast(list[Any], value)
            combined: list[Any] = [*current, *copy.deepcopy(value_list)]
            positions: dict[tuple[Any, Any], int] = {}
            deduped: list[Any] = []
            for plugin in combined:
                plugin_map = _mapping(plugin)
                identity = (
                    (plugin_map.get("module"), plugin_map.get("factory", "create_tools"))
                    if plugin_map is not None
                    else (repr(plugin), None)
                )
                if identity not in positions:
                    positions[identity] = len(deduped)
                    deduped.append(plugin)
                else:
                    # Retain the original ordering while making the
                    # higher-scope declaration (and its source directory)
                    # authoritative.
                    deduped[positions[identity]] = plugin
            base[key] = deduped
            continue
        if field_path in _MERGED_LISTS and isinstance(value, list):
            current = _list(base.get(key)) or []
            value_list = cast(list[Any], value)
            base[key] = _dedupe([*current, *copy.deepcopy(value_list)])
            continue
        nested_base = _mapping(base.get(key))
        if isinstance(value, dict) and nested_base is not None:
            _deep_merge(nested_base, cast(dict[str, Any], value), field_path)
        else:
            base[key] = copy.deepcopy(cast(Any, value))


def _merge_layers(layers: list[tuple[ConfigSource, dict[str, Any]]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for _source, layer in layers:
        agent = _mapping(layer.get("agent"))
        if agent is not None:
            current_agent = _mapping(merged.get("agent"))
            if current_agent is None:
                current_agent = {}
                merged["agent"] = current_agent
            if "model" in agent:
                current_agent.pop("models", None)
                current_agent.pop("default_model", None)
            if "models" in agent:
                current_agent.pop("model", None)
        _deep_merge(merged, layer)

    permissions = _mapping(merged.get("permissions"))
    if permissions is not None:
        denied = set(_list(permissions.get("always_deny")) or [])
        permissions["always_allow"] = [
            item for item in (_list(permissions.get("always_allow")) or []) if item not in denied
        ]
    return merged


class ConfigResolver:
    """Discover and merge Lumen configuration without executing resources."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        explicit_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        home: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.environ = dict(os.environ if environ is None else environ)
        self.home = Path.home() if home is None else Path(home).expanduser()
        selected = explicit_path if explicit_path is not None else self.environ.get("LUMEN_CONFIG")
        self.explicit_path = Path(selected).expanduser().resolve() if selected is not None else None

    @property
    def exclusive(self) -> bool:
        return self.explicit_path is not None

    def candidate_sources(self) -> tuple[ConfigSource, ...]:
        if self.explicit_path is not None:
            return (ConfigSource(ConfigScope.EXPLICIT, self.explicit_path),)
        return (
            ConfigSource(ConfigScope.USER, (self.home / ".lumen" / "agent.yaml").resolve()),
            ConfigSource(ConfigScope.LEGACY, self.workspace / "agent.yaml"),
            ConfigSource(ConfigScope.PROJECT, self.workspace / ".lumen" / "agent.yaml"),
            ConfigSource(ConfigScope.LOCAL, self.workspace / ".lumen" / "agent.local.yaml"),
            ConfigSource(ConfigScope.MANAGED, self.workspace / ".lumen" / "agent.web.yaml"),
        )

    def discovered_sources(self) -> tuple[ConfigSource, ...]:
        return tuple(source for source in self.candidate_sources() if source.path.is_file())

    def project_sources(self) -> tuple[ConfigSource, ...]:
        if self.exclusive:
            return ()
        return tuple(
            source
            for source in self.discovered_sources()
            if source.scope
            in {ConfigScope.LEGACY, ConfigScope.PROJECT, ConfigScope.LOCAL, ConfigScope.MANAGED}
        )

    def has_project_skills(self) -> bool:
        return not self.exclusive and (self.workspace / ".lumen" / "skills").is_dir()

    def resolve(
        self,
        *,
        project_trusted: bool = True,
        source_overrides: Mapping[Path, Mapping[str, Any]] | None = None,
    ) -> ConfigResolution:
        overrides = {
            Path(path).expanduser().resolve(): copy.deepcopy(dict(value))
            for path, value in (source_overrides or {}).items()
        }
        candidates = self.candidate_sources()
        discovered = tuple(
            source for source in candidates if source.path.is_file() or source.path in overrides
        )
        if self.explicit_path is not None and not discovered:
            raise ConfigLoadError(f"cannot read configuration {self.explicit_path}: file does not exist")
        if not discovered:
            searched = "\n".join(f"  - {source.path}" for source in candidates)
            raise ConfigLoadError(
                "no Lumen configuration found; searched:\n"
                f"{searched}\n"
                "Run 'lumen init --global' for a user configuration or 'lumen init' for this project."
            )
        if not project_trusted and self.project_sources():
            files = ", ".join(str(source.path) for source in self.project_sources())
            raise ConfigLoadError(f"project is not trusted; refusing to read: {files}")

        layers: list[tuple[ConfigSource, dict[str, Any]]] = []
        provenance: dict[str, ConfigSource] = {}
        warnings: list[str] = []
        for source in discovered:
            if source.path in overrides:
                loaded: object = overrides[source.path]
            else:
                try:
                    loaded = yaml.safe_load(source.path.read_text(encoding="utf-8"))
                except OSError as error:
                    raise ConfigLoadError(
                        f"cannot read {source.scope.value} configuration {source.path}: {error}"
                    ) from error
                except yaml.YAMLError as error:
                    raise ConfigLoadError(
                        f"invalid YAML in {source.scope.value} configuration {source.path}: {error}"
                    ) from error
            if not isinstance(loaded, dict):
                raise ConfigLoadError(
                    f"{source.scope.value} configuration root must be a mapping: {source.path}"
                )
            raw = cast(dict[str, Any], loaded)
            if source.scope is ConfigScope.LEGACY:
                warnings.append(
                    f"Legacy configuration {source.path} is deprecated; migrate it to "
                    f"{self.workspace / '.lumen' / 'agent.yaml'}."
                )
            prepared = _prepare_layer(raw, source)
            layers.append((source, prepared))
            _record_provenance(provenance, prepared, source)

        merged = _merge_layers(layers)
        sessions = _mapping(merged.get("sessions"))
        if sessions is None:
            sessions = {}
            merged["sessions"] = sessions
        if "directory" not in sessions:
            sessions["directory"] = self.workspace / ".lumen" / "sessions"

        primary = discovered[-1].path
        try:
            config = validate_config_data(
                merged,
                config_path=primary,
                workspace=self.workspace,
                config_sources=discovered,
                config_warnings=tuple(warnings),
                project_trusted=project_trusted,
            )
        except ConfigLoadError as error:
            source_text = ", ".join(f"{source.scope.value}={source.path}" for source in discovered)
            raise ConfigLoadError(f"invalid merged configuration ({source_text}):\n{error}") from error
        return ConfigResolution(
            config=config,
            sources=discovered,
            searched_paths=tuple(source.path for source in candidates),
            warnings=tuple(warnings),
            exclusive=self.exclusive,
            provenance=provenance,
        )


__all__ = [
    "ConfigResolution",
    "ConfigResolver",
    "ConfigScope",
    "ConfigSource",
    "ResolvedConfigReport",
]
