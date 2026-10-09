from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from lumen.config import AppConfig


def git_common_directory(workspace: str | Path) -> Path | None:
    """Return Git's common directory without invoking Git or trusting hooks."""

    root = Path(workspace).expanduser().resolve()
    for candidate in (root, *root.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return marker.resolve()
        if not marker.is_file():
            continue
        try:
            line = marker.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir:"):
                continue
            git_dir = Path(line.removeprefix("gitdir:").strip())
            if not git_dir.is_absolute():
                git_dir = marker.parent / git_dir
            git_dir = git_dir.resolve()
            common_file = git_dir / "commondir"
            if common_file.is_file():
                common = Path(common_file.read_text(encoding="utf-8").strip())
                if not common.is_absolute():
                    common = git_dir / common
                return common.resolve()
            return git_dir
        except OSError:
            continue
    return None


def canonical_project_identity(workspace: str | Path) -> str:
    workspace_path = Path(workspace).expanduser().resolve()
    authority = git_common_directory(workspace_path) or workspace_path
    digest = hashlib.sha256(str(authority).encode("utf-8")).hexdigest()[:24]
    return f"repo-{digest}"


def _read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else fallback


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class TrustStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path).expanduser() if path is not None else Path.home() / ".lumen" / "state" / "trust.json"
        )

    def _data(self) -> dict[str, Any]:
        value = _read_json(self.path, {"version": 1, "projects": {}})
        if not isinstance(value.get("projects"), dict):
            value["projects"] = {}
        value["version"] = 1
        return value

    def is_trusted(self, workspace: str | Path) -> bool:
        project_id = canonical_project_identity(workspace)
        return project_id in self._data()["projects"]

    def trust(self, workspace: str | Path) -> str:
        resolved = Path(workspace).expanduser().resolve()
        project_id = canonical_project_identity(resolved)
        data = self._data()
        data["projects"][project_id] = {
            "path": str(resolved),
            "trusted_at": datetime.now(UTC).isoformat(),
        }
        _atomic_write_json(self.path, data)
        return project_id

    def revoke(self, workspace: str | Path) -> bool:
        project_id = canonical_project_identity(workspace)
        data = self._data()
        removed = data["projects"].pop(project_id, None) is not None
        if removed:
            _atomic_write_json(self.path, data)
        return removed


McpDecision = Literal["always", "deny"]


class McpApprovalStore:
    def __init__(
        self,
        workspace: str | Path,
        *,
        state_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.project_id = canonical_project_identity(self.workspace)
        root = Path(state_root).expanduser() if state_root is not None else Path.home() / ".lumen" / "state"
        self.path = root / "mcp-approvals" / f"{self.project_id}.json"

    def _data(self) -> dict[str, Any]:
        data = _read_json(
            self.path,
            {"version": 1, "project_id": self.project_id, "servers": {}},
        )
        if not isinstance(data.get("servers"), dict):
            data["servers"] = {}
        data.update({"version": 1, "project_id": self.project_id})
        return data

    def decision(self, name: str, fingerprint: str) -> McpDecision | None:
        servers = cast(dict[str, Any], self._data()["servers"])
        record_value = servers.get(name)
        record = cast(dict[str, Any], record_value) if isinstance(record_value, dict) else None
        if record is None or record.get("fingerprint") != fingerprint:
            return None
        decision = record.get("decision")
        return decision if decision in {"always", "deny"} else None

    def set(self, name: str, fingerprint: str, decision: McpDecision) -> None:
        data = self._data()
        data["servers"][name] = {
            "decision": decision,
            "fingerprint": fingerprint,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        _atomic_write_json(self.path, data)

    def reset(self, name: str | None = None) -> bool:
        if name is None:
            if not self.path.exists():
                return False
            self.path.unlink()
            return True
        data = self._data()
        removed = data["servers"].pop(name, None) is not None
        if removed:
            _atomic_write_json(self.path, data)
        return removed


class ApprovalRuleStore:
    """Cross-session "always allow" approval rules, scoped to one project.

    Rules are keyed by the same bounded capability string the host derives for
    session rules (``origin:tool[:sha256:invocation_digest]``). Stored separately from user
    config because these files are machine-managed: the config YAML may hold
    credentials and hand-written comments that must never be rewritten.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        state_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.project_id = canonical_project_identity(self.workspace)
        root = Path(state_root).expanduser() if state_root is not None else Path.home() / ".lumen" / "state"
        self.path = root / "approval-rules" / f"{self.project_id}.json"

    def _data(self) -> dict[str, Any]:
        data = _read_json(
            self.path,
            {"version": 1, "project_id": self.project_id, "allow": {}},
        )
        if not isinstance(data.get("allow"), dict):
            data["allow"] = {}
        data.update({"version": 1, "project_id": self.project_id})
        return data

    def allowed_keys(self) -> set[str]:
        return {str(key) for key in self._data()["allow"]}

    def allow(self, key: str) -> None:
        data = self._data()
        data["allow"][key] = {"updated_at": datetime.now(UTC).isoformat()}
        _atomic_write_json(self.path, data)

    def forget(self, key: str | None = None) -> bool:
        data = self._data()
        if key is None:
            if not self.path.exists():
                return False
            self.path.unlink()
            return True
        removed = data["allow"].pop(key, None) is not None
        if removed:
            _atomic_write_json(self.path, data)
        return removed


__all__ = [
    "ApprovalRuleStore",
    "McpApprovalStore",
    "TrustStore",
    "canonical_project_identity",
    "git_common_directory",
]


def apply_mcp_approvals(
    config: AppConfig,
    workspace: Path,
    *,
    interactive: bool,
    report_only: bool = False,
    choices: Mapping[str, Literal["once", "always", "deny"]] | None = None,
) -> AppConfig:
    import typer

    from lumen.config import ConfigLoadError
    from lumen.config_resolver import ConfigScope

    store = McpApprovalStore(workspace)
    accepted = {}
    warnings = list(config.config_warnings)
    diagnostics: list[dict[str, str]] = []
    for name, server in config.mcp_servers.items():
        if not config.mcp.enabled.get(name, True):
            accepted[name] = server.model_copy(update={"approval_status": "disabled"})
            diagnostics.append(
                {"name": name, "scope": server.source_scope, "approval": "disabled"}
            )
            continue
        if server.source_scope not in {ConfigScope.LEGACY.value, ConfigScope.PROJECT.value}:
            accepted[name] = server.model_copy(update={"approval_status": "automatic"})
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "automatic"})
            continue
        fingerprint = server.definition_fingerprint or ""
        decision = store.decision(name, fingerprint)
        if decision == "always":
            accepted[name] = server.model_copy(update={"approval_status": "always"})
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "always"})
            continue
        if decision == "deny":
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "deny"})
            warnings.append(
                f"MCP server {name!r} from {server.source_scope} scope is denied and was not started."
            )
            continue
        if report_only:
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "pending"})
            warnings.append(
                f"MCP server {name!r} from {server.source_scope} scope is pending approval; "
                f"run 'lumen mcp approve {name}'."
            )
            continue
        selected = choices.get(name) if choices is not None else None
        if not interactive and selected is None:
            raise ConfigLoadError(
                f"MCP server {name!r} from {server.source_scope} scope requires approval. "
                f"Run 'lumen mcp approve {name}' or 'lumen mcp deny {name}'."
            )
        choice = selected or (
            typer.prompt(
                f"Allow project MCP server {name!r} ({server.transport})? [once/always/deny]",
                default="once",
            )
            .strip()
            .lower()
        )
        if choice not in {"once", "always", "deny"}:
            raise ConfigLoadError("MCP approval must be one of: once, always, deny")
        if choice == "deny":
            store.set(name, fingerprint, "deny")
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "deny"})
            warnings.append(f"MCP server {name!r} was denied and was not started.")
            continue
        if choice == "always":
            store.set(name, fingerprint, "always")
        accepted[name] = server.model_copy(update={"approval_status": choice})
        diagnostics.append({"name": name, "scope": server.source_scope, "approval": choice})
    return config.model_copy(
        update={
            "mcp_servers": accepted,
            "config_warnings": tuple(warnings),
            "mcp_diagnostics": tuple(diagnostics),
        }
    )
