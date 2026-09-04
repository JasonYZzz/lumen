"""Pure formatting for the ``/context`` budget report.

Extracted from ``app.py``: this does not touch any app state, so it stays a
plain function. ``LumenApp.format_context_result`` re-exports it as a
staticmethod for backwards compatibility with existing callers and tests.
"""

from __future__ import annotations

from lumen.context import ContextControlResult


def format_context_result(result: ContextControlResult) -> str:
    """Format a control result as the ``/context`` zone table (plan §14.1)."""

    payload = result.payload
    if not payload:
        return result.message or "No context prepared yet."
    lines = [result.message]
    profile = payload.get("model_profile", "unknown")
    estimated_fields = payload.get("estimated_fields", [])
    estimate_note = f"; estimated: {', '.join(estimated_fields)}" if estimated_fields else ""
    lines.append(f"Model: {payload.get('active_model', 'unknown')}  profile={profile}{estimate_note}")
    tokenizer = payload.get("tokenizer_adapter", "unknown")
    fallback = payload.get("tokenizer_fallback_reason")
    lines.append(f"Tokenizer: {tokenizer}" + (f"  fallback={fallback}" if fallback else ""))
    lines.append(
        "Thresholds: "
        f"soft={payload.get('soft_limit_tokens', 0)}  "
        f"hard={payload.get('hard_limit_tokens', 0)}  "
        f"target={payload.get('target_tokens', 0)}  "
        f"output={payload.get('output_reserve_tokens', 0)}  "
        f"recent-max={payload.get('keep_recent_tokens', 0)}"
    )
    for zone in payload.get("zones", []):
        share_pct = zone["share"] * 100
        lines.append(f"  {zone['zone']:<18} {zone['tokens']:>7}  {share_pct:5.1f}%  {zone['survival']}")
    pressure = payload.get("pressure", [])
    if pressure:
        lines.append("Top pressure:")
        for item in pressure[:5]:
            lines.append(f"  {item['label']}: {item['tokens']}  ({item['source']})")
    capabilities = payload.get("capabilities", [])
    if capabilities:
        lines.append("Capability schemas:")
        for item in capabilities:
            state = "loaded" if item["loaded"] else "deferred"
            lines.append(f"  {item['name']}: {item['tokens']} tokens  [{state}]  {item['origin']}")
    skills = payload.get("skill_working_set", [])
    if skills:
        lines.append("Active skills:")
        for item in skills:
            lines.append(f"  {item['name']}: {item['tokens']} tokens")
    if payload.get("estimated"):
        lines.append("(model window estimated; no known profile for this provider)")
    checkpoint = payload.get("checkpoint")
    if checkpoint:
        lines.append(
            f"Checkpoint: {checkpoint.get('checkpoint_id')}  "
            f"source={checkpoint.get('source_start')}:{checkpoint.get('source_end')}  "
            f"parent={checkpoint.get('parent_checkpoint_id') or '-'}"
        )
    compaction = payload.get("compaction", {})
    lines.append(
        "Compaction: "
        f"reason={payload.get('last_compaction_reason', 'none')}  "
        f"success={compaction.get('successes', 0)}  "
        f"failure={compaction.get('failures', 0)}  "
        f"cooldown={compaction.get('cooldown', False)}  "
        f"background={payload.get('background_candidate', 'idle')}  "
        f"checkpoint-hits={payload.get('checkpoint_hits', 0)}"
    )
    drift = payload.get("usage_drift", {})
    if drift:
        lines.append(
            "Usage drift: "
            f"estimated={drift.get('estimated_input_tokens', 0)}  "
            f"actual={drift.get('actual_input_tokens', 0)}  "
            f"ratio={drift.get('drift_ratio', 0):.1%}" + ("  WARNING" if drift.get("warning") else "")
        )
    latest_run = payload.get("latest_run", {})
    if latest_run:
        elapsed = latest_run.get("elapsed_seconds")
        elapsed_text = f"{elapsed:.2f}s" if isinstance(elapsed, int | float) else "unknown"
        usage = latest_run.get("usage", {})
        lines.append(
            "Latest run: "
            f"status={latest_run.get('status', 'unknown')}  "
            f"requests={latest_run.get('request_count', 0)}  "
            f"tools={latest_run.get('tool_call_count', 0)}  "
            f"elapsed={elapsed_text}  "
            f"input={usage.get('input_tokens', 0)}  "
            f"output={usage.get('output_tokens', 0)}  "
            f"cache-read={usage.get('cache_read_tokens', 0)}"
        )
        bottlenecks = latest_run.get("bottlenecks", [])
        if bottlenecks:
            lines.append(f"  Signals: {', '.join(bottlenecks)}")
        for tool in latest_run.get("slow_tools", [])[:3]:
            lines.append(
                f"  Tool: {tool['name']}  {tool['elapsed_seconds']:.2f}s  [{tool['status']}]"
            )
        for change in latest_run.get("schema_changes", [])[:3]:
            lines.append(f"  Request step {change['step']} changed: {', '.join(change['changed'])}")
        if latest_run.get("model_context_host_seconds_estimate") is not None:
            lines.append(
                "  Non-tool time estimate: "
                f"{latest_run['model_context_host_seconds_estimate']:.2f}s "
                "(model + context + host; parallel tool time may overlap)"
            )
    legacy = payload.get("legacy_overrides", {})
    active_legacy = [name for name, enabled in legacy.items() if enabled]
    if active_legacy:
        lines.append(f"Deprecated overrides: {', '.join(active_legacy)}")
    return "\n".join(lines)
