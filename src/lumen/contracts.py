"""Deterministic catalog generated from Lumen's runtime contract authorities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from lumen.agent_loop import LoopEvent, LoopState
from lumen.application.models import CommandResult, WorkspaceCommand
from lumen.config import AppConfig
from lumen.context import ModelInputManifest, ProviderRequestReceipt
from lumen.events import RunEvent
from lumen.provider_catalog import CATALOG_REVISION, RULES
from lumen.sessions import SCHEMA_VERSION, SESSION_RECORD_TYPES, SUPPORTED_SCHEMA_VERSIONS
from lumen.tools.presentation import ToolCallView, ToolResultView
from lumen.tools.spec import EffectKind, Risk, ToolConcurrency

CATALOG_VERSION = 7
GENERATED_PATH = Path(__file__).resolve().parents[2] / "docs" / "generated" / "contracts.json"


def build_contract_catalog() -> dict[str, Any]:
    """Build one navigation/readiness projection; runtime types remain authoritative."""

    return {
        "catalog_version": CATALOG_VERSION,
        "app_config": AppConfig.model_json_schema(),
        "provider_catalog": {
            "revision": CATALOG_REVISION,
            "authority": "lumen.provider_catalog",
            "profiles": [rule.key for rule in RULES],
            "generated_matrix": "docs/generated/provider-reasoning.md",
        },
        "workspace_commands": TypeAdapter(WorkspaceCommand).json_schema(),
        "workspace_results": TypeAdapter(CommandResult).json_schema(),
        "run_events": TypeAdapter(RunEvent).json_schema(),
        "agent_loop": {
            "states": [item.value for item in LoopState],
            "events": TypeAdapter(LoopEvent).json_schema(),
            "authority": "LumenAgentLoop",
            "provider_adapter": "PydanticAIModelDriver",
        },
        "tool_spec": {
            "compatibility_constructor_fields": [
                "function",
                "risk",
                "name",
                "description",
                "timeout",
                "effect_kind",
                "output",
                "concurrency",
                "presentation",
            ],
            "risk": [item.value for item in Risk],
            "effect_kind": [item.value for item in EffectKind],
            "concurrency": [item.value for item in ToolConcurrency],
            "output_contract": {
                "canonical_value": "Pydantic TypeAdapter validated JSON value",
                "model_projection": "str",
                "client_projection": "object|null",
            },
            "presentation": {
                "call": ToolCallView.model_json_schema(),
                "result": ToolResultView.model_json_schema(),
                "failure_policy": "fallback projection; never changes authoritative outcome",
            },
        },
        "session": {
            "current_schema": SCHEMA_VERSION,
            "supported_schemas": list(SUPPORTED_SCHEMA_VERSIONS),
            "record_types": list(SESSION_RECORD_TYPES),
            "upgrade_policy": "append-only schema_upgrade; never rewrite prior records",
            "model_input_manifest": ModelInputManifest.model_json_schema(),
            "request_receipt": ProviderRequestReceipt.model_json_schema(),
        },
        "module_graph": [
            {
                "module": "WorkspaceHost",
                "implementation": "lumen.application.host.WorkspaceHost",
                "consumes": ["RunCoordinator", "SessionRepository", "ResourceManager"],
            },
            {
                "module": "AgentRuntime",
                "implementation": "lumen.runtime.AgentRuntime",
                "consumes": [
                    "LumenAgentLoop",
                    "ContextEngine",
                    "ToolRegistry",
                    "SessionRepository",
                    "InteractiveMessageQueue",
                ],
            },
            {
                "module": "LumenAgentLoop",
                "implementation": "lumen.agent_loop.loop.LumenAgentLoop",
                "consumes": [
                    "ModelDriver",
                    "CapabilityGateway",
                    "CompletionGate",
                ],
            },
            {
                "module": "PydanticAIModelDriver",
                "implementation": "lumen.agent_loop.pydantic_driver.PydanticAIModelDriver",
                "consumes": ["PydanticAI Model.request_stream"],
            },
            {
                "module": "RegistrationScope",
                "implementation": "lumen.lifecycle.RegistrationScope",
                "consumes": [],
            },
            {
                "module": "TaskWorkspace",
                "implementation": "lumen.work_products.workspace.TaskWorkspace",
                "consumes": ["ArtifactStore", "SessionRepository", "Workspace"],
            },
            {
                "module": "AgentOrchestrator",
                "implementation": "lumen.agents.orchestrator.AgentOrchestrator",
                "consumes": ["AgentRuntimeFactory", "SessionRepository", "ArtifactStore"],
            },
        ],
        "runtime_invariants": [
            {
                "owner": "ContextEngine",
                "name": "model_visible_history_is_canonical",
                "enforced_by": "ContextEngine.prepare/commit and context validation",
            },
            {
                "owner": "LumenAgentLoop",
                "name": "provider_events_follow_validated_explicit_state_transitions",
                "enforced_by": "LumenAgentLoop.validate_transition and provider sequence validation",
            },
            {
                "owner": "LumenAgentLoop",
                "name": "tool_calls_only_execute_through_capability_gateway",
                "enforced_by": (
                    "LumenAgentLoop tool scheduler and LoopToolCallsUnsupported fail-closed gate"
                ),
            },
            {
                "owner": "LumenAgentLoop",
                "name": "tool_results_preserve_provider_call_order",
                "enforced_by": (
                    "provider-ordered tool continuation; completion-ordered events retain call order"
                ),
            },
            {
                "owner": "CapabilityGateway",
                "name": "pre_hook_precedes_approval_and_post_hook_cannot_change_canonical_output",
                "enforced_by": "CapabilityGateway.prepare and _finish_success",
            },
            {
                "owner": "AgentRuntime",
                "name": "interactive_input_is_frozen_before_provider_request_receipt",
                "enforced_by": "_dequeue_native_input and _freeze_lumen_request",
            },
            {
                "owner": "RecoveryReceiptLedger",
                "name": "successful_side_effect_replay_is_exact",
                "enforced_by": "replay and record_success adapters",
            },
            {
                "owner": "ContextEngine",
                "name": "every_provider_request_has_bounded_input_evidence",
                "enforced_by": "AgentRuntime._freeze_lumen_request and ModelInputManifest",
            },
            {
                "owner": "TaskWorkspace",
                "name": "completion_requires_verified_effects",
                "enforced_by": "TaskWorkspace.completion_issues",
            },
            {
                "owner": "TaskWorkspace",
                "name": "mutation_uses_expected_revision",
                "enforced_by": "Workspace.atomic_write",
            },
            {
                "owner": "AgentOrchestrator",
                "name": "root_completion_has_no_unresolved_agents",
                "enforced_by": "AgentOrchestrator.completion_issues",
            },
            {
                "owner": "ResourceManager",
                "name": "effective_capability_catalog_is_consistent",
                "enforced_by": "ResourceManager.runtime_invariant_report",
            },
        ],
    }


def render_contract_catalog() -> str:
    return json.dumps(build_contract_catalog(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Regenerate the checked-in catalog.")
    mode.add_argument("--check", action="store_true", help="Fail when the checked-in catalog is stale.")
    args = parser.parse_args()
    rendered = render_contract_catalog()
    if args.write:
        GENERATED_PATH.parent.mkdir(parents=True, exist_ok=True)
        GENERATED_PATH.write_text(rendered, encoding="utf-8")
        return 0
    if not GENERATED_PATH.is_file() or GENERATED_PATH.read_text(encoding="utf-8") != rendered:
        print("generated contract catalog is stale; run `python -m lumen.contracts --write`")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "CATALOG_VERSION",
    "GENERATED_PATH",
    "build_contract_catalog",
    "render_contract_catalog",
]
