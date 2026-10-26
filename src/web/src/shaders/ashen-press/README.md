# Lumen 资料书架

Upstream: [ThreeUI Ashen Press source bundle](https://threeui.com/source-code/ashen-press.json),
retrieved 2026-10-26, revision `5fe2554e578a`.

`sources/ashen-press.html` is the complete, unchanged registered source, SHA-256
`5fe2554e578acac5d55cb466a9564440e7767e38797981f8e829dfd2de0bc90f`.
The shared `../threeui.css` is unchanged, SHA-256
`efe4447139f1358dd8e9be68edf6fa46cbefbd1de423a4d6c439ca61d2c8eccf`.
The original registered `AshenPress.tsx` hash is
`36b365f7a69223f1c3347bfcb3f2d1aa1ab20d30f2c31e5efb248e62ab4ddc90`;
its local wrapper adds an optional source document and a frame-scoped message interface.

`library-source.ts` derives the Lumen document from that exact HTML. It changes the catalogue,
cover/back-cover facts, Chinese interface copy and theme colors, while preserving the source's
geometry, GLSL, wood grain and reflections, contact shadows, raycasting, springs, camera,
background blur, torn-paper reveal and intro. Incomplete rows are centered. Mobile retains the
original stacked catalogue and adds dragging for the selected volume. The authored flip rotates
the whole volume between front and back; the upstream source has no animated individual pages.
“打开资料” opens the existing format-aware resource viewer for the full content.

`components/library-scene.tsx` supplies at most ten actual resources from the current query and
pages through the loaded results. The upstream example games, prices and barcode do not appear
in the Lumen catalogue. Resource metadata is serialized safely and rendered as text; no resource
HTML executes in the scene. The frame retains `sandbox="allow-scripts"` without same-origin
access. Only messages from that frame can request opening, and only a displayed resource ID is
accepted. Opening still uses the existing AssistantWorkspace flow. Grid and list views remain
available, including when WebGL or an external dependency fails; the recycle bin uses those views.

The registered import map loads Three.js r181 from `unpkg.com`; registered fonts load from Google
Fonts. Their URLs remain unchanged. This scene therefore needs network access on a cold cache.
The canonical illustrated game plates remain archived inside the registered HTML but are removed
from the derived Lumen document. Covers use actual resource titles and types; they are not a
rendering of the file's pages. Upstream reduced-motion behavior is retained. Leaving the view,
scrolling it out of view or hiding the browser removes the iframe and its WebGL context.

Validation: source hash/shader and injection checks in `library-source.test.ts`, frame-message,
pagination and failure checks in `components/library-scene.test.tsx`, and shared Library workflows
in `components/assistant-workspace.test.tsx`. Browser acceptance covers selection, flipping,
dragging, returning the volume, opening the real file and the mobile stack. These checks do not
establish low-end GPU or physical-device performance.
