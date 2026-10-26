# Lumen v2 product-flow documenter

Date: 2026-10-21. Scope: app-level documentation after the goal and material-space implementation.
Read the complete Impeccable `reference/document.md` before review. This is an incumbent-system
extension, with the existing product and visual commitments preserved.

Paths written:

- `src/web/PRODUCT.md`
- `src/web/DESIGN.md`
- `.impeccable/review/lumen-v2-product-flow-2026-10-21/documenter-report.md`

Five-line system summary:

1. Identity: the familiar neutral Lumen workspace retains Geist, the original logo and authored animated nine-tail companion.
2. Color and type: existing page, ink, muted and boundary tokens remain authoritative; no palette, font or token values changed.
3. Form and depth: house controls, restrained strokes, flat reading surfaces and existing temporary-layer depth remain the system.
4. Layout: five main entries and global activity support goal actions, shared-query resource list/grid, desktop content/discussion columns and a mobile content/discussion switch.
5. Interaction: object identity, explicit result/version actions, visible recovery and ordinary-chat visibility carry the work from intent to optional follow-up.

`PRODUCT.md` now describes implemented intent-only goals, associated sessions, explicit execution
and publication, resource versions and organization, and bounded background research. It replaces
the obsolete four-open-review-issues statement with the current implementation/validation sources.
It also records the two final navigation fixes: leaving mobile resource content preserves new and
existing ordinary conversations, and section changes clear the prior resource notice.

`DESIGN.md` retains its frontmatter, North Star, visual identity, primitives and authored assets.
Only stale layout and navigation facts changed: 资料空间 naming, global activity, list/grid,
desktop discussion columns and the mobile switch. No root PRODUCT/DESIGN, runtime code, asset or
sidecar was written. The app sidecar inspected at `src/web/.impeccable/design.json` is already
schemaVersion 2; no root `.impeccable/design.json` was present, and no schema migration was needed.

Evidence reviewed: `lumen-app.tsx`, `assistant-workspace.tsx`, `assistant-workspace.css`, global CSS,
the accepted architecture chapter 16, implementation plan section 12 and current validation record.
All seven supplied screenshots were inspected: 01 goal desktop, 02 resource discussion desktop,
03 discussion mobile, 04 resource mobile, 06 goal mobile, 07 content-to-new-chat and 08
content-to-existing-chat. The final two show a visible ordinary conversation/composer after leaving
resource content; source resets and resource-scoped mobile selectors support that observation.
The screenshot model output is fixture evidence of product interaction, not live research evidence.

Documentation validation: checked cited local paths, canonical DESIGN section order, unchanged
frontmatter and whitespace. No implementation test, browser rerun, external CI, real mobile device
or live search was executed by this documenter; broader results remain attributed to the validation
record and their stated scope.

Uncanonized pre-existing drift: global `--sidebar` is `#f5f5f3` while the rendered sidebar uses literal
`#fafafa`; the existing DESIGN explanation remains, and this pass does not standardize that source.
