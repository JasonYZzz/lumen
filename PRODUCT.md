# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary audience for the Architecture Atlas is inferred from the repository and this task: maintainers who need to change Lumen without creating a second runtime authority. A secondary audience is framework and platform engineers evaluating how Lumen can be integrated or extended. This audience ordering remains open for explicit user confirmation.

## Product Purpose

Lumen is a general, configurable Agent framework. Model providers are replaceable Implementations; Lumen owns Session history, context, tools, approvals, Sandbox execution, Work Products, Realtime calls, and multi-Agent lifecycle. Success means those capabilities remain understandable, auditable, recoverable, and consistent across TUI, Web, and headless Adapters.

The Architecture Atlas explains the current Implementation from source-backed facts. It should let a maintainer identify the authoritative Module and the correct Seam before editing code, and let an evaluator understand the runtime flow, safety guarantees, compatibility policy, and extension model.

## Positioning

Lumen keeps control-plane and durable state authority inside its own deep Modules while allowing provider, client, transport, tool, and storage Adapters to vary. Its documentation is generated and audited against runtime contracts instead of treating historical plans as shipped behavior.

## Operating Context

Readers move between the offline Architecture Atlas, Markdown decision records, Python source, contract tests, generated OpenAPI/TypeScript contracts, and the generated runtime contract catalog. The Atlas must work without a server or external dependency, remain printable, and make source paths and decision records easy to reach.

## Capabilities and Constraints

- Python 3.11–3.13; distribution, package, and CLI names are `lumen-agent` / `lumen`.
- The Architecture Atlas is a static HTML/CSS/JavaScript Read surface under `docs/architecture-guide/`.
- Source, contract tests, and current schema outrank prose documentation.
- Session is an append-only v9 journal with read compatibility for v1–v8.
- Configuration schema is v2; v1 migration is memory-only and does not rewrite user files.
- The page must preserve offline search, keyboard access, responsive reading, print output, and direct source links.
- No customer, benchmark, deployment, pricing, or compatibility claims may be invented.

## Brand Commitments

The confirmed name is Lumen. Existing terminology is binding: Module, Interface, Implementation, Seam, Adapter, Depth, Leverage, and Locality. The documentation voice is precise, implementation-grounded, bilingual where technical English improves source navigation, and explicit about uncertainty or compatibility.

## Evidence on Hand

- Accepted decisions: `docs/architecture-guide/10-native-multi-agent-runtime.md` and `11-realtime-voice-runtime.md`.
- Runtime authorities: `src/lumen/application/host.py`, `run_coordinator.py`, `runtime.py`, `context/`, `work_products/`, `agents/`, `live/`, `tools/`, and `sessions.py`.
- Generated contract evidence: `docs/generated/contracts.json`.
- Contract suites under `tests/` and Web contracts under `src/web/`.
- Existing Lumen mark and architecture imagery under `docs/architecture-guide/`.

## Product Principles

1. One state concept has one runtime authority.
2. Explain behavior through the smallest authoritative Interface and the complex Implementation it hides.
3. Keep compatibility, security, failure, and recovery paths visible beside the happy path.
4. Prefer generated or tested evidence over manually repeated claims.
5. Make the correct code entry point obvious before the reader changes anything.

## Accessibility & Inclusion

The Atlas must support keyboard-only navigation, visible focus, reduced motion and transparency preferences, semantic headings, sufficient contrast, mobile reflow, and legible print output. Chinese prose and English identifiers must remain readable together without relying on color alone.
