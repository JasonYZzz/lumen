"""Exercise the frozen backend under a clean HOME and a Finder-like environment."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx


class LocalProvider(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/chat/completions" and body["messages"]
        chunk = {"id": "desktop-smoke", "object": "chat.completion.chunk", "created": 1,
                 "model": "desktop-smoke", "choices": [{"index": 0,
                 "delta": {"role": "assistant", "content": "Hello desktop"}, "finish_reason": None}]}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()


async def smoke(backend: Path) -> None:
    provider = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="lumen-desktop-smoke-") as temporary:
        root = Path(temporary)
        home, project = root / "home", root / "project"
        (home / ".lumen").mkdir(parents=True)
        project.mkdir()
        (home / ".lumen/agent.yaml").write_text(
            "version: 2\nagent:\n  model:\n    id: openai:desktop-smoke\n    api: chat\n"
            f"    base_url: http://127.0.0.1:{provider.server_port}/v1\n"
            "    api_key: not-a-real-credential\ntools: {builtins: []}\n", encoding="utf-8",
        )
        process = await asyncio.create_subprocess_exec(
            str(backend), "--cwd", str(project), cwd=root,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdout is not None and process.stdin is not None
        try:
            ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
            assert ready["type"] == "ready", "Frozen backend did not become ready"
            origin = f'http://127.0.0.1:{ready["port"]}'
            async with httpx.AsyncClient(base_url=origin, follow_redirects=True, timeout=10) as client:
                assert (await client.get("/api/v1/bootstrap")).status_code == 401
                response = await client.get("/", params={"token": ready["token"]})
                assert response.status_code == 200 and "token=" not in str(response.url)
                assert (await client.get("/api/v1/bootstrap")).status_code == 200
                response = await client.post("/api/v1/sessions", headers={"Origin": "https://invalid.test"})
                assert response.status_code == 403
                response = await client.post("/api/v1/sessions", headers={"Origin": origin})
                response.raise_for_status()
                identity = response.json()["sessionId"]
                response = await client.post(f"/api/v1/sessions/{identity}/runs",
                    json={"input": "Say hello", "clientRequestId": "desktop-smoke"},
                    headers={"Origin": origin})
                assert response.status_code == 202
                async with asyncio.timeout(30):
                    while True:
                        snapshot = (await client.get(f"/api/v1/sessions/{identity}")).json()
                        if snapshot["activeRunId"] is None:
                            assert any(item["kind"] == "assistant" and "Hello desktop" in item["text"]
                                       for item in snapshot["timeline"]), "Assistant result was not persisted"
                            break
                        await asyncio.sleep(0.05)
                assert (await client.get("/", params={"token": ready["token"]})).status_code == 401
            process.stdin.write(b'{"version":1,"type":"inspect","request_id":"check","value":{}}\n')
            await process.stdin.drain()
            state = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
            assert state["type"] == "state" and state["active_run_id"] is None
            process.stdin.close()
            closed = json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
            assert closed["type"] == "closed"
            await asyncio.wait_for(process.wait(), 30)
            assert process.returncode == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.to_thread(provider.shutdown)
            provider.server_close()
    print("Frozen arm64 backend: clean environment, auth, Host turn, persistence and EOF shutdown passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", type=Path)
    args = parser.parse_args()
    if os.uname().machine != "arm64":
        parser.error("This smoke test requires Apple Silicon")
    asyncio.run(smoke(args.backend.resolve()))


if __name__ == "__main__":
    main()
