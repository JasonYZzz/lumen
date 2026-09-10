"""Install complete Skill directories without routing file bytes through the model."""

from __future__ import annotations

import asyncio
import io
import json
import re
import stat
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, unquote, urlparse

import httpx

from lumen.skills import Skill, load_skill
from lumen.tools.spec import EffectKind, Risk, ToolSpec
from lumen.tools.web import is_public_host, validate_public_url
from lumen.tools.workspace import Workspace
from lumen.work_products import TaskWorkspace, WorkProductKind
from lumen.work_products.directories import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    USER_SKILLS_PREFIX,
    BundleFile,
    DirectoryBundle,
    DirectoryResourceAdapter,
    bundle_path,
)

_METADATA = ".lumen-install.json"
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HOSTS = {"api.github.com", "codeload.github.com"}


class SkillInstaller:
    def __init__(
        self,
        workspace: Path,
        task_workspace: TaskWorkspace,
        *,
        project_trusted: bool,
        user_skills: Path | None = None,
        refresh: Callable[[], None],
        token: Callable[[], str | None] = lambda: None,
        transport: httpx.AsyncBaseTransport | None = None,
        host_guard: Callable[[str], bool] = is_public_host,
    ) -> None:
        self.workspace = Workspace(workspace)
        self.task_workspace = task_workspace
        self.project_trusted = project_trusted
        self.user_skills = user_skills
        self.refresh = refresh
        self.token = token
        self.transport = transport
        self.host_guard = host_guard

    def spec(self) -> ToolSpec:
        scopes = ["project"] if self.project_trusted else []
        if self.user_skills is not None:
            scopes.append("user")
        return ToolSpec(
            self.install_skill,
            description=(self.install_skill.__doc__ or "") + "\n本轮可用安装范围: "
            + ", ".join(scopes) + "。遵循用户明确指定的 user/global 范围, 不得静默更改。",
            risk=Risk.EXTERNAL,
            effect_kind=EffectKind.MUTATION,
            timeout=120,
        )

    async def install_skill(
        self,
        source: str,
        path: str | None = None,
        ref: str | None = None,
        name: str | None = None,
        scope: Literal["project", "user"] = "project",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """从 GitHub URL、owner/repo 或工作区目录安装完整 Skill。

        安装 Skill 时优先使用此工具。它负责发现、二进制/文本传输、校验、目录原子发布和
        即时目录刷新, 且不会执行下载的脚本。GitHub 仓库根路径使用真实默认分支;
        支持 tree/blob/raw URL 以及显式 path/ref。分支名含斜杠时使用 ref。
        如果匹配到多个 Skill, 返回 selection_required 和候选项且不写入; 使用精确候选路径
        再次调用。project 范围安装到 .lumen/skills; user 范围要求父权限允许
        ~/.lumen/skills。内容完全相同时不执行操作。只有用户明确要求更新或替换时才设置
        overwrite=True; 不得静默覆盖本地已修改的托管安装。私有仓库使用操作者环境中的
        GH_TOKEN/GITHUB_TOKEN, 不在工具参数中接收凭据。安装后可立即加载 Skill。
        """
        if not self.task_workspace.enabled:
            raise ValueError("skill installation requires TaskWorkspace verification to be enabled")
        if scope == "project" and not self.project_trusted:
            raise ValueError("project skill installation requires a trusted project")
        if scope == "user" and self.user_skills is None:
            return {
                "status": "scope_unavailable",
                "requested_scope": "user",
                "available_scopes": ["project"] if self.project_trusted else [],
                "message": "全局 Skill 安装未启用, 未写入任何文件。操作者可以启用 "
                "agent.user_skill_install_enabled, 无需关闭命令 Sandbox。若用户要求全局范围, "
                "不得静默改为安装到项目; 如必须选择其他范围, 最多提出一个简洁问题。",
            }
        if name is not None and (len(name) > 64 or not _NAME.fullmatch(name)):
            raise ValueError("name must be a lowercase kebab-case skill directory name")
        if path is not None and path != ".":
            bundle_path(path)

        async with asyncio.timeout(110):
            source_id, commit, selected, tree = await self._source(source, path, ref)
        candidates = await asyncio.to_thread(
            self._candidates, tree, selected, name or source_id.rsplit("/", 1)[-1]
        )
        if len(candidates) != 1:
            if not candidates:
                raise ValueError("no valid SKILL.md found at the selected source path")
            return {
                "status": "selection_required",
                "source": source_id,
                "ref": commit,
                "candidates": [
                    {"path": folder, "name": skill.name, "description": skill.description[:200]}
                    for folder, skill in candidates[:32]
                ],
                "candidate_count": len(candidates),
                "truncated": len(candidates) > 32,
                "message": "Choose an exact candidate path; no files were installed.",
            }
        folder, skill = candidates[0]
        prefix = folder + "/" if folder != "." else ""
        files = {
            file.removeprefix(prefix): value for file, value in tree.files.items() if file.startswith(prefix)
        }
        if _METADATA in files:
            raise ValueError("source contains reserved Lumen installation metadata")
        directories = [
            directory.removeprefix(prefix)
            for directory in tree.directories
            if directory.startswith(prefix) and directory != folder
        ]
        bundle = DirectoryBundle(files=files, directories=directories)
        bundle.validated_files()
        directory_name = name or skill.name
        resource = (
            f"{USER_SKILLS_PREFIX}{directory_name}" if scope == "user" else f".lumen/skills/{directory_name}"
        )

        def publish() -> dict[str, Any]:
            adapter = DirectoryResourceAdapter(
                self.workspace,
                self.task_workspace.artifacts,
                user_skills=self.user_skills,
            )
            existing = adapter.read(resource)
            source_revision = bundle.revision
            if existing is not None:
                local_files = dict(existing.files)
                metadata_file = local_files.pop(_METADATA, None)
                local_revision = DirectoryBundle(files=local_files, directories=existing.directories).revision
                if local_revision == source_revision:
                    return self._result("already_installed", skill, adapter.path(resource), source_id, commit)
                if not overwrite:
                    return {
                        "status": "conflict",
                        "path": str(adapter.path(resource)),
                        "message": "Destination exists; an authorized update requires overwrite=True.",
                    }
                if metadata_file is not None:
                    try:
                        metadata = json.loads(metadata_file.bytes())
                    except (ValueError, UnicodeError) as error:
                        raise ValueError(
                            "invalid install metadata; preserve the existing directory"
                        ) from error
                    if (
                        not isinstance(metadata, dict)
                        or cast(dict[str, Any], metadata).get("content_revision") != local_revision
                    ):
                        raise ValueError(
                            "installed skill has local changes; preserve or move them before updating"
                        )
            # SKILL.md's invocation name must not silently collide with another
            # directory in the same discovery scope.
            destination = adapter.path(resource)
            if destination.parent.exists():
                for sibling in destination.parent.iterdir():
                    if sibling == destination or sibling.is_symlink() or not sibling.is_dir():
                        continue
                    other = load_skill(sibling / "SKILL.md", scope)
                    if other is not None and other.name == skill.name:
                        raise ValueError(f"skill name {skill.name!r} already exists in {sibling.name!r}")
            files[_METADATA] = BundleFile.from_bytes(
                json.dumps(
                    {
                        "source": source_id,
                        "ref": commit,
                        "path": folder,
                        "content_revision": source_revision,
                    },
                    sort_keys=True,
                ).encode()
            )
            change = DirectoryBundle(files=files, directories=directories)
            product = self.task_workspace.open_work_product(resource, WorkProductKind.DIRECTORY.value)
            # Recheck the full revision after opening, including metadata. A
            # concurrent local edit must not be absorbed into a new baseline.
            expected = "missing" if existing is None else existing.revision
            if product["current"]["revision"] != expected:
                raise ValueError("destination changed during installation; retry after inspecting it")
            effect = self.task_workspace.change_work_product(product["id"], "whole", change.model_dump())
            if effect["status"] != "verified":
                raise ValueError("skill publication failed verification; inspect the work-product receipt")
            return {
                **self._result(
                    "updated" if existing is not None else "installed", skill, destination, source_id, commit
                ),
                "files": len(bundle.files),
                "bytes": sum(len(item.bytes()) for item in bundle.files.values()),
                "revision": change.revision,
                "work_product_id": product["id"],
                "effect_id": effect["id"],
            }

        publication = asyncio.create_task(asyncio.to_thread(publish))
        try:
            result = await asyncio.shield(publication)
        except asyncio.CancelledError:
            await publication
            await asyncio.to_thread(self.refresh)
            raise
        await asyncio.to_thread(self.refresh)
        return result

    @staticmethod
    def _result(status: str, skill: Skill, destination: Path, source: str, ref: str) -> dict[str, Any]:
        invocation = f"/skill:{skill.name}" if skill.disable_model_invocation else "load_skill(name)"
        return {
            "status": status,
            "name": skill.name,
            "path": str(destination),
            "source": source,
            "ref": ref,
            "message": f"Skill {skill.name!r} is available now; use {invocation}. No restart required.",
        }

    async def _get(self, client: httpx.AsyncClient, url: str) -> bytes:
        for _ in range(4):
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in _HOSTS:
                raise ValueError("refusing a GitHub archive redirect outside the approved hosts")
            await asyncio.to_thread(validate_public_url, url, self.host_guard)
            token = self.token()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with client.stream("GET", url, headers=headers) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("GitHub returned a redirect without a location")
                    url = str(response.url.join(location))
                    continue
                if response.status_code >= 400:
                    raise ValueError(
                        f"GitHub returned HTTP {response.status_code}; check repository/ref/access. "
                        "Private repositories require GH_TOKEN or GITHUB_TOKEN in the operator environment."
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > MAX_BUNDLE_BYTES:
                        raise ValueError("GitHub response exceeds the download size limit")
                    body.extend(chunk)
                return bytes(body)
        raise ValueError("too many GitHub redirects")

    async def _source(
        self,
        source: str,
        path: str | None,
        ref: str | None,
    ) -> tuple[str, str, str | None, DirectoryBundle]:
        if source.startswith((".", "/", "~")):
            directory = self.workspace.resolve_for_mutation(source)
            relative = directory.relative_to(self.workspace.root).as_posix()
            adapter = DirectoryResourceAdapter(self.workspace, self.task_workspace.artifacts)
            tree = await asyncio.to_thread(adapter.read, relative)
            if tree is None:
                raise ValueError("local source directory does not exist")
            return relative, tree.revision, path, tree
        owner, repository, resolved_ref, selected = self._github(source, path, ref)
        base = f"https://api.github.com/repos/{owner}/{repository}"
        async with httpx.AsyncClient(timeout=30, transport=self.transport, follow_redirects=False) as client:
            if resolved_ref is None:
                info = json.loads(await self._get(client, base))
                resolved_ref = info.get("default_branch")
                if not isinstance(resolved_ref, str) or not resolved_ref:
                    raise ValueError("GitHub did not return a default branch")
            info = json.loads(await self._get(client, base + "/commits/" + quote(resolved_ref, safe="")))
            commit = info.get("sha")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise ValueError("GitHub did not return a valid commit SHA")
            archive = await self._get(
                client, f"https://codeload.github.com/{owner}/{repository}/zip/{commit}"
            )
        tree = await asyncio.to_thread(self._archive, archive)
        return f"https://github.com/{owner}/{repository}", commit, selected, tree

    @staticmethod
    def _github(source: str, path: str | None, ref: str | None) -> tuple[str, str, str | None, str | None]:
        if "://" not in source:
            source = "https://github.com/" + source
        parsed = urlparse(source)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"github.com", "raw.githubusercontent.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise ValueError("source must be an HTTPS GitHub URL, owner/repo, or workspace directory")
        parts = unquote(parsed.path).strip("/").split("/")
        if len(parts) < 2 or not all(
            _SEGMENT.fullmatch(part) and part not in {".", ".."} for part in parts[:2]
        ):
            raise ValueError("invalid GitHub owner/repository")
        owner, repository = parts[:2]
        repository = repository.removesuffix(".git")
        tail = parts[2:]
        if parsed.hostname == "github.com" and tail:
            if tail.pop(0) not in {"tree", "blob"}:
                raise ValueError("use a repository, tree or SKILL.md blob URL")
        if tail:
            if ref is not None:
                ref_parts = ref.split("/")
                if tail[: len(ref_parts)] != ref_parts:
                    raise ValueError("explicit ref does not match the URL; use owner/repo with ref and path")
                tail = tail[len(ref_parts) :]
            else:
                ref, tail = tail[0], tail[1:]
            url_path = "/".join(tail)
            if url_path.endswith("/SKILL.md") or url_path == "SKILL.md":
                url_path = url_path.removesuffix("SKILL.md").rstrip("/")
            if path is None and url_path:
                path = url_path
        if path is not None and path != ".":
            bundle_path(path)
        return owner, repository, ref, path

    @staticmethod
    def _archive(body: bytes) -> DirectoryBundle:
        files: dict[str, BundleFile] = {}
        directories: list[str] = []
        total = 0
        root: str | None = None
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if len(archive.infolist()) > MAX_BUNDLE_FILES * 2:
                raise ValueError("repository contains too many archive entries")
            for item in archive.infolist():
                safe = bundle_path(item.filename.rstrip("/"))
                first, _, relative = safe.partition("/")
                if root is None:
                    root = first
                if first != root:
                    raise ValueError("unexpected GitHub archive layout")
                mode = item.external_attr >> 16
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ValueError("repository archive contains a symbolic link or special file")
                if item.is_dir():
                    if relative:
                        directories.append(relative)
                    continue
                if not relative or relative in files:
                    raise ValueError("invalid or duplicate repository archive entry")
                total += item.file_size
                if total > MAX_BUNDLE_BYTES:
                    raise ValueError("repository exceeds expanded archive size limit")
                files[relative] = BundleFile.from_bytes(archive.read(item), executable=bool(mode & 0o111))
        bundle = DirectoryBundle(files=files, directories=directories)
        bundle.validated_files()
        return bundle

    @staticmethod
    def _candidates(
        tree: DirectoryBundle,
        selected: str | None,
        fallback: str,
    ) -> list[tuple[str, Skill]]:
        candidates: list[tuple[str, Skill]] = []
        with tempfile.TemporaryDirectory(prefix="lumen-skill-inspect-") as temporary:
            root = Path(temporary) / fallback
            for file, item in sorted(tree.files.items()):
                if file != "SKILL.md" and not file.endswith("/SKILL.md"):
                    continue
                folder = file.removesuffix("SKILL.md").rstrip("/") or "."
                if selected is not None and folder != selected:
                    continue
                # Ignore implementation/vendor directories when discovering a
                # collection; explicit paths still work.
                if selected is None and any(
                    part in {".git", "node_modules", ".venv"} for part in file.split("/")
                ):
                    continue
                candidate = root / file
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_bytes(item.bytes())
                skill = load_skill(candidate, "project")
                if skill is not None:
                    candidates.append((folder, skill))
        # A root SKILL.md defines the repository itself as one skill bundle.
        root_candidate = [item for item in candidates if item[0] == "."]
        return root_candidate or candidates
