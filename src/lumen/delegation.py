"""Bounded subagent delegation behind one small interface.

The parent runtime sees one ``delegate_task`` tool. This module owns all child
agent details: isolated conversation state, read-only tools, concurrency,
request/tool limits, and timeout handling. Child agents never receive the
delegation tool, so delegation depth is exactly one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pydantic_ai import Agent, Tool, UsageLimits
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from lumen.config import DelegationConfig

DELEGATION_INSTRUCTIONS = """You are a focused research subagent working for a parent coding agent.
Complete only the delegated task. Use the available read-only tools to inspect evidence.
Do not claim to have modified files or run commands: you have no write or execute tools.
Return a concise, self-contained result with relevant file paths and concrete findings.
"""


class DelegationManager:
    """Run isolated, depth-one, read-only child agents with bounded resources."""

    def __init__(
        self,
        *,
        model: Model | str,
        tools: Sequence[Tool[None]],
        config: DelegationConfig,
        model_settings: ModelSettings | None = None,
    ) -> None:
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._agent: Agent[None, str] = Agent(
            model,
            instructions=DELEGATION_INSTRUCTIONS,
            deps_type=type(None),
            tools=list(tools),
            model_settings=model_settings,
            tool_timeout=config.timeout_seconds,
        )

    async def delegate_task(self, task: str) -> str:
        """Complete one independent research task in an isolated context."""

        prompt = task.strip()
        if not prompt:
            raise ValueError("delegated task must not be empty")
        limits = UsageLimits(
            request_limit=self._config.request_count,
            tool_calls_limit=self._config.tool_calls,
        )
        async with self._semaphore:
            try:
                async with asyncio.timeout(self._config.timeout_seconds):
                    result = await self._agent.run(prompt, usage_limits=limits)
            except TimeoutError as error:
                raise TimeoutError(f"delegated task exceeded {self._config.timeout_seconds:g}s") from error
        return result.output
