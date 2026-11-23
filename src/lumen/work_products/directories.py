"""Whole-directory snapshots and verified publication for installed bundles."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

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
        or len(value) > 4096
        or len(path.parts) > 128
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
        for name in self.directories:
            bundle_path(name)
        directories = self.directory_names()
        if len(directories) > MAX_BUNDLE_FILES or len(set(self.directories)) != len(self.directories):
            raise AdapterError("too many or duplicate bundle directories")
        for name in directories:
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
        return self.read_tree(target)

    @staticmethod
    def read_tree(target: Path) -> DirectoryBundle:
        if target.is_symlink() or not target.is_dir():
            raise AdapterError("bundle target is not a directory")
        files: dict[str, BundleFile] = {}
        directory_names: list[str] = []
        total = 0
        def fail(error: OSError) -> None:
            raise error

        if sys.platform == "win32":
            entries = ((str(path), dirs, names, None) for path, dirs, names in
                       os.walk(target, onerror=fail, followlinks=False))
        else:
            entries = os.fwalk(target, onerror=fail, follow_symlinks=False)
        for current, directories, names, descriptor in entries:
            for name in directories:
                path = Path(current) / name
                info = (path.lstat() if sys.platform == "win32"
                        else os.stat(name, dir_fd=descriptor, follow_symlinks=False))
                if not stat.S_ISDIR(info.st_mode):
                    raise AdapterError("bundle contains a symbolic link or special file")
                directory_names.append(path.relative_to(target).as_posix())
            if len(directory_names) > MAX_BUNDLE_FILES:
                raise AdapterError("bundle contains too many directories")
            for name in sorted(names):
                path = Path(current) / name
                if len(files) >= MAX_BUNDLE_FILES:
                    raise AdapterError("bundle exceeds snapshot limits")
                if sys.platform == "win32":
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        raise AdapterError("bundle contains a symbolic link or special file")
                    stream = path.open("rb")
                else:
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    stream = os.fdopen(os.open(name, flags, dir_fd=descriptor), "rb")
                with stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode):
                        raise AdapterError("bundle contains a symbolic link or special file")
                    if sys.platform == "win32" and not os.path.samestat(path.lstat(), opened):
                        raise AdapterError("bundle file changed during read")
                    remaining = MAX_BUNDLE_BYTES - total
                    if opened.st_size > remaining:
                        raise AdapterError("bundle exceeds snapshot limits")
                    body = stream.read(remaining + 1)
                    if len(body) > remaining:
                        raise AdapterError("bundle exceeds snapshot limits")
                total += len(body)
                files[path.relative_to(target).as_posix()] = BundleFile.from_bytes(
                    body, executable=bool(opened.st_mode & 0o111),
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
        if self._removal(resource, before, change):
            current = self.read(resource)
            if current is None or ".lumen-install.json" not in current.files:
                raise AdapterError("only managed installations can be removed")
            marker = current.files.pop(".lumen-install.json")
            fields = json.loads(marker.bytes())
            if not isinstance(fields, dict) or cast(dict[str, object], fields).get(
                "content_revision"
            ) != current.revision:
                raise AdapterError("locally modified installation; files preserved")
            bundle = None
        else:
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
        del target
        expected = ("missing" if self._removal(resource, before, change)
                    else DirectoryBundle.model_validate(change).revision)
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

    @staticmethod
    def _removal(resource: str, before: RevisionSnapshot, change: Any) -> bool:
        return (
            any(resource.startswith(prefix) and "/" not in resource.removeprefix(prefix)
                for prefix in (".lumen/skills/", USER_SKILLS_PREFIX))
            and before.exists and change == {"remove_install": before.revision}
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
        if self.workspace.root.is_relative_to(target):
            raise WorkspaceViolation("cannot replace the workspace root or its ancestors")
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
