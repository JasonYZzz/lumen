"""Whole-directory snapshots and verified publication for installed bundles."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lumen.context.artifacts import ArtifactStore
from lumen.tools.workspace import Workspace, WorkspaceViolation

from .adapters import AdapterError, StaleResourceError
from .types import RevisionSnapshot, TargetCandidate, VerificationResult, WorkProductKind

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_FILES = 4096
USER_SKILLS_PREFIX = "@user-skills/"


def bundle_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or ":" in value
    ):
        raise WorkspaceViolation(f"invalid bundle-relative path: {value!r}")
    return path.as_posix()


class BundleFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    content: str
    executable: bool = False

    def bytes(self) -> bytes:
        try:
            return base64.b64decode(self.content, validate=True)
        except ValueError as error:
            raise AdapterError("invalid bundle file encoding") from error

    @classmethod
    def from_bytes(cls, content: bytes, *, executable: bool = False) -> BundleFile:
        return cls(content=base64.b64encode(content).decode("ascii"), executable=executable)


class DirectoryBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    files: dict[str, BundleFile] = Field(max_length=MAX_BUNDLE_FILES)
    directories: list[str] = Field(default_factory=list, max_length=MAX_BUNDLE_FILES)

    def directory_names(self) -> list[str]:
        names = set(self.directories)
        for name in (*self.files, *self.directories):
            names.update(
                parent.as_posix() for parent in PurePosixPath(name).parents if parent.as_posix() != "."
            )
        return sorted(names)

    def validated_files(self) -> dict[str, bytes]:
        decoded: dict[str, bytes] = {}
        total = 0
        folded: set[str] = set()
        for name, item in self.files.items():
            bundle_path(name)
            if name.casefold() in folded:
                raise AdapterError("case-colliding bundle paths")
            folded.add(name.casefold())
            body = item.bytes()
            total += len(body)
            if total > MAX_BUNDLE_BYTES:
                raise AdapterError("bundle exceeds the expanded size limit")
            decoded[name] = body
        for name in decoded:
            if any(parent.as_posix().casefold() in folded for parent in PurePosixPath(name).parents):
                raise AdapterError("bundle path is both a file and a directory")
        directory_case: set[str] = set()
        for name in self.directory_names():
            bundle_path(name)
            if name.casefold() in folded or name.casefold() in directory_case:
                raise AdapterError("bundle directory paths collide")
            directory_case.add(name.casefold())
        return decoded

    def encoded(self) -> bytes:
        payload = self.model_dump()
        payload["directories"] = self.directory_names()
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    @property
    def revision(self) -> str:
        return "sha256:" + hashlib.sha256(self.encoded()).hexdigest()


class DirectoryResourceAdapter:
    kind = WorkProductKind.DIRECTORY

    def __init__(
        self,
        workspace: Workspace,
        artifacts: ArtifactStore,
        *,
        user_skills: Path | None = None,
    ) -> None:
        self.workspace = workspace
        self.artifacts = artifacts
        self.user_skills = user_skills

    def path(self, resource: str) -> Path:
        if resource.startswith(USER_SKILLS_PREFIX):
            if self.user_skills is None:
                raise WorkspaceViolation("managed global skill installation is not enabled")
            name = bundle_path(resource.removeprefix(USER_SKILLS_PREFIX))
            if "/" in name:
                raise WorkspaceViolation("managed user skill targets must name one skill directory")
            for parent in (self.user_skills, *self.user_skills.parents):
                if parent.is_symlink():
                    raise WorkspaceViolation("managed skill root contains a symbolic link")
            return Workspace(self.user_skills).resolve_for_mutation(name)
        target = self.workspace.resolve_for_mutation(resource)
        return target

    def read(self, resource: str) -> DirectoryBundle | None:
        target = self.path(resource)
        if not target.exists():
            return None
        if not target.is_dir():
            raise AdapterError("bundle target is not a directory")
        files: dict[str, BundleFile] = {}
        directory_names: list[str] = []
        total = 0
        for current, directories, names in os.walk(target, followlinks=False):
            directory_names.extend(
                (Path(current) / name).relative_to(target).as_posix() for name in directories
            )
            if len(directory_names) > MAX_BUNDLE_FILES:
                raise AdapterError("bundle contains too many directories")
            for name in (*directories, *names):
                path = Path(current) / name
                mode = path.lstat().st_mode
                if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
                    raise AdapterError("bundle contains a symbolic link or special file")
            for name in sorted(names):
                path = Path(current) / name
                size = path.stat().st_size
                total += size
                if total > MAX_BUNDLE_BYTES or len(files) >= MAX_BUNDLE_FILES:
                    raise AdapterError("bundle exceeds snapshot limits")
                body = path.read_bytes()
                files[path.relative_to(target).as_posix()] = BundleFile.from_bytes(
                    body,
                    executable=bool(path.stat().st_mode & 0o111),
                )
        bundle = DirectoryBundle(files=files, directories=directory_names)
        bundle.validated_files()
        return bundle

    def snapshot(self, resource: str) -> RevisionSnapshot:
        bundle = self.read(resource)
        if bundle is None:
            return RevisionSnapshot(revision="missing", exists=False)
        return RevisionSnapshot(
            revision=bundle.revision,
            content_ref=self.artifacts.store(bundle.encoded()),
            byte_size=sum(len(body) for body in bundle.validated_files().values()),
        )

    def locate(
        self,
        resource: str,
        snapshot: RevisionSnapshot,
        selector: str | None,
    ) -> tuple[TargetCandidate, ...]:
        if selector not in {None, "whole", "directory"}:
            raise AdapterError("directory bundles only support the whole selector")
        return (
            TargetCandidate(
                id="target:" + hashlib.sha256(f"{resource}:{snapshot.revision}".encode()).hexdigest()[:20],
                selector="whole",
                label="entire directory bundle",
            ),
        )

    def apply(
        self,
        resource: str,
        before: RevisionSnapshot,
        target: TargetCandidate,
        change: Any,
    ) -> RevisionSnapshot:
        del target
        bundle = DirectoryBundle.model_validate(change)
        self._publish(resource, bundle, expected_revision=before.revision)
        return self.snapshot(resource)

    def verify(
        self,
        resource: str,
        before: RevisionSnapshot,
        after: RevisionSnapshot,
        target: TargetCandidate,
        change: Any,
    ) -> VerificationResult:
        del resource, target
        expected = DirectoryBundle.model_validate(change).revision
        changed = before.revision != after.revision
        passed = after.revision == expected and changed
        return VerificationResult(
            passed=passed,
            target_changed=changed,
            non_target_unchanged=after.revision == expected,
            summary="directory bytes and executable modes verified"
            if passed
            else "directory verification failed",
        )

    def restore(
        self,
        resource: str,
        snapshot: RevisionSnapshot,
        *,
        expected_revision: str,
    ) -> RevisionSnapshot:
        bundle = None
        if snapshot.exists:
            body = self.artifacts.read(snapshot.content_ref) if snapshot.content_ref else None
            if body is None:
                raise AdapterError("directory snapshot artifact is missing")
            bundle = DirectoryBundle.model_validate_json(body)
        self._publish(resource, bundle, expected_revision=expected_revision)
        return self.snapshot(resource)

    def _publish(self, resource: str, bundle: DirectoryBundle | None, *, expected_revision: str) -> None:
        decoded = bundle.validated_files() if bundle is not None else {}
        target = self.path(resource)
        if target == self.workspace.root:
            raise WorkspaceViolation("cannot replace the workspace root")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Temporary trees are siblings: publication stays on one filesystem.
        stage = Path(tempfile.mkdtemp(prefix=".lumen-install-", dir=target.parent))
        backup = stage / "previous"
        incoming = stage / "incoming"
        incoming.mkdir()
        moved = False
        published = False
        try:
            if bundle is not None:
                for name in bundle.directory_names():
                    (incoming / name).mkdir(parents=True, exist_ok=True)
            for name, body in decoded.items():
                path = incoming / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as file:
                    file.write(body)
                    file.flush()
                    os.fsync(file.fileno())
                assert bundle is not None
                path.chmod(0o755 if bundle.files[name].executable else 0o644)
            for directory in sorted(incoming.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if directory.is_dir():
                    Workspace.fsync_directory(directory)
            Workspace.fsync_directory(incoming)
            target = self.path(resource)
            if self.snapshot(resource).revision != expected_revision:
                raise StaleResourceError("bundle changed before publication; refusing to overwrite")
            if target.exists():
                target.rename(backup)
                moved = True
            if bundle is not None:
                if target.exists() or target.is_symlink():
                    raise StaleResourceError("bundle target appeared during publication")
                incoming.rename(target)
                published = True
            Workspace.fsync_directory(target.parent)
        except BaseException:
            if moved and not published and not target.exists() and not target.is_symlink():
                backup.rename(target)
                moved = False
            raise
        finally:
            # A collision after moving the old target must preserve its backup.
            if not moved or published or bundle is None:
                shutil.rmtree(stage)
