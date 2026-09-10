"""Structured, approval-aware Git capabilities owned by the Host."""

# Model-facing Chinese prose is kept as authored for readability.
# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import Field

from lumen.config import SandboxConfig
from lumen.sandbox import SandboxRunner
from lumen.tools.capability import run_prepared_command
from lumen.tools.spec import EffectKind, Risk, ToolConcurrency, ToolSpec
from lumen.tools.workspace import Workspace

_REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SCP_REMOTE = re.compile(
    r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:[A-Za-z0-9._~/-]+\Z"
)
_PROTECTED_PARTS = frozenset({".git", ".lumen"})
_MAX_PATHS = 256
_MAX_MESSAGE_CHARS = 10_000


class GitCapabilityError(RuntimeError):
    """A structured Git precondition or command failed."""


class GitWorkspace:
    """Deep implementation behind the small model-facing Git Interface.

    Git subprocesses receive a narrowly scoped exception to the normal
    ``.git`` write protection. They remain inside the workspace OS sandbox;
    only ``push`` receives network access, and repository hooks are disabled.
    """

    def __init__(self, root: str | Path, *, config: SandboxConfig, max_timeout: float) -> None:
        self.workspace = Workspace(root)
        self.config = config
        self.max_timeout = max_timeout

    async def status(self) -> dict[str, Any]:
        await self._require_repository()
        status = await self._git(["status", "--porcelain=v1", "--branch"], optional_locks=False)
        head = await self._head()
        branch_result = await self._git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            optional_locks=False,
            allowed_exit_codes={0, 1},
        )
        index_fingerprint = await self._index_fingerprint()
        remotes: list[dict[str, str]] = []
        names = await self._git(["remote"], optional_locks=False)
        for name in sorted(filter(None, str(names["stdout"]).splitlines())):
            remote_url = await self._remote_url(name)
            remotes.append(
                {
                    "name": name,
                    "url": _redact_remote_url(remote_url),
                    "fingerprint": _fingerprint(remote_url),
                }
            )
        return {
            "branch": str(branch_result["stdout"]).strip() or None,
            "head": head,
            "index_fingerprint": index_fingerprint,
            "porcelain": str(status["stdout"]),
            "remotes": remotes,
        }

    async def diff(self, *, staged: bool, paths: list[str] | None) -> dict[str, Any]:
        await self._require_repository()
        normalized = self._paths(paths or []) if paths else []
        args = ["diff", "--no-ext-diff", "--no-textconv"]
        if staged:
            args.append("--cached")
        if normalized:
            args.extend(["--", *normalized])
        result = await self._git(args, optional_locks=False)
        return {
            "staged": staged,
            "paths": normalized,
            "diff": str(result["stdout"]),
            "truncated": bool(result["stdout_truncated"]),
            "total_bytes": int(result["stdout_total_bytes"]),
        }

    async def stage(self, *, paths: list[str], expected_head: str | None) -> dict[str, Any]:
        await self._require_repository()
        normalized = self._paths(paths)
        await self._require_head(expected_head)
        for path in normalized:
            await self._stage_without_filters(path)
        return {
            "staged": normalized,
            "head": await self._head(),
            "index_fingerprint": await self._index_fingerprint(),
        }

    async def commit(
        self,
        *,
        message: str,
        expected_head: str | None,
        expected_index_fingerprint: str,
    ) -> dict[str, Any]:
        await self._require_repository()
        normalized_message = message.strip()
        if not normalized_message:
            raise ValueError("commit message must not be empty")
        if len(normalized_message) > _MAX_MESSAGE_CHARS or "\x00" in normalized_message:
            raise ValueError(f"commit message must be at most {_MAX_MESSAGE_CHARS} characters without NUL")
        await self._require_head(expected_head)
        actual_index = await self._index_fingerprint()
        if actual_index != expected_index_fingerprint:
            raise GitCapabilityError(
                "staged changes changed after review: expected index fingerprint "
                f"{expected_index_fingerprint}, found {actual_index}"
            )
        tree_result = await self._git(
            ["write-tree"],
            allow_protected_writes=True,
            optional_locks=True,
        )
        tree = str(tree_result["stdout"]).strip()
        await self._require_index_fingerprint(expected_index_fingerprint)
        if expected_head is None:
            empty_result = await self._git(
                ["mktree"],
                allow_protected_writes=True,
                optional_locks=True,
                stdin_data=b"",
            )
            parent_tree = str(empty_result["stdout"]).strip()
            object_format = await self._git(
                ["rev-parse", "--show-object-format"], optional_locks=False
            )
            zero_oid = "0" * (64 if str(object_format["stdout"]).strip() == "sha256" else 40)
            parent_args: list[str] = []
        else:
            parent_result = await self._git(
                ["rev-parse", f"{expected_head}^{{tree}}"], optional_locks=False
            )
            parent_tree = str(parent_result["stdout"]).strip()
            zero_oid = expected_head
            parent_args = ["-p", expected_head]
        if tree == parent_tree:
            raise GitCapabilityError("nothing to commit: staged tree matches HEAD")
        commit_result = await self._git(
            ["commit-tree", tree, *parent_args],
            allow_protected_writes=True,
            optional_locks=True,
            stdin_data=(normalized_message + "\n").encode("utf-8"),
        )
        commit_id = str(commit_result["stdout"]).strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_id):
            raise GitCapabilityError("git commit-tree returned an invalid object id")
        subject = normalized_message.splitlines()[0]
        await self._git(
            ["update-ref", "-m", f"commit: {subject}", "HEAD", commit_id, zero_oid],
            allow_protected_writes=True,
            optional_locks=True,
        )
        branch_result = await self._git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            optional_locks=False,
            allowed_exit_codes={0, 1},
        )
        branch = str(branch_result["stdout"]).strip() or "detached HEAD"
        return {
            "head": commit_id,
            "message": normalized_message,
            "summary": f"[{branch} {commit_id[:12]}] {subject}",
        }

    async def push(
        self,
        *,
        remote: str,
        branch: str,
        expected_head: str,
        expected_remote_url: str,
        expected_remote_fingerprint: str,
    ) -> dict[str, Any]:
        await self._require_repository()
        if not _REMOTE_NAME.fullmatch(remote):
            raise ValueError("remote must be a configured Git remote name")
        await self._validate_branch(branch)
        await self._require_head(expected_head)
        remote_url = await self._remote_url(remote)
        _require_network_remote(remote_url)
        redacted_remote_url = _redact_remote_url(remote_url)
        if redacted_remote_url != expected_remote_url:
            raise GitCapabilityError(
                "remote URL changed after review: expected "
                f"{expected_remote_url}, found {redacted_remote_url}"
            )
        actual_fingerprint = _fingerprint(remote_url)
        if actual_fingerprint != expected_remote_fingerprint:
            raise GitCapabilityError(
                "remote changed after review: expected fingerprint "
                f"{expected_remote_fingerprint}, found {actual_fingerprint}"
            )
        result = await self._git(
            [
                "push",
                "--porcelain",
                "--no-verify",
                "--",
                remote,
                f"{expected_head}:refs/heads/{branch}",
            ],
            allow_protected_writes=True,
            network=True,
            optional_locks=True,
        )
        return {
            "head": expected_head,
            "remote": remote,
            "remote_url": redacted_remote_url,
            "branch": branch,
            "summary": "\n".join(
                item for item in (str(result["stdout"]).strip(), str(result["stderr"]).strip()) if item
            ),
        }

    async def _require_repository(self) -> None:
        git_control = self.workspace.root / ".git"
        if git_control.is_symlink():
            raise GitCapabilityError("workspace .git control path must not be a symbolic link")
        result = await self._git(
            ["rev-parse", "--show-toplevel"],
            optional_locks=False,
            require_repository=False,
        )
        root = await asyncio.to_thread(Path(str(result["stdout"]).strip()).resolve)
        if root != self.workspace.root:
            raise GitCapabilityError(
                f"Git repository root must equal the Lumen workspace: {root} != {self.workspace.root}"
            )

    async def _head(self) -> str | None:
        result = await self._git(
            ["rev-parse", "--verify", "HEAD"],
            optional_locks=False,
            allowed_exit_codes={0, 128},
        )
        return str(result["stdout"]).strip() or None

    async def _require_head(self, expected: str | None) -> None:
        actual = await self._head()
        if actual != expected:
            raise GitCapabilityError(f"HEAD changed after review: expected {expected}, found {actual}")

    async def _index_fingerprint(self) -> str:
        result = await self._git(
            ["diff", "--cached", "--raw", "--no-abbrev", "--no-renames", "--no-ext-diff"],
            optional_locks=False,
        )
        if result["stdout_truncated"]:
            raise GitCapabilityError("staged change list exceeds the bounded Git output limit")
        return _fingerprint(str(result["stdout"]))

    async def _require_index_fingerprint(self, expected: str) -> None:
        actual = await self._index_fingerprint()
        if actual != expected:
            raise GitCapabilityError(
                "staged changes changed during commit: expected index fingerprint "
                f"{expected}, found {actual}"
            )

    async def _remote_url(self, remote: str) -> str:
        result = await self._git(["remote", "get-url", "--push", remote], optional_locks=False)
        value = str(result["stdout"]).strip()
        if not value:
            raise GitCapabilityError(f"remote {remote!r} has no push URL")
        return value

    async def _validate_branch(self, branch: str) -> None:
        if not branch or branch.startswith("-") or len(branch) > 255:
            raise ValueError("branch must be a non-option Git branch name")
        await self._git(["check-ref-format", "--branch", branch], optional_locks=False)

    async def _stage_without_filters(self, path: str) -> None:
        """Update one index entry without executing repository content filters.

        ``git add`` may execute an arbitrary ``filter.<name>.clean`` or
        long-running filter process selected by repository attributes. The
        structured Interface instead writes an unfiltered blob and updates the
        index directly. Directories and symbolic links deliberately fail closed
        until their semantics have an equally narrow implementation.
        """

        target = self.workspace.root.joinpath(*PurePosixPath(path).parts)
        if target.is_symlink():
            raise GitCapabilityError(f"git_stage does not accept symbolic links: {path}")
        if not target.exists():
            await self._git(
                ["update-index", "--force-remove", "--", path],
                allow_protected_writes=True,
                optional_locks=True,
            )
            return
        if not target.is_file():
            raise GitCapabilityError(f"git_stage requires explicit regular-file paths: {path}")
        hashed = await self._git(
            ["hash-object", "-w", "--no-filters", "--", path],
            allow_protected_writes=True,
            optional_locks=True,
        )
        object_id = str(hashed["stdout"]).strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", object_id):
            raise GitCapabilityError("git hash-object returned an invalid object id")
        mode = "100755" if target.stat().st_mode & 0o111 else "100644"
        await self._git(
            ["update-index", "--add", "--cacheinfo", f"{mode},{object_id},{path}"],
            allow_protected_writes=True,
            optional_locks=True,
        )

    def _paths(self, paths: list[str]) -> list[str]:
        if not paths or len(paths) > _MAX_PATHS:
            raise ValueError(f"paths must contain between 1 and {_MAX_PATHS} entries")
        normalized: list[str] = []
        for raw in paths:
            path = PurePosixPath(raw.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"Git path must stay inside the workspace: {raw}")
            if any(part in _PROTECTED_PARTS for part in path.parts):
                raise ValueError(f"Git path targets protected control state: {raw}")
            value = path.as_posix()
            self.workspace.resolve(value)
            if value not in normalized:
                normalized.append(value)
        return normalized

    async def _git(
        self,
        args: list[str],
        *,
        allow_protected_writes: bool = False,
        network: bool = False,
        optional_locks: bool,
        allowed_exit_codes: set[int] | None = None,
        require_repository: bool = True,
        stdin_data: bytes | None = None,
    ) -> dict[str, Any]:
        if require_repository and not (self.workspace.root / ".git").exists():
            raise GitCapabilityError("workspace is not a Git repository")
        sandbox_config = self.config.model_copy(update={"network": network})
        sandbox = SandboxRunner(self.workspace.root, sandbox_config)
        read_paths, environment = _git_environment(optional_locks=optional_locks)
        argv = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "gc.auto=0",
            "-c", "maintenance.auto=false",
            "-c", "credential.helper=",
            "-c", "core.askPass=/usr/bin/false",
            "-c", "http.proxy=",
            "-c", "http.followRedirects=false",
            "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always",
            "-c", "protocol.ssh.allow=always",
            *args,
        ]
        result = await run_prepared_command(
            sandbox.prepare(
                argv,
                cwd=self.workspace.root,
                overrides=environment,
                read_paths=read_paths,
                writable_protected_paths=(
                    (self.workspace.root / ".git",) if allow_protected_writes else ()
                ),
                workspace_writable=False,
            ),
            argv=argv,
            resolved_cwd=self.workspace.root,
            cwd=".",
            timeout_seconds=self.max_timeout,
            sandbox_config=sandbox_config,
            stdin_data=stdin_data,
        )
        accepted = allowed_exit_codes or {0}
        if int(result["exit_code"]) not in accepted:
            detail = str(result["stderr"]).strip() or str(result["stdout"]).strip()
            raise GitCapabilityError(detail or f"git {' '.join(args)} failed")
        if result["stderr_truncated"]:
            raise GitCapabilityError("Git stderr exceeds the bounded output limit")
        return result


def build_git_specs(
    root: str | Path,
    *,
    max_timeout: float,
    sandbox_config: SandboxConfig | None = None,
) -> list[ToolSpec]:
    git = GitWorkspace(root, config=sandbox_config or SandboxConfig(), max_timeout=max_timeout)

    async def git_status() -> dict[str, Any]:
        """读取当前分支、HEAD、工作区状态、暂存指纹和脱敏 remote 信息。"""

        return await git.status()

    async def git_diff(
        staged: Annotated[bool, Field(description="读取已暂存 diff；false 表示未暂存 diff。")] = False,
        paths: Annotated[list[str] | None, Field(description="可选的工作区相对路径过滤。")] = None,
    ) -> dict[str, Any]:
        """读取 Git diff；禁用 external diff/textconv，不执行仓库 Hook。"""

        return await git.diff(staged=staged, paths=paths)

    async def git_stage(
        paths: Annotated[list[str], Field(description="明确要暂存的工作区相对路径。")],
        expected_head: Annotated[str | None, Field(description="git_status 返回的 HEAD；初始仓库为 null。")],
    ) -> dict[str, Any]:
        """仅暂存明确列出的路径；HEAD 变化时安全失败。"""

        return await git.stage(paths=paths, expected_head=expected_head)

    async def git_commit(
        message: Annotated[str, Field(description="提交消息。", min_length=1, max_length=_MAX_MESSAGE_CHARS)],
        expected_head: Annotated[str | None, Field(description="git_status 返回的 HEAD；初始仓库为 null。")],
        expected_index_fingerprint: Annotated[
            str, Field(description="git_stage/git_status 返回的暂存区指纹。")
        ],
    ) -> dict[str, Any]:
        """提交已审查的暂存区；始终需要显式审批并禁用仓库 Hook。"""

        return await git.commit(
            message=message,
            expected_head=expected_head,
            expected_index_fingerprint=expected_index_fingerprint,
        )

    async def git_push(
        remote: Annotated[str, Field(description="已配置的 remote 名称，例如 origin。")],
        branch: Annotated[str, Field(description="目标远端分支名。")],
        expected_head: Annotated[str, Field(description="要推送的完整 HEAD SHA。")],
        expected_remote_url: Annotated[
            str, Field(description="git_status 返回的脱敏 remote URL。")
        ],
        expected_remote_fingerprint: Annotated[
            str, Field(description="git_status 返回的 remote 指纹，避免审批后目标变化。")
        ],
    ) -> dict[str, Any]:
        """把指定 HEAD 推送到已审查 remote/branch；始终需要显式审批。"""

        return await git.push(
            remote=remote,
            branch=branch,
            expected_head=expected_head,
            expected_remote_url=expected_remote_url,
            expected_remote_fingerprint=expected_remote_fingerprint,
        )

    return [
        ToolSpec(
            git_status,
            risk=Risk.READ,
            effect_kind=EffectKind.OBSERVE,
            concurrency=lambda _args: ToolConcurrency.PARALLEL_SAFE,
        ),
        ToolSpec(
            git_diff,
            risk=Risk.READ,
            effect_kind=EffectKind.OBSERVE,
            concurrency=lambda _args: ToolConcurrency.PARALLEL_SAFE,
        ),
        ToolSpec(git_stage, risk=Risk.WRITE, effect_kind=EffectKind.MUTATION),
        ToolSpec(git_commit, risk=Risk.CONFIRM, effect_kind=EffectKind.MUTATION),
        ToolSpec(git_push, risk=Risk.CONFIRM, effect_kind=EffectKind.EXTERNAL_ACTION),
    ]


def _git_environment(*, optional_locks: bool) -> tuple[tuple[Path, ...], dict[str, str]]:
    environment = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "1" if optional_locks else "0",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true",
    }
    read_paths: list[Path] = []
    user_home = Path.home()
    global_configs = (user_home / ".gitconfig", user_home / ".config" / "git" / "config")
    for candidate in global_configs:
        if candidate.is_file():
            environment["GIT_CONFIG_GLOBAL"] = str(candidate.resolve())
            read_paths.append(candidate.resolve())
            break
    known_hosts = user_home / ".ssh" / "known_hosts"
    if known_hosts.is_file():
        environment["GIT_SSH_COMMAND"] = (
            "ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={shlex.quote(str(known_hosts.resolve()))}"
        )
        read_paths.append(known_hosts.resolve())
    if socket_path := os.environ.get("SSH_AUTH_SOCK"):
        environment["SSH_AUTH_SOCK"] = socket_path
        read_paths.append(Path(socket_path).resolve())
    return tuple(read_paths), environment


def _fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_remote_url(value: str) -> str:
    if _SCP_REMOTE.fullmatch(value):
        return value
    if "://" in value:
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port is not None else ""
            username = f"{parsed.username}@" if parsed.username and parsed.scheme == "ssh" else ""
            safe = SplitResult(parsed.scheme, f"{username}{hostname}{port}", parsed.path, "", "")
            return urlunsplit(safe)
        except ValueError:
            pass
    return f"unsupported:{_fingerprint(value)}"


def _require_network_remote(value: str) -> None:
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if parsed.hostname and not parsed.username and not parsed.query and not parsed.fragment:
            return
    elif value.startswith("ssh://"):
        parsed = urlsplit(value)
        if parsed.hostname and not parsed.password and not parsed.query and not parsed.fragment:
            return
    elif _SCP_REMOTE.fullmatch(value):
        return
    raise GitCapabilityError("git_push permits only HTTPS or SSH remotes without embedded passwords")


__all__ = ["GitCapabilityError", "GitWorkspace", "build_git_specs"]
