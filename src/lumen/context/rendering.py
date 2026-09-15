"""Deterministic rendering for transient provider context.

Provider roles remain the authority. Shallow XML carries provenance, trust,
and revision metadata; Markdown remains the human-readable body format. Every
dynamic value is escaped as XML text or an XML attribute, so source content
cannot close an envelope or forge a higher-trust sibling element.
"""

from __future__ import annotations

from collections.abc import Sequence
from xml.sax.saxutils import escape, quoteattr

from lumen.context.types import ContextBlock, ContextZone


def _attr(value: object) -> str:
    return quoteattr(str(value), entities={"\n": "&#10;", "\r": "&#13;", "\t": "&#9;"})


def _text(value: object) -> str:
    return escape(str(value), entities={'"': "&quot;", "'": "&apos;"})


def render_session_policy_context(blocks: Sequence[ContextBlock]) -> str:
    """Render task state and active Skill snapshots for a system-role part."""

    lines = ['<session-policy-context version="1">']
    for block in blocks:
        if block.source.origin == "runtime:skill-catalog" and block.payload.text:
            lines.extend([
                '  <available_skills trust="system" format="text">',
                _text(block.payload.text),
                "  </available_skills>",
            ])
        if block.zone is ContextZone.RUNTIME_CONTEXT and block.payload.text:
            lines.extend(
                [
                    '  <runtime-context trust="system" format="text">',
                    _text(block.payload.text),
                    "  </runtime-context>",
                ]
            )
        if block.zone is ContextZone.TASK_STATE and block.payload.text:
            lines.extend(
                [
                    '  <task-state trust="system" format="markdown">',
                    _text(block.payload.text),
                    "  </task-state>",
                ]
            )
    skills = [block for block in blocks if block.zone is ContextZone.ACTIVE_SKILLS]
    if skills:
        lines.append('  <active-skills trust="policy">')
        for block in skills:
            details = block.payload.structured or {}
            name = details.get("name", block.source.origin.removeprefix("skill:"))
            source = details.get("source", block.source.origin)
            revision = details.get("revision", block.source.revision or "unknown")
            lines.append(
                "    <skill"
                f" name={_attr(name)} revision={_attr(revision)} source={_attr(source)}"
                ' format="markdown">'
            )
            lines.append(_text(block.payload.text or ""))
            lines.append("    </skill>")
        lines.append("  </active-skills>")
    lines.append("</session-policy-context>")
    return "\n".join(lines) if len(lines) > 2 else ""


def render_context_data(blocks: Sequence[ContextBlock]) -> str:
    """Render memory and retrieved resources for a transient user-role part."""

    memory_index = [b for b in blocks if b.zone is ContextZone.MEMORY_INDEX and b.payload.text]
    recalled = [b for b in blocks if b.zone is ContextZone.RECALLED_MEMORY and b.payload.text]
    retrieved = [b for b in blocks if b.zone is ContextZone.RETRIEVED_CONTEXT and b.payload.text]
    if not memory_index and not recalled and not retrieved:
        return ""
    lines = ['<context-data version="1">']
    if memory_index or recalled:
        lines.append('  <memory-context trust="low">')
        for block in memory_index:
            lines.extend(
                ['    <memory-index format="markdown">', _text(block.payload.text), "    </memory-index>"]
            )
        for block in recalled:
            lines.extend(
                [
                    '    <recalled-memory format="markdown">',
                    _text(block.payload.text),
                    "    </recalled-memory>",
                ]
            )
        lines.append("  </memory-context>")
    if retrieved:
        lines.append('  <retrieved-context trust="untrusted-external">')
        for block in retrieved:
            details = block.payload.structured or {}
            lines.append(
                "    <document"
                f" server={_attr(details.get('server', 'unknown'))}"
                f" uri={_attr(details.get('uri', block.source.origin))}"
                f" revision={_attr(details.get('revision', block.source.revision or 'unknown'))}"
                ' format="markdown">'
            )
            lines.append(_text(block.payload.text))
            lines.append("    </document>")
        lines.append("  </retrieved-context>")
    lines.append("</context-data>")
    return "\n".join(lines)


def render_history_summary(markdown: str) -> str:
    """Wrap a structured compaction summary with explicit recalled trust."""

    return (
        '<history-summary version="1" trust="recalled" format="markdown">\n'
        f"{_text(markdown)}\n"
        "</history-summary>"
    )


def render_mcp_prompt(*, reference: str, body: str) -> str:
    """Wrap a user-selected external template without granting authority."""

    return (
        '<mcp-rendered-prompt trust="untrusted-external" '
        f'source={_attr(reference)} format="markdown">\n'
        f"{_text(body)}\n"
        "</mcp-rendered-prompt>"
    )


__all__ = [
    "render_context_data",
    "render_history_summary",
    "render_mcp_prompt",
    "render_session_policy_context",
]
