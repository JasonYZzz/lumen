"""``download_file``: transfer a public URL's exact UTF-8 bytes to the workspace."""

# Model-facing Chinese prose is kept as authored for readability.
# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from lumen.tools.spec import EffectKind, Risk, ToolSpec
from lumen.tools.web.http import (
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    REQUEST_HEADERS,
    is_public_host,
    retry_delay,
    retry_exhausted,
    retryable,
    tool_timeout,
    validate_public_url,
)
from lumen.tools.workspace import Workspace

if TYPE_CHECKING:
    from lumen.work_products import TaskWorkspace


def build_download_file_spec(
    root: str | Path,
    *,
    task_workspace: TaskWorkspace | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    transport: httpx.AsyncBaseTransport | None = None,
    host_guard: Callable[[str], bool] = is_public_host,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> ToolSpec:
    """Transfer source text without sending its body through model output.

    Network reads use the web permission path, independent of subprocess
    networking. Local publication uses TaskWorkspace's existing journal.
    """
    workspace = Workspace(root)
    if retry_attempts < 1 or retry_backoff_seconds < 0:
        raise ValueError("retry_attempts must be positive and retry_backoff_seconds cannot be negative")
    total_timeout = tool_timeout(timeout, retry_attempts, retry_backoff_seconds)

    async def download_file(
        url: str, path: str, overwrite: bool = False, sha256: str | None = None,
    ) -> dict[str, Any]:
        """Download a public URL's exact UTF-8 source bytes to a workspace file.

        Use this only when the resource must be saved exactly. Use web_fetch
        to read webpages, APIs, JSON or RSS/XML without creating a file.
        For complete skill installation use install_skill instead.
        Use raw.githubusercontent.com URLs, not GitHub
        HTML views. Never transcribe large files or base64 through the model.
        Parent directories are created; existing files require overwrite=True.
        Returns only path, byte size and SHA-256, not the downloaded body.
        Optionally require an expected sha256. Binary files are rejected.
        This tool cannot write global home paths. Use list_skills to refresh
        the catalog after manually downloading a skill's complete resources.
        """
        resolved = workspace.resolve_for_mutation(path)
        expected_revision = await asyncio.to_thread(workspace.revision, resolved)
        if expected_revision != "missing" and not overwrite:
            raise FileExistsError(f"file already exists; pass overwrite=True to replace: {path}")
        if sha256 is not None:
            if len(sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in sha256):
                raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        encoded: bytes | None = None
        async with asyncio.timeout(total_timeout):
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=transport,
                headers=REQUEST_HEADERS,
            ) as client:
                for attempt in range(retry_attempts):
                    current = await asyncio.to_thread(validate_public_url, url, host_guard)
                    body = bytearray()
                    try:
                        for _ in range(MAX_REDIRECTS + 1):
                            async with client.stream("GET", current) as response:
                                if response.is_redirect:
                                    location = response.headers.get("location")
                                    if not location:
                                        raise ValueError(
                                            f"redirect without location header from {current}"
                                        )
                                    current = await asyncio.to_thread(
                                        validate_public_url,
                                        str(current.join(location)),
                                        host_guard,
                                    )
                                    continue
                                response.raise_for_status()
                                content_type = (
                                    response.headers.get("content-type", "")
                                    .split(";", 1)[0]
                                    .lower()
                                )
                                if content_type in {"text/html", "application/xhtml+xml"}:
                                    raise ValueError(
                                        "download_file requires a raw source URL, not an HTML page"
                                    )
                                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                                    if len(body) + len(chunk) > max_bytes:
                                        raise ValueError(
                                            f"download exceeds {max_bytes} bytes; no file was written"
                                        )
                                    body.extend(chunk)
                                encoded = bytes(body)
                                break
                        else:
                            raise ValueError(
                                f"too many redirects (>{MAX_REDIRECTS}) starting from {url}"
                            )
                        break
                    except httpx.HTTPError as error:
                        if not retryable(error):
                            raise
                        if attempt + 1 >= retry_attempts:
                            raise retry_exhausted(
                                "download_file", url, retry_attempts, error
                            ) from error
                        await asyncio.sleep(
                            retry_delay(error, attempt, retry_backoff_seconds)
                        )
        if encoded is None:
            raise RuntimeError("download_file exhausted retries without a result")
        digest = hashlib.sha256(encoded).hexdigest()
        if sha256 is not None and digest != sha256.lower():
            raise ValueError("download SHA-256 mismatch; no file was written")
        try:
            content = encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("download_file supports UTF-8 text only; no file was written") from error
        if "\x00" in content:
            raise ValueError("download_file does not support binary files; no file was written")

        def publish() -> dict[str, Any]:
            result = {"path": path, "bytes": len(encoded), "sha256": digest}

            def write() -> dict[str, Any]:
                workspace.atomic_write(resolved, encoded, expected_revision=expected_revision)
                return result

            if task_workspace is None:
                return write()
            return task_workspace.perform_text_mutation(
                path, operation="download_file", selector="whole", change=content, action=write,
            )

        # Finish the atomic publication/journal before propagating cancellation;
        # no worker may mutate the workspace after the invocation has returned.
        publication = asyncio.create_task(asyncio.to_thread(publish))
        try:
            return await asyncio.shield(publication)
        except asyncio.CancelledError:
            await publication
            raise

    return ToolSpec(
        download_file,
        description=(
            "仅当用户需要把已知原始 URL 的完整 UTF-8 文件保存到工作区时使用。不要用它阅读"
            "网页、API、JSON 或 RSS/XML；这些任务应使用 web_fetch。HTML 页面和二进制文件会被"
            "拒绝；临时网络错误会自动重试。已有文件只有 overwrite=true 时才覆盖，可用 sha256 "
            "校验内容。结果只返回路径、字节数和 SHA-256。"
        ),
        risk=Risk.EXTERNAL,
        effect_kind=EffectKind.MUTATION,
        timeout=total_timeout,
    )
