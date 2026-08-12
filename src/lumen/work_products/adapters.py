"""Internal resource adapters used by :mod:`lumen.work_products`."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import yaml

from lumen.context.artifacts import ArtifactStore
from lumen.tools.workspace import Workspace

from .types import (
    RevisionSnapshot,
    TargetCandidate,
    VerificationResult,
    WorkProductKind,
)


class ResourceAdapter(Protocol):
    """Internal seam for resources with different location and edit semantics."""

    kind: WorkProductKind

    def snapshot(self, resource: str) -> RevisionSnapshot: ...
    def locate(
        self,
        resource: str,
        snapshot: RevisionSnapshot,
        selector: str | None,
    ) -> tuple[TargetCandidate, ...]: ...
    def apply(
        self,
        resource: str,
        before: RevisionSnapshot,
        target: TargetCandidate,
        change: Any,
    ) -> RevisionSnapshot: ...
    def verify(
        self,
        resource: str,
        before: RevisionSnapshot,
        after: RevisionSnapshot,
        target: TargetCandidate,
        change: Any,
    ) -> VerificationResult: ...
    def restore(self, resource: str, snapshot: RevisionSnapshot) -> RevisionSnapshot: ...


class AdapterError(ValueError):
    """Raised when a resource cannot be located or changed safely."""


class _FileAdapter:
    kind: WorkProductKind

    def __init__(self, workspace: Workspace, artifacts: ArtifactStore) -> None:
        self.workspace = workspace
        self.artifacts = artifacts

    def _path(self, resource: str) -> Path:
        return self.workspace.resolve(resource)

    def snapshot(self, resource: str) -> RevisionSnapshot:
        path = self._path(resource)
        if not path.exists():
            return RevisionSnapshot(revision="missing", exists=False)
        if not path.is_file():
            raise AdapterError(f"resource is not a file: {resource}")
        body = path.read_bytes()
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AdapterError(f"resource is not valid UTF-8 text: {resource}") from error
        digest = hashlib.sha256(body).hexdigest()
        return RevisionSnapshot(
            revision=f"sha256:{digest}",
            content_ref=self.artifacts.store(body),
            exists=True,
            byte_size=len(body),
        )

    def _read_snapshot(self, snapshot: RevisionSnapshot) -> str:
        if not snapshot.exists:
            return ""
        if snapshot.content_ref is None:
            raise AdapterError(f"snapshot {snapshot.revision} has no content reference")
        body = self.artifacts.read(snapshot.content_ref)
        if body is None:
            raise AdapterError(f"snapshot artifact is missing: {snapshot.content_ref}")
        return body.decode("utf-8")

    def _write(self, resource: str, text: str) -> RevisionSnapshot:
        path = self._path(resource)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.resolve().is_relative_to(self.workspace.root):
            raise AdapterError(f"resource parent escapes workspace: {resource}")
        _atomic_write(path, text.encode("utf-8"))
        return self.snapshot(resource)

    def restore(self, resource: str, snapshot: RevisionSnapshot) -> RevisionSnapshot:
        path = self._path(resource)
        if not snapshot.exists:
            path.unlink(missing_ok=True)
            return self.snapshot(resource)
        return self._write(resource, self._read_snapshot(snapshot))


class TextResourceAdapter(_FileAdapter):
    kind = WorkProductKind.TEXT
    _heading = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

    def locate(
        self,
        resource: str,
        snapshot: RevisionSnapshot,
        selector: str | None,
    ) -> tuple[TargetCandidate, ...]:
        text = self._read_snapshot(snapshot)
        normalized = (selector or "whole").strip()
        if normalized in {"", "whole", "file"}:
            return (self._candidate(resource, snapshot, "whole", "entire file", 0, len(text)),)
        if normalized.startswith("anchor:"):
            anchor = normalized.removeprefix("anchor:")
            if not anchor:
                raise AdapterError("anchor selector must not be empty")
            return tuple(
                self._candidate(
                    resource,
                    snapshot,
                    normalized,
                    f"anchor {anchor!r}",
                    start,
                    start + len(anchor),
                )
                for start in _find_all(text, anchor)
            )
        if normalized.startswith("lines:"):
            match = re.fullmatch(r"lines:(\d+)-(\d+)", normalized)
            if match is None:
                raise AdapterError("line selector must use lines:<start>-<end>")
            first, last = int(match.group(1)), int(match.group(2))
            if first < 1 or last < first:
                raise AdapterError("line selector range is invalid")
            lines = text.splitlines(keepends=True)
            if last > len(lines):
                return ()
            start = sum(len(line) for line in lines[: first - 1])
            end = sum(len(line) for line in lines[:last])
            return (self._candidate(resource, snapshot, normalized, f"lines {first}-{last}", start, end),)
        if normalized.startswith("heading:"):
            path = tuple(
                part.strip().lstrip("#").strip()
                for part in normalized.removeprefix("heading:").split(">")
                if part.strip()
            )
            if not path:
                raise AdapterError("heading selector must include a heading path")
            return self._heading_candidates(resource, snapshot, text, normalized, path)
        raise AdapterError(
            "unsupported text selector; use whole, anchor:<text>, lines:<start>-<end>, "
            "or heading:<parent > child>"
        )

    def apply(
        self,
        resource: str,
        before: RevisionSnapshot,
        target: TargetCandidate,
        change: Any,
    ) -> RevisionSnapshot:
        if not isinstance(change, str):
            raise AdapterError("text work products require a string change")
        if target.start is None or target.end is None:
            raise AdapterError("text target has no character range")
        text = self._read_snapshot(before)
        if not 0 <= target.start <= target.end <= len(text):
            raise AdapterError("text target is outside the current revision")
        return self._write(resource, text[: target.start] + change + text[target.end :])

    def verify(
        self,
        resource: str,
        before: RevisionSnapshot,
        after: RevisionSnapshot,
        target: TargetCandidate,
        change: Any,
    ) -> VerificationResult:
        if not isinstance(change, str) or target.start is None or target.end is None:
            return VerificationResult(passed=False, summary="text verification inputs are invalid")
        old = self._read_snapshot(before)
        new = self._read_snapshot(after)
        expected = old[: target.start] + change + old[target.end :]
        changed = old[target.start : target.end] != change
        passed = new == expected and changed
        return VerificationResult(
            passed=passed,
            target_changed=changed,
            non_target_unchanged=new == expected,
            summary=(
                "target changed and all non-target text is byte-for-byte unchanged"
                if passed
                else "result does not equal the requested isolated text replacement"
            ),
        )

    def _candidate(
        self,
        resource: str,
        snapshot: RevisionSnapshot,
        selector: str,
        label: str,
        start: int,
        end: int,
    ) -> TargetCandidate:
        raw = f"{resource}\0{snapshot.revision}\0{selector}\0{start}\0{end}".encode()
        return TargetCandidate(
            id=f"target:{hashlib.sha256(raw).hexdigest()[:20]}",
            selector=selector,
            label=label,
            start=start,
            end=end,
        )

    def _heading_candidates(
        self,
        resource: str,
        snapshot: RevisionSnapshot,
        text: str,
        selector: str,
        wanted: tuple[str, ...],
    ) -> tuple[TargetCandidate, ...]:
        lines = text.splitlines(keepends=True)
        offsets: list[int] = []
        cursor = 0
        for line in lines:
            offsets.append(cursor)
            cursor += len(line)
        headings: list[tuple[int, int, str, tuple[str, ...]]] = []
        stack: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            match = self._heading.match(line.rstrip("\r\n"))
            if match is None:
                continue
            level = len(match.group(1))
            name = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, name))
            headings.append((index, level, name, tuple(item[1] for item in stack)))
        candidates: list[TargetCandidate] = []
        for position, (line_index, level, _name, path) in enumerate(headings):
            if path != wanted:
                continue
            start = offsets[line_index] + len(lines[line_index])
            end = len(text)
            for next_line, next_level, _next_name, _next_path in headings[position + 1 :]:
                if next_level <= level:
                    end = offsets[next_line]
                    break
            candidates.append(
                self._candidate(resource, snapshot, selector, f"heading {' > '.join(path)}", start, end)
            )
        return tuple(candidates)


class StructuredResourceAdapter(_FileAdapter):
    def __init__(
        self,
        workspace: Workspace,
        artifacts: ArtifactStore,
        kind: WorkProductKind,
    ) -> None:
        if kind not in {WorkProductKind.JSON, WorkProductKind.YAML}:
            raise ValueError("structured adapter kind must be json or yaml")
        super().__init__(workspace, artifacts)
        self.kind = kind

    def locate(
        self,
        resource: str,
        snapshot: RevisionSnapshot,
        selector: str | None,
    ) -> tuple[TargetCandidate, ...]:
        pointer = "" if selector in {None, "", "whole", "file"} else str(selector)
        document = self._parse(self._read_snapshot(snapshot))
        try:
            value = _pointer_get(document, pointer)
        except (KeyError, IndexError, TypeError, ValueError):
            return ()
        raw = f"{resource}\0{snapshot.revision}\0{pointer}".encode()
        return (
            TargetCandidate(
                id=f"target:{hashlib.sha256(raw).hexdigest()[:20]}",
                selector=pointer,
                label=f"JSON Pointer {pointer or '<root>'}",
                json_pointer=pointer,
                value=_bounded_value(value),
            ),
        )

    def apply(
        self,
        resource: str,
        before: RevisionSnapshot,
        target: TargetCandidate,
        change: Any,
    ) -> RevisionSnapshot:
        pointer = target.json_pointer
        if pointer is None:
            raise AdapterError("structured target has no JSON Pointer")
        document = self._parse(self._read_snapshot(before))
        updated = change if pointer == "" else _pointer_set(document, pointer, change)
        return self._write(resource, self._dump(updated))

    def verify(
        self,
        resource: str,
        before: RevisionSnapshot,
        after: RevisionSnapshot,
        target: TargetCandidate,
        change: Any,
    ) -> VerificationResult:
        pointer = target.json_pointer
        if pointer is None:
            return VerificationResult(passed=False, summary="structured target has no JSON Pointer")
        old = self._parse(self._read_snapshot(before))
        new = self._parse(self._read_snapshot(after))
        try:
            new_value = _pointer_get(new, pointer)
        except (KeyError, IndexError, TypeError, ValueError):
            return VerificationResult(passed=False, summary="target path is missing after mutation")
        changed = _pointer_get(old, pointer) != new_value
        non_target_unchanged = _without_pointer(old, pointer) == _without_pointer(new, pointer)
        passed = new_value == change and changed and non_target_unchanged
        return VerificationResult(
            passed=passed,
            target_changed=changed,
            non_target_unchanged=non_target_unchanged,
            summary=(
                "target value changed and all non-target structured paths are unchanged"
                if passed
                else "structured result changed the wrong value or affected non-target paths"
            ),
        )

    def _parse(self, text: str) -> Any:
        try:
            return json.loads(text) if self.kind is WorkProductKind.JSON else yaml.safe_load(text)
        except (json.JSONDecodeError, yaml.YAMLError) as error:
            raise AdapterError(f"invalid {self.kind.value} document") from error

    def _dump(self, value: Any) -> str:
        if self.kind is WorkProductKind.JSON:
            return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def _find_all(text: str, needle: str) -> Sequence[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        found = text.find(needle, cursor)
        if found < 0:
            return positions
        positions.append(found)
        cursor = found + max(1, len(needle))


def _bounded_value(value: Any, *, limit: int = 2_048) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        if not isinstance(value, str) or len(value) <= limit:
            return value
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) <= limit:
        return value
    return {
        "truncated": True,
        "type": type(value).__name__,
        "characters": len(encoded),
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "preview": encoded[:limit],
    }


def _decode_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must be empty or start with '/'")
    parts: list[str] = []
    for part in pointer[1:].split("/"):
        if re.search(r"~(?![01])", part):
            raise ValueError("JSON Pointer contains an invalid escape")
        parts.append(part.replace("~1", "/").replace("~0", "~"))
    return parts


def _pointer_get(document: Any, pointer: str) -> Any:
    current: Any = document
    for part in _decode_pointer(pointer):
        if isinstance(current, list):
            current = cast(list[Any], current)[int(part)]
        elif isinstance(current, dict):
            current = cast(dict[str, Any], current)[part]
        else:
            raise TypeError("pointer traverses a scalar")
    return current


def _pointer_set(document: Any, pointer: str, value: Any) -> Any:
    parts = _decode_pointer(pointer)
    if not parts:
        return value
    current: Any = document
    for part in parts[:-1]:
        current = (
            cast(list[Any], current)[int(part)]
            if isinstance(current, list)
            else cast(dict[str, Any], current)[part]
        )
    leaf = parts[-1]
    if isinstance(current, list):
        cast(list[Any], current)[int(leaf)] = value
    elif isinstance(current, dict):
        mapping = cast(dict[str, Any], current)
        if leaf not in mapping:
            raise KeyError(leaf)
        mapping[leaf] = value
    else:
        raise TypeError("pointer parent is a scalar")
    return document


def _without_pointer(document: Any, pointer: str) -> Any:
    """Return a canonical structure with the selected subtree replaced."""

    if pointer == "":
        return "<selected-root>"
    clone = copy.deepcopy(document)
    return _pointer_set(clone, pointer, "<selected-value>")


def _atomic_write(path: Path, body: bytes) -> None:
    suffix = secrets.token_hex(8)
    temporary = path.parent / f".{path.name}.tmp-{suffix}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        handle = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        with handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


__all__ = [
    "AdapterError",
    "ResourceAdapter",
    "StructuredResourceAdapter",
    "TextResourceAdapter",
]
