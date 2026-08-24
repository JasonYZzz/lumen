"""Persistent, permission-hardened token storage for FastMCP OAuth."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast


class JsonCredentialStore:
    """Small AsyncKeyValue adapter storing all OAuth state in one 0600 file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = asyncio.Lock()

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        async with self._lock:
            item = self._load_item(key, collection)
            return dict(item["value"]) if item is not None else None

    async def ttl(
        self, key: str, *, collection: str | None = None
    ) -> tuple[dict[str, Any] | None, float | None]:
        async with self._lock:
            item = self._load_item(key, collection)
            if item is None:
                return None, None
            expires_at = item.get("expires_at")
            remaining = max(0.0, float(expires_at) - time.time()) if expires_at is not None else None
            return dict(item["value"]), remaining

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None:
        async with self._lock:
            data = self._load()
            bucket = data.setdefault(collection or "default", {})
            bucket[key] = {
                "value": dict(value),
                "expires_at": time.time() + float(ttl) if ttl is not None else None,
            }
            self._save(data)

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        async with self._lock:
            data = self._load()
            removed = data.get(collection or "default", {}).pop(key, None) is not None
            if removed:
                self._save(data)
            return removed

    async def get_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[dict[str, Any] | None]:
        return [await self.get(key, collection=collection) for key in keys]

    async def ttl_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        return [await self.ttl(key, collection=collection) for key in keys]

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None:
        if len(keys) != len(values):
            raise ValueError("keys and values must have the same length")
        for key, value in zip(keys, values, strict=True):
            await self.put(key, value, collection=collection, ttl=ttl)

    async def delete_many(self, keys: Sequence[str], *, collection: str | None = None) -> int:
        return sum([await self.delete(key, collection=collection) for key in keys])

    def _load_item(self, key: str, collection: str | None) -> dict[str, Any] | None:
        data = self._load()
        raw = data.get(collection or "default", {}).get(key)
        if not isinstance(raw, dict):
            return None
        expires_at = raw.get("expires_at")
        if expires_at is not None and float(expires_at) <= time.time():
            data.get(collection or "default", {}).pop(key, None)
            self._save(data)
            return None
        value = raw.get("value")
        return raw if isinstance(value, dict) else None

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"cannot read MCP OAuth credentials {self.path}: {error}") from error
        if not isinstance(raw, dict):
            raise RuntimeError(f"invalid MCP OAuth credential file: {self.path}")
        return cast(dict[str, dict[str, dict[str, Any]]], raw)

    def _save(self, data: dict[str, dict[str, dict[str, Any]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass


__all__ = ["JsonCredentialStore"]
