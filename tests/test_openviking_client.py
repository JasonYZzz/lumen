"""Wire contract and end-to-end scoped worker tests for OpenViking 0.5.6."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from lumen.context.memory.manager import MemoryManager
from lumen.context.memory.openviking import OpenVikingClient, OpenVikingService, VikingError
from lumen.context.memory.repository import SQLiteMemoryRepository


def key() -> str:
    return ".".join(base64.urlsafe_b64encode(s).decode().rstrip("=") for s in (b"demo", b"alice", b"test"))


def identity(role: str = "user") -> dict[str, object]:
    return {
        "status": "ok",
        "healthy": True,
        "auth_mode": "api_key",
        "version": "0.5.6",
        "role": role,
        "account_id": "demo",
        "user_id": "alice",
    }


@pytest.mark.asyncio
async def test_scoped_wire_sync_recall_forget(tmp_path: Path) -> None:
    requests: list[str] = []
    objects: dict[str, str] = {}

    def serve(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        requests.append(path)
        assert req.headers["x-api-key"] == key()
        if path == "/health":
            return httpx.Response(200, json=identity())
        if path.endswith("/write"):
            body = json.loads(req.content)
            assert body["mode"] == "replace" and body["uri"].startswith("viking://user/alice/")
            objects[body["uri"]] = body["content"]
            result = {"uri": body["uri"]}
        elif path.endswith("/find"):
            body = json.loads(req.content)
            assert body["limit"] == 8 and body["read_content"] is False
            assert body["target_uri"] and all(
                u.startswith("viking://user/alice/") for u in body["target_uri"]
            )
            result = {
                "resources": [
                    {"uri": u, "abstract": "ignored remote prose"}
                    for u in [*objects, "viking://user/bob/resources/private.md"]
                ]
            }
        elif path.endswith("/stat"):
            uri = req.url.params["uri"]
            if uri not in objects:
                return httpx.Response(404)
            result = {"uri": uri}
        else:
            assert path == "/api/v1/fs" and req.method == "DELETE" and req.url.params["recursive"] == "false"
            uri = req.url.params["uri"]
            objects.pop(uri, None)
            result = {"uri": uri}
        return httpx.Response(200, json={"status": "ok", "result": result})

    repo = SQLiteMemoryRepository(tmp_path / "records.sqlite3")
    service = OpenVikingService(
        repo,
        endpoint="https://example.invalid",
        api_key=key(),
        project_id="demo",
        transport=httpx.MockTransport(serve),
    )
    manager = MemoryManager(repo, project_id="demo")
    try:
        service.start()
        assert not (await service.control("status"))["enabled"]
        assert await service.find("hello", set()) == () and not requests
        await service.control("enable")
        rec = manager.remember("Canary release for internal accounts")
        async with asyncio.timeout(5):
            while (await service.control("status")).get("ready") != 1:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        uris = await service.find("发布第一批用户", set())
        assert len(uris) == 1 and [r.content for r in service.resolve(uris)] == [rec.content]
        # Simulate an upstream restore, then advance the durable audit schedule.
        objects.clear()
        with sqlite3.connect(tmp_path / "records.sqlite3") as db:
            db.execute("UPDATE ov_object SET due=0 WHERE state='ready'")
            db.execute("UPDATE ov_audit SET next_audit=0")
        async with asyncio.timeout(6):
            while not objects:  # noqa: ASYNC110 - require actual background re-upload
                await asyncio.sleep(0.02)
        assert list(objects) == list(uris)
        count = len(requests)
        assert await service.find("发布", {rec.id}) == ()
        assert await service.find("token=example-secret", set()) == ()
        service.set_incognito(True)
        assert (
            await service.find("发布", set()) == () and service.resolve(uris) == [] and len(requests) == count
        )
        service.set_incognito(False)
        manager.forget(rec.id)
        assert service.resolve(uris) == []
        async with asyncio.timeout(5):
            while objects:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        await service.control("disable")
        assert await service.find("发布", set()) == ()
    finally:
        await service.close()
        await manager.close()


@pytest.mark.asyncio
async def test_health_requires_pinned_authenticated_identity_and_redirects_fail_closed() -> None:
    for response in [
        httpx.Response(200, json=identity("root")),
        httpx.Response(200, json={"status": "ok", "healthy": True}),
        httpx.Response(307, headers={"location": "https://untrusted.invalid/"}),
    ]:
        requests: list[httpx.Request] = []

        def handle(
            request: httpx.Request, result: httpx.Response = response, seen: list[httpx.Request] = requests
        ) -> httpx.Response:
            seen.append(request)
            return result

        client = OpenVikingClient("https://test.invalid", key(), transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(VikingError):
                await client.identity()
            assert len(requests) == 1
        finally:
            await client.close()


@pytest.mark.parametrize(
    "endpoint",
    ["http://remote.invalid", "file:///etc", "https://user@host", "https://host/?query", "https://host/path"],
)
def test_rejects_unsafe_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError):
        OpenVikingClient(endpoint, key())


@pytest.mark.asyncio
async def test_concurrent_disable_wins_after_slow_grant_verification(tmp_path: Path) -> None:
    checking = asyncio.Event()
    resume = asyncio.Event()

    async def serve(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        checking.set()
        await resume.wait()
        return httpx.Response(200, json=identity())

    repo = SQLiteMemoryRepository(tmp_path / "grant.sqlite3")
    service = OpenVikingService(
        repo,
        endpoint="http://127.0.0.2",
        api_key=key(),
        project_id="race",
        transport=httpx.MockTransport(serve),
    )
    try:
        enabling = asyncio.create_task(service.control("enable"))
        await asyncio.wait_for(checking.wait(), timeout=2)
        disabling = asyncio.create_task(service.control("disable"))
        await asyncio.sleep(0)
        assert not disabling.done()
        resume.set()
        await asyncio.gather(enabling, disabling)
        assert not (await service.control("status"))["enabled"]
    finally:
        await service.close()
        repo.close()


@pytest.mark.asyncio
async def test_worker_and_queries_share_failed_restart_health_check_backoff(tmp_path: Path) -> None:
    requests: list[str] = []

    def offline(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        raise httpx.ConnectError("unavailable")

    repo = SQLiteMemoryRepository(tmp_path / "restart.sqlite3")
    repo.viking.bind(
        "https://example.invalid",
        "demo",
        "alice",
        "restart",
        credential_hash=hashlib.sha256(key().encode()).hexdigest(),
    )
    service = OpenVikingService(
        repo,
        endpoint="https://example.invalid",
        api_key=key(),
        project_id="restart",
        transport=httpx.MockTransport(offline),
    )
    try:
        service.start()
        searches = await asyncio.gather(*(service.find("current policy", set()) for _ in range(8)))
        assert all(not result for result in searches)
        assert requests == ["/health"]
        assert (await service.control("status"))["last_error"] == "transport_error"
    finally:
        await service.close()
        repo.close()


@pytest.mark.asyncio
async def test_bounded_errors_omit_remote_text_and_credentials() -> None:
    tag = "server-private-error"

    def large(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tag.encode() * 20_000)

    client = OpenVikingClient("https://example.invalid", key(), transport=httpx.MockTransport(large))
    try:
        with pytest.raises(VikingError, match=r"^response_too_large$"):
            await client.identity()
    finally:
        await client.close()
