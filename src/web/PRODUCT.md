# Lumen Web

Lumen is a local, configurable personal assistant and agent framework. Web, desktop, TUI and
headless clients share WorkspaceHost; the browser never owns execution or permission state.

Browser authentication has its own recovery state. Refresh and CLI server restarts retain login
for the same workspace and endpoint; missing credentials show a Chinese reconnect view rather
than a failed conversation. Reconnection preserves drafts and never resubmits user operations.

Conversation activity summarizes consecutive successful tool receipts between narration into
expandable rows, retaining every receipt and result. Tool families supply the action icons;
running statuses shimmer, while completed, cancelled and approval states stay static. Reduced
motion and forced colors retain readable status text without shimmer.

The v2 interface extends the current Lumen product with five main entries: 对话、信息流、灵感、
目标、资料空间. Activity and ongoing follow-ups have a global header entry, with goal-specific
controls in goal details. Personal preferences and connections belong in Settings; there is
no standalone Memory button. The approved 2026-10-22 Web identity upgrade uses a pink-snow
canvas, white reading surfaces and muted plum actions (#FAF7F8 / #302B33 / #795271 / #F2E9F1),
preserving Geist and the existing workflows. The Web corners and favicon use the original
Lumen Fold L; the welcome companion uses five regenerated transparent nine-tail fox forms
based on the supplied reference. Dense controls use solid surfaces. Architecture Atlas, TUI,
native startup and app icons retain their Fold identity.

The user approved the runnable r2 design in `../../design/lumen-v2/`. That prototype supplies
composition and interactions, not production facts. Real pages use Host APIs and saved state;
empty installations show empty states, never synthetic mail, travel or successful connections.

Work moves from intent to an associated discussion, real execution, automatically collected files
or an explicitly applied page revision, and optional ongoing follow-up. A goal needs an intent, not a file or a
prewritten plan. Creation asks only for a title and optional explanation. “创建并规划” saves first,
then sends one ordinary planning run in the fixed associated session. Original descriptions,
source conversations and existing drafts survive. Failure after saving keeps the goal and offers
planning retry with the same request identity; refresh never auto-starts a new run. A late response
does not override subsequent navigation. Editing remains “保存修改”. Continue opens a draft
in the fixed associated session; the user sends it to continue execution. Goal details expose
user-confirmed milestones, actual output files, reference materials and collapsed optional follow-up
controls. Editing milestones preserves their identities and completion states; basic edits retain
existing follow-up associations without enabling them. Ordinary questions do not
automatically create pages, and writing “every day” does not enable a schedule.

Creating a goal from a conversation retains that original conversation, constraints and history.
Durable goal status (unfinished/completed/archived) is separate from advancement (not started,
active, paused, blocked, turn budget reached, awaiting review, accepted). An explicit start asks
for verifiable success criteria and an optional total turn limit. The Host continues through
the existing Coordinator only after the prior turn and results are persisted and the execution
seat is released. Actual successful tool receipts support progress reports; no-progress loops,
missing reports, failed turns and pending messages stop continuation. Submitting results for
review does not mark the goal completed. The user accepts it; enabled follow-ups require an
explicit pause/keep choice when completing or archiving. Pause cancels queued follow-ups,
while already admitted work must first finish or be stopped. Restart never resumes goal work
implicitly. Deleting a goal retains its original discussion and removes its automatic context
projection until restoration. No extra approval or new capability comes from Goal mode.

Composer model and thinking effort share one trigger and one anchored panel. The panel opens on
the current model and its supported effort stops; its model row opens a searchable list in place.
Selecting a model refreshes its capabilities and returns to settings in the same panel. Dragging
previews effort, release saves; reset returns to provider default. Model names use configured model
IDs in both the trigger and list; differing aliases remain visible as secondary text and remain
the selection keys. Search matches IDs and aliases. The Kimi Code example uses `k3.1-pro` for both
its configuration name and model slug. Unknown defaults never acquire an invented stop.
A queued model hides the old model's effort
controls until switching finishes. Unsupported effort never disables model selection. Thinking
still applies to the next turn through Session settings. Mobile keeps the combined trigger,
independent approval and add/send actions on one row, with long names truncated and full names
available in the panel. Save errors remain retryable; closing during a save never reopens the panel.

Composer permissions combine access scope and approval in four presets: 每次确认、自动改文件、
工作区内自动执行 and 完全访问. A persistent scope label distinguishes workspace, configured extra
directories and local-computer access. Full access requires an explicit confirmation; Host saves
the session scope and approval together without changing workspace defaults or other sessions.
Scope changes wait for execution, active agents and Live connections to end. Resume restores the
saved session preset; a fork narrows access to the workspace. Built-in Git commit/push still require
fresh approval. Unrestricted commands have the current user's file and network access. A lost save
response is reconciled from a fresh snapshot; if that fails, execution waits for reconnection.
The existing approval-only API and /mode commands continue to preserve the current scope.

Fox growth is an independent browser appearance preference. The welcome places a compact
five-stop native “外观” slider directly below the composer; no appearance menu remains in Settings.
樱粉 (default), 杏白, 薄荷, 雾蓝 and 浅紫 pair light page/side surfaces with the five fox forms
from juvenile to mature. The current color is visible; assistive technology also reads the position.
`src/components/mascot/fox-growth.ts` defines the choices; `appearance-control.tsx` owns the
local preference. Changes apply immediately and save the color ID in `lumen.appearance`;
reload restores it and other tabs synchronize it. Invalid values fall back to the base pink
appearance; unavailable storage leaves the control usable with an honest notice. Model,
reasoning and Session settings never select a form, and changing appearance sends no runtime
mutation. The model/effort panel contains no fox. New images preload before a 320ms crossfade;
a failed load retains the current image and a newer choice cancels stale callbacks. Static,
reduced and inactive views do not animate. The two corner L marks and favicon remain independent of the form;
empty states use the base fox. The thinking icon uses `lumen-claude-amber.svg` with its original
breathing motion, disabled for reduced motion. Form changes do not represent model load,
ability, task progress or accumulated experience. The centered welcome uses native 1254px
regenerated sources with lossless responsive assets, quiet breathing, a base-form eye blink,
pointer attention and a brief greeting. Reduced/static and hidden/offscreen modes stop motion.
Short screens can scroll the complete welcome and composer. The original input PNGs remain;
earlier WebP and video/layered/Rive/three.js alternatives are archived in ignored local backups.

Ideas are explainable rules over unfinished goals and visible document results. Active advancement
is not proposed again; awaiting review opens the goal, and an unvalidated follow-up opens its existing
configuration. Generic starters are labeled and require a scope. Each task proposal shows its source,
steps and expected output before an explicit Start task submits a real ordinary Host run. Goal/resource
ideas reuse their discussion and all its context references; revisions and withdrawn suggestions are
checked at launch. Failed admissions retain the proposal and request identity for retry. Ideas do not
silently enable schedules, accept goals, expand permissions, or run a model merely to populate the page.

The material space collects verified Work Product outputs and newly generated local files linked
in a completed answer. Files keep their actual bytes, type, source conversation and version;
associated goals receive output links automatically. Ordinary conversation text is not a file.
The material space also supports blank pages, original-file imports up to 20 MiB, collections,
favorites, saved / hidden state, indexed-content search and versioned edits. Type filters, list
and grid views share the same query. The default 3D shelf shares that query too: real resources become
clothbound volumes with hover, selection, front/back flipping and drag rotation from the registered
Ashen Press source. Its original source has no individual-page animation. “打开资料” reads the existing
resource through its format-aware viewer. The shelf shows up to ten items at a time and pages through
loaded results; grid/list remain available when WebGL or the source's external dependency cannot load.
No example books substitute for missing resources. Goal, feed and conversation links open the same resource identity.
Goals support creation, editing, status changes, archive, deletion and restoration, with search
and state filters. Per-row menus expose management without opening the conversation. New goals
start their associated planning conversation after saving; continuous goal advancement and
scheduled follow-up each require their own explicit start.
Resources support display-name changes, page-content edits, collection/goal organization,
deletion and restoration. File content is modified through its original conversation and validation
boundary. Renaming changes metadata, not the workspace path. Deletion moves goals and resources
to a recycle bin; original files, conversations, versions and associations remain recoverable.
Feed queries hide entries whose associated goal or resource is missing or in the recycle bin,
before pagination and category filtering. Restoring all targets restores the original notification.
Deleted goals stay out of the feed aside and suggestions; new follow-up results do not link to them.
New files and changed content versions create idempotent feed events during collection, pointing
to the exact resource revision. Unchanged reads, metadata-only renames, ordinary answers and trials
do not create successful output notices. Goal review, blocked and turn-limit states create actionable
events; resolved or superseded events leave the attention filter while remaining in history.
“查看本次成果” opens that fixed version; “围绕成果讨论” carries the same resource reference.
Deleting a collection removes its grouping only. Optimistic revision checks prevent stale edits.
Desktop resource discussion places content beside conversation; mobile lets people switch
between them. Leaving resource content for a new or existing ordinary conversation keeps that
conversation visible, and changing sections clears the previous resource notice. Deep links,
discussion drafts and list view state support returning to work.

Files need no extra save action. Applying an AI reply to an existing page remains explicit.
Version conflicts preserve the current resource; historical file and page versions remain readable. Work
Products retain their existing workspace validation boundary instead of using generic page edits.

Ongoing follow-ups support local workspace text snapshots and restricted read-only research,
finite background execution, explicit trial / enable, durable results and recoverable failures.
Research needs a configured built-in search capability and explicit permission for background
reads; a model's native search or an MCP connection does not supply that capability. The Host
collects bounded search summaries and optional page excerpts before the model summarizes them.
Results arrive in the in-app feed. No email or other external sends are represented as available.
Normal interactive capabilities remain available through their existing approvals. The installed
provider is replaceable.

Runtime contracts and acceptance requirements live in the repository AGENTS.md and implementation
guide at `../../docs/architecture-guide/16-personal-assistant-automations.md`. Section 12 of
`../../docs/plans/2026-10-21-lumen-v2-product-flow-upgrade.md` records implementation choices;
earlier sections preserve the proposal. The four issues in the earlier code review are fixed
with regressions; current validation and remaining release gates are recorded in
`../../docs/research/2026-10-21-v2-product-flow-validation.md`. Component tests, screenshots and
a configured-model smoke do not establish live web research, all-provider, cross-platform CI,
device, long-running scheduler or power-loss acceptance.
The 2026-10-22 workflow and visual upgrade is recorded in
`../../docs/research/2026-10-22-muse-ui-upgrade-validation.md`; its current create-and-plan behavior
supersedes the older save-only goal creation record. Root PRODUCT.md / DESIGN.md describe the
Architecture Atlas and must not be overwritten.
The Web identity assets, mapping, backup boundary and validation entry points are documented in
`../../docs/brand/mascot/README.md`.
