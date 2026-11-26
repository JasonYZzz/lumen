"""OpenViking v0.5.6 private resource projection and query client."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Collection, Sequence
from typing import Any, cast

import httpx

from lumen.config import OpenVikingConfig
from lumen.context.memory.records import MemoryRecord
from lumen.context.memory.redaction import contains_secret
from lumen.context.memory.repository import SQLiteMemoryRepository
from lumen.context.memory.viking_store import VikingJob
from lumen.io import run_io


class VikingError(ValueError):
    """Bounded category suitable for status reporting; never contains remote text."""


def _identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9_@.-]{1,128}", value)
    ):
        raise VikingError("invalid_identity")
    return value


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VikingError("invalid_response")
    return cast(dict[str, Any], value)


def _key_identity(key: str) -> tuple[str, str] | None:
    """The public key prefixes select local state only. /health authenticates the binding."""
    segments = key.split(".")
    if len(segments) != 3:
        return None
    try:

        def decode(part: str) -> str:
            return _identifier(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode())

        return decode(segments[0]), decode(segments[1])
    except (ValueError, UnicodeError):
        return None


class OpenVikingClient:
    def __init__(
        self, endpoint: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        origin = OpenVikingConfig(enabled=True, endpoint=endpoint).endpoint
        assert origin is not None
        self.endpoint = origin
        self._headers = {"X-API-Key": api_key}
        # AsyncClient logs request URLs at INFO, including the private URI on fs calls.
        # Calling the transport directly also makes redirect following impossible.
        self._transport = transport or httpx.AsyncHTTPTransport(
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with asyncio.timeout(8.0):
                request = httpx.Request(method, self.endpoint + path, headers=self._headers, **kwargs)
                response = await self._transport.handle_async_request(request)
                try:
                    if response.status_code in {401, 403}:
                        raise VikingError("authorization_failed")
                    if response.status_code == 404:
                        raise VikingError("not_found")
                    if response.status_code != 200:
                        raise VikingError(f"http_{response.status_code}")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(16384):
                        if len(data) + len(chunk) > 262144:
                            raise VikingError("response_too_large")
                        data.extend(chunk)
                finally:
                    await response.aclose()
        except (TimeoutError, httpx.HTTPError):
            raise VikingError("transport_error") from None
        try:
            result: object = json.loads(data)
        except (ValueError, RecursionError):
            raise VikingError("invalid_response") from None
        envelope = _object(result)
        if envelope.get("status") != "ok":
            raise VikingError("invalid_response")
        return envelope

    async def identity(self) -> tuple[str, str]:
        data = await self._request("GET", "/health")
        if (
            data.get("version") not in {"v0.5.6", "0.5.6"}
            or data.get("auth_mode") != "api_key"
            or data.get("healthy") is not True
            or str(data.get("role", "")).lower().removeprefix("role.") not in {"user", "admin"}
        ):
            raise VikingError("unsupported_service_identity")
        return _identifier(data.get("account_id")), _identifier(data.get("user_id"))

    async def find(self, roots: Sequence[str], query: str) -> tuple[str, ...]:
        if not roots or any(not root.startswith("viking://user/") for root in roots):
            return ()
        envelope = await self._request(
            "POST",
            "/api/v1/search/find",
            json={
                "query": query,
                "target_uri": list(roots),
                "limit": 8,
                "level": 2,
                "read_content": False,
            },
        )
        result = _object(envelope.get("result"))
        if not isinstance(result.get("resources"), list):
            raise VikingError("invalid_response")
        uris: list[str] = []
        for row in cast(list[object], result["resources"])[:8]:
            uri: object = _object(row).get("uri")
            if (
                isinstance(uri, str)
                and len(uri) <= 1024
                and any(uri.startswith(root.rstrip("/") + "/") for root in roots)
            ):
                uris.append(uri)
        return tuple(dict.fromkeys(uris))

    async def exists(self, uri: str) -> bool:
        try:
            data = await self._request("GET", "/api/v1/fs/stat", params={"uri": uri})
        except VikingError as error:
            if str(error) == "not_found":
                return False
            raise
        if _object(data.get("result")).get("uri") != uri:
            raise VikingError("invalid_response")
        return True

    async def write(self, uri: str, content: str) -> None:
        result = (
            await self._request(
                "POST",
                "/api/v1/content/write",
                json={
                    "uri": uri,
                    "content": content,
                    "mode": "replace",
                    "processing_mode": "vectors_only",
                    "wait": False,
                },
            )
        ).get("result")
        if _object(result).get("uri") != uri:
            raise VikingError("invalid_response")

    async def delete(self, uri: str) -> None:
        try:
            result = (
                await self._request(
                    "DELETE",
                    "/api/v1/fs",
                    params={
                        "uri": uri,
                        "recursive": "false",
                        "wait": "false",
                    },
                )
            ).get("result")
        except VikingError as error:
            if str(error) != "not_found":
                raise
        else:
            if _object(result).get("uri") != uri:
                raise VikingError("invalid_response")

    async def close(self) -> None:
        await self._transport.aclose()


class OpenVikingService:
    def __init__(
        self,
        repository: SQLiteMemoryRepository,
        *,
        endpoint: str,
        api_key: str,
        project_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = repository.viking
        self.client = OpenVikingClient(endpoint, api_key, transport=transport)
        self.project_id = project_id
        self._identity = _key_identity(api_key)
        self._credential_hash = hashlib.sha256(api_key.encode()).hexdigest()
        self.store.select_writer(self.client.endpoint, self._credential_hash, project_id)
        self._binding: str | None = None
        self._generation = 0
        self._paused = False
        self._closed = False
        self._last_error: str | None = None
        self._fallback = 0
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._control_lock = asyncio.Lock()
        self._attach_lock = asyncio.Lock()
        self._attach_failures = 0
        self._retry_attach_at = 0.0

    def start(self) -> None:
        if not self._closed and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._worker())

    def set_incognito(self, enabled: bool) -> None:
        self.store.private = enabled
        self._generation += 1
        self._wake.set()

    def _active(self, generation: int) -> bool:
        return not (self._closed or self.store.private or self._paused) and self._generation == generation

    def _lookup(self) -> str | None:
        if self._identity is None:
            return self.store.lookup_writer(self.client.endpoint, self._credential_hash, self.project_id)
        return self.store.lookup(self.client.endpoint, *self._identity, self.project_id)

    def _error(self, error: VikingError) -> None:
        self._last_error = str(error)
        if str(error) in {"authorization_failed", "unsupported_service_identity", "identity_changed"}:
            self._paused = True
            self._generation += 1

    async def _verify(self) -> tuple[str, str]:
        identity = await self.client.identity()
        if self._identity is not None and self._identity != identity:
            raise VikingError("identity_changed")
        self._identity = identity
        return identity

    async def control(self, action: str, *, personal: bool = False) -> dict[str, object]:
        async with self._control_lock:
            return await self._control(action, personal=personal)

    async def _control(self, action: str, *, personal: bool) -> dict[str, object]:
        binding = await run_io(self._lookup)
        if action == "enable":
            if self.store.private or self._closed:
                raise ValueError("OpenViking is unavailable in private mode")
            generation = self._generation
            try:
                account, user = await self._verify()
            except VikingError as error:
                self._error(error)
                raise
            if self.store.private or self._closed or generation != self._generation:
                raise ValueError("OpenViking authorization changed")
            binding = await run_io(
                self.store.bind,
                self.client.endpoint,
                account,
                user,
                self.project_id,
                user_scope=personal,
                credential_hash=self._credential_hash,
            )
            self._binding = binding
            self._paused = False
            self._retry_attach_at = 0.0
            self._attach_failures = 0
            self._last_error = None
            self.start()
        elif action in {"disable", "purge"}:
            self._generation += 1
            if binding is not None:
                await run_io(
                    self.store.disable, binding, self.project_id, user_scope=personal, purge=action == "purge"
                )
        elif action == "backfill":
            if self.store.private or binding is None:
                raise ValueError("Enable OpenViking for this scope first")
            await run_io(self.store.backfill, binding, self.project_id, user_scope=personal)
        elif action != "status":
            raise ValueError("Unknown OpenViking operation")
        if action != "status":
            self._wake.set()
        status: dict[str, object] = {
            "configured": True,
            "endpoint": self.client.endpoint,
            "enabled": False,
            "pending": 0,
            "cleanup": 0,
        }
        if binding is not None:
            status.update(await run_io(self.store.status, binding, self.project_id, user_scope=personal))
        status["eligible"] = await run_io(self.store.eligible_count, self.project_id, user_scope=personal)
        status.update(last_error=status.get("last_error") or self._last_error, fallbacks=self._fallback)
        return status

    async def _attach(self) -> str | None:
        async with self._attach_lock:
            if time.monotonic() < self._retry_attach_at:
                return None
            try:
                binding = await self._resume_binding()
            except VikingError:
                self._attach_failures += 1
                self._retry_attach_at = time.monotonic() + min(60, 2 ** min(self._attach_failures, 6))
                raise
            self._attach_failures = 0
            return binding

    async def _resume_binding(self) -> str | None:
        if self._binding is not None:
            return self._binding
        binding = await run_io(self._lookup)
        if binding is None or not await run_io(self.store.has_work, binding, self.project_id):
            return None
        generation = self._generation
        if not self._active(generation):
            return None
        account, user = await self._verify()
        if not self._active(generation):
            return None
        self._binding = await run_io(
            self.store.connect,
            self.client.endpoint,
            account,
            user,
            self.project_id,
            credential_hash=self._credential_hash,
        )
        return self._binding

    async def find(self, query: str, exclude_ids: Collection[str]) -> tuple[str, ...]:
        query = query.strip()
        generation = self._generation
        if not self._active(generation) or not query or contains_secret(query):
            return ()
        try:
            async with asyncio.timeout(0.3):
                binding = await self._attach()
                if binding is None:
                    return ()
                roots = await run_io(self.store.roots, binding, self.project_id, exclude_ids)
                if not roots or not self._active(generation):
                    return ()
                uris = await self.client.find(roots, query[:512])
                self._last_error = None
                return uris if self._active(generation) else ()
        except TimeoutError:
            self._last_error = "query_timeout"
        except sqlite3.OperationalError:
            self._last_error = "storage_unavailable"
        except VikingError as error:
            self._error(error)
        self._fallback += 1
        return ()

    def resolve(self, uris: Sequence[str]) -> list[MemoryRecord]:
        if not self._binding or self.store.private or self._closed or self._paused:
            return []
        return self.store.resolve(self._binding, self.project_id, uris)

    async def _process(self, job: VikingJob, generation: int) -> None:
        try:
            content = await run_io(self.store.validate, job)
            if content is None or not self._active(generation):
                return
            if job.op == "verify":
                present = await self.client.exists(job.uri)
                if self._active(generation):
                    await run_io(self.store.finish, job, missing=not present)
                return
            if job.op == "delete":
                await self.client.delete(job.uri)
                if self._active(generation) and await run_io(self.store.validate, job) is not None:
                    if self._active(generation) and await self.client.exists(job.uri):
                        raise VikingError("delete_pending")
            elif job.attempts <= 1 or not await self.client.exists(job.uri):
                content = await run_io(self.store.validate, job)
                if content is None or not self._active(generation):
                    return
                await self.client.write(job.uri, content)
            if self._active(generation):
                await run_io(self.store.finish, job)
        except (VikingError, TimeoutError) as exc:
            error = exc if isinstance(exc, VikingError) else VikingError("sync_timeout")
            self._error(error)
            await run_io(self.store.finish, job, error=str(error))

    async def _worker(self) -> None:
        while not self._closed:
            self._wake.clear()
            generation = self._generation
            if self._active(generation):
                try:
                    binding = await self._attach()
                    if binding and self._active(generation):
                        job = await run_io(self.store.claim, binding, self.project_id)
                        if job is not None:
                            await self._process(job, generation)
                            continue
                except VikingError as error:
                    self._error(error)
                except sqlite3.OperationalError:
                    self._last_error = "storage_unavailable"
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._closed = True
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.client.close()
