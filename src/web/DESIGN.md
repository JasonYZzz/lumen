---
name: Lumen Web
description: A snow-fox workspace for real progress, inspectable results, and the next action.
colors:
  page: "#faf7f8"
  surface: "#ffffff"
  sidebar: "#f3edf1"
  surface-soft: "#f2e9f1"
  accent: "#795271"
  accent-hover: "#62405c"
  ink: "#302b33"
  ink-soft: "#5e5360"
  muted: "#706271"
  faint: "#8a8a86"
  line: "#e6dce4"
  line-strong: "#c9b8c6"
  danger: "#a63f4d"
  appearance-pink: "#faf7f8"
  appearance-apricot: "#fdf5eb"
  appearance-mint: "#eff8f4"
  appearance-blue: "#f0f6fc"
  appearance-lilac: "#f5f1fb"
  appearance-apricot-sidebar: "#f5ebdf"
  appearance-apricot-surface-soft: "#f6ebdd"
  appearance-apricot-line: "#e9ddcf"
  appearance-apricot-line-strong: "#cfbda6"
  appearance-mint-sidebar: "#e7f0ea"
  appearance-mint-surface-soft: "#e1efe7"
  appearance-mint-line: "#d5e5dc"
  appearance-mint-line-strong: "#aec7b7"
  appearance-blue-sidebar: "#e7eef6"
  appearance-blue-surface-soft: "#e0ebf7"
  appearance-blue-line: "#d5e1ef"
  appearance-blue-line-strong: "#afc1d8"
  appearance-lilac-sidebar: "#eee8f6"
  appearance-lilac-surface-soft: "#eae1f5"
  appearance-lilac-line: "#e1d6ee"
  appearance-lilac-line-strong: "#c2afd7"
  attention-ink: "#79531d"
  attention-bg: "#f6ecd9"
  attention-surface: "#fffdf7"
  failure-bg: "#fff0ee"
  excerpt: "#f8f8f3"
typography:
  headline:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "27px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "-0.02em"
  title:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.5
  body:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.75
  label:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "13px"
    fontWeight: 400
  metadata:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6
  resource:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
    fontWeight: 450
  composer:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "16px"
    fontWeight: 430
    lineHeight: 1.55
  welcome:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "clamp(22px, 2.1vw, 28px)"
    fontWeight: 550
    lineHeight: 1.5
    letterSpacing: "-0.02em"
rounded:
  status: "6px"
  field: "8px"
  compact: "9px"
  control: "10px"
  inset: "12px"
  dialog-small: "16px"
  card: "18px"
  result: "20px"
  panel: "22px"
  composer: "24px"
  pill: "999px"
spacing:
  micro: "4px"
  tight: "8px"
  control: "12px"
  compact: "16px"
  form: "18px"
  regular: "20px"
  section: "24px"
  card: "28px"
  panel: "32px"
  page: "40px"
  wide: "48px"
  page-bottom: "64px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
  field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    padding: "10px 12px"
  nav-active:
    backgroundColor: "{colors.surface-soft}"
    textColor: "{colors.accent}"
    rounded: "{rounded.control}"
    padding: "9px 12px"
  filter-active:
    backgroundColor: "{colors.surface-soft}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "8px 16px"
  status:
    backgroundColor: "{colors.surface-soft}"
    textColor: "{colors.accent}"
    typography: "{typography.metadata}"
    rounded: "{rounded.status}"
    padding: "4px 9px"
  feed-card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.result}"
    padding: "28px"
  goal-card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "26px"
  goal-result:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "0"
    padding: "14px 0"
  library-card:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "0"
    padding: "0 0 16px"
  idea-workbench:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "28px"
  idea-plan:
    backgroundColor: "{colors.surface-soft}"
    textColor: "{colors.ink-soft}"
    rounded: "{rounded.inset}"
    padding: "20px"
  notice:
    backgroundColor: "{colors.surface-soft}"
    textColor: "{colors.ink-soft}"
    rounded: "{rounded.control}"
    padding: "14px 18px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "32px"
  composer:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.composer}"
    rounded: "{rounded.composer}"
---

# Design System: Lumen Web

## Overview

**Creative North Star: "The snow-fox Lumen workspace"**

Lumen is a calm place to see what is progressing, read a real result, and choose the next action. Pink-snow canvas surrounds white reading surfaces; muted plum identifies the current destination and deliberate primary actions. Bundled Geist, the original Fold L marks and a regenerated transparent nine-tail snow fox give the Web client its identity.

The existing workflows make work legible before adding context. Results, source and version identity, explicit state, and recovery actions carry the meaning. The fox stays a quiet companion whose five forms follow an independent light-background preference. Dense controls use solid surfaces and short transitions.

**Key Characteristics:**

- Pink-snow canvas, white reading surfaces, fine boundaries, and muted plum actions.
- One Geist family with Chinese system fallbacks.
- Clear work status, real output previews, and one visible next action.
- Optional context collapses on narrow screens while results remain visible.
- Five regenerated fox forms share one character identity and nine separately visible tail tips.

This app-scoped specification refreshes production CSS and components after the approved 2026-10-22 Web identity upgrade. It does not replace root DESIGN.md or PRODUCT.md, which describe Architecture Atlas, or the Fold assets used by TUI and native startup/app icons. Source styles remain authoritative for component details; this document records the reusable vocabulary, not every legacy literal.

## Colors

The palette uses pink-snow neutrals for reading and muted plum for action. Frontmatter owns the color values.

### Primary

- **Muted plum** (`accent`): assistant primary actions, active navigation, card-kind labels, progress indicators, and keyboard focus. **Deep plum** (`accent-hover`) is the primary action's hover state.
- **Lumen ink** (`ink`): headings, body emphasis, tab underlines, and input caret or native accent treatments. Retained legacy controls may still use ink-filled actions.
- The regenerated fox keeps its source colors and transparent background. Fold L marks keep their local amber/gold identity colors.

### Secondary

- **Attention amber** (`attention-ink`, `attention-bg`, `attention-surface`): written blocked, review, budget, or user-action states and their quiet reading surface.
- **Failure red** (`danger`, `failure-bg`): explicit failure or destructive-action text with a pale failure badge. Color always accompanies words.

### Neutral

- **Pink-snow canvas** (`page`): application background and space between reading regions.
- **Reading white** (`surface`): header, result cards, goal cards, library paper previews, assistant fields, and panels.
- **Rose paper** (`sidebar`): persistent navigation and the feed's auxiliary context.
- **Pale lilac** (`surface-soft`): selected navigation, contextual notices, task plans, and thumbnail covers.
- **Secondary ink / muted plum-gray** (`ink-soft`, `muted`): descriptions, dates, helper text, and source metadata; the muted token retains WCAG AA normal-text contrast on the four main surfaces.
- **Decorative gray** (`faint`): nonessential separators or decoration only; never small body text or an icon that alone conveys meaning.
- **Fine / strong lilac line** (`line`, `line-strong`): surface boundaries and control strokes.
- **Excerpt paper** (`excerpt`): real text previews in resource thumbnails.
- **Five light appearances:** 樱粉 `#faf7f8`, 杏白 `#fdf5eb`, 薄荷 `#eff8f4`, 雾蓝 `#f0f6fc`, 浅紫 `#f5f1fb`. Each coordinates page, sidebar, soft surface and boundaries through `data-appearance`; white reading panels, dark text and plum actions remain consistent. Appearance changes use a 320ms background transition, removed with reduced motion.

**The One Action Accent Rule.** Use muted plum for deliberate primary actions and current navigation; preserve the fox's authored colors.

**The State Has Words Rule.** Color never replaces execution state, source coverage, version identity, or recovery text.

## Typography

**Display / Body Font:** Geist, bundled through `geist/font/sans`, with PingFang SC, Microsoft YaHei, and sans-serif fallbacks. Production uses `--font-geist-sans`.

One family serves navigation, reading, and controls. Size and weight establish hierarchy without a new display face or decorative typography.

### Hierarchy

- **Headline:** page headings use `headline`; compact pages and the library heading use 25px.
- **Title:** feed headings use `title`; ordinary assistant subheadings use 18px at weight 600 and line-height 1.5. Idea headings use 19px; goal title actions use 17px at weight 550. Panels use 21px; goal-detail headings use 24px at line-height 1.4.
- **Body:** explanations use `body`; ordinary assistant fields use 14px. Transcript text follows the existing `--transcript-size` setting, whose default is 16px.
- **Label:** actions use `label`; desktop navigation uses 14px, form labels use 13px at weight 500, and mobile navigation uses 11px with visible destination names.
- **Metadata:** status, dates, and footnotes use `metadata`; timestamps use tabular numerals. Library source labels and cover labels have compact 11–12px treatments.
- **Resource:** library filenames wrap below their preview and retain the full name in the detail view.
- **Composer:** the existing overlay editor uses `composer`; textarea and highlight layer retain matching metrics. The centered welcome greeting uses `welcome`, with a responsive 22–28px size at weight 550 and line-height 1.5.
- **Appearance selector:** standard settings headings, 16px current color, and 13px color choices and notes. Five positions communicate the explicit selection order, without fantasy stage names.

## Layout

The desktop shell fills the available width, uses a 248px sidebar collapsible to a 52px rail, and has a header with a 58px minimum height. The shell, main region and nested reading panes preserve `min-width: 0`, bounded scrolling, wrapping actions, and readable content at narrow widths.

Assistant pages use 40px top padding, at least 32px side padding with a centered 1280px rail, and 64px bottom padding. The feed combines a flexible reading stream with a 260px auxiliary column and a 32px gap. Result cards group metadata, title, written state, summary, preview, and actions on white. A result with a preview uses a 168px preview column; this becomes 88px on compact screens.

Goals use a two-column grid with a 24px gap and a 1200px maximum width. Goal details use a white reading region capped at 900px, with 28px 32px padding, section separators, a two-column result list, and collapsed optional follow-ups. Ideas use white workbenches capped at 1120px; the explanation stays within 68ch and expands an inset task plan.

The library shares search and create controls beside its heading, followed immediately by type filters and view controls above a fine separator. The default 3D shelf is the actual catalogue, using the registered Ashen Press source with Lumen titles, types, versions, theme colors and Chinese actions. It retains the authored oak furniture and reflections, clothbound volumes, hover lift, selection, front/back flip, drag rotation, background blur and torn-paper detail reveal. At most ten actual resources occupy a shelf; paging covers further loaded results. No example games or invented files fill empty slots. The original mobile stack and reduced-motion path remain; the selected mobile volume also supports dragging. Opening a volume's resource uses the existing viewer, while grid and list remain directly selectable, including after scene-loading failure. The recycle bin uses grid/list. This source flips a whole book, not individual content pages. The canonical source is preserved; adaptation and provenance are documented in `src/shaders/ashen-press/README.md`.

List rows use name, source, update date, and management columns; the row minimum is 66px. The grid auto-fills columns of at least 220px with 32px row and 24px column gaps. Cover stages use a 1.2 ratio on desktop and .9 on mobile; real image bytes or bounded text excerpts lead when available, otherwise a typed cover carries the resource identity. Names wrap below the cover; source, update date, favorites and management remain directly available. The 3D frame is 520–680px tall on desktop and 580px on compact screens; only this view loads its source bundle. Offscreen, hidden and unmounted scenes release their iframe rather than running a background WebGL loop.

Resource discussion places a 42% reading column, with a 280px minimum, beside conversation. A shared context bar identifies the resource and its stored version. Ordinary conversations use the existing centered 800px conversation rail. The welcome region uses a centered 760px cap with the fox, greeting and input stacked vertically. Its fox stage is 240px square, reducing to 168px on mobile and 144px at viewport heights up to 700px. The landing region scrolls vertically, and its entry does not shrink, so the whole composer stays reachable on short screens.

| Implemented breakpoint | Behavior |
| --- | --- |
| At least 1500px | Page padding becomes 48px with at least 48px sides; feed gap becomes 48px. |
| At most 1050px | Feed and goals become one column. The optional feed context becomes a single collapsed summary above the stream; its repeated inner heading is hidden. Automation rows stack. |
| At most 900px | Library heading and toolbar stack; search expands and type filters scroll horizontally. |
| At most 800px | Shell becomes one main region with a 270px off-canvas sidebar. Five labeled bottom destinations appear; the workspace reserves 64px plus the safe-area inset. Resource discussion switches between content and conversation. |
| At most 700px | Page padding becomes 24px 20px 48px; feed cards use 20px padding, goal and idea cards use 22px, goal details use 24px 20px, panels use 22px 18px, and result/form columns stack. Assistant page and panel buttons, relevant summary actions, and header actions have a 44px minimum touch height. Goal-editor text fields use 16px. |
| At most 640px | Idea summaries and launch actions stack; plan padding becomes 16px. Welcome fox becomes 168px; existing voice focus fills the viewport. |
| At most 700px high | Welcome fox becomes 144px; entry gaps and padding tighten while the landing region remains vertically scrollable. |
| At most 600px | Library retains a two-column grid with 22px 12px gaps. List rows omit the source column, which remains available in resource details. |
| At most 480px | Combined composer model/effort trigger truncates long names and omits the provider icon/default suffix; independent approval and add/send actions remain on one row. Full names and supported effort stops remain available in the panel. |

Assistant panels are capped at 700px and `calc(100vw - 32px)`, with `calc(100dvh - 40px)` maximum height. The goal editor is capped at 560px, keeps its fields internally scrollable, and retains a visible footer action. Composer model and effort share the incumbent 360px anchored settings/search panel.

**The First Viewport Rule.** On initial entry, show the current work or latest result and its primary action. On narrow feed views, optional context begins collapsed so the first result remains visible.

## Elevation & Depth

White surfaces and lilac boundaries provide the main hierarchy. Feed, goal, and idea cards stay flat at rest. Library paper previews have a soft resting shadow and lift 4px on hover or keyboard focus; temporary menus and dialogs carry stronger depth. Dense controls remain solid.

### Shadow Vocabulary

- **Library paper:** `0 6px 16px rgb(48 43 51 / .09)`.
- **Assistant menu:** `0 8px 24px rgb(35 42 36 / .1)`.
- **Assistant panel:** `0 24px 80px rgb(35 42 36 / .14)`, over a `rgb(35 42 36 / .22)` scrim with 5px backdrop blur.
- **Existing settings / session search:** `0 24px 80px rgb(0 0 0 / 18%)`.
- **Existing runtime inspector:** `0 26px 72px rgb(25 25 22 / .2), 0 8px 22px rgb(25 25 22 / .08)`.

**The Quiet Surface Rule.** Reading surfaces stay flat at rest; hover depth is reserved for library previews and temporary layers.

## Shapes

Use soft rectangular controls and reading regions with fine 1px boundaries. Fields use the field radius; ordinary assistant buttons and navigation use the control radius. Status badges use the smaller status radius. Task plans and menus use the inset radius. Idea and goal cards use the card radius; feed cards and goal-detail surfaces use the result radius. Library metadata stays unboxed below the cover stage. Assistant panels use the panel radius, and the existing composer retains its most generous curve.

Pill filters and search distinguish query controls from rectangular actions. Feed tabs remain underlined. Library list rows and result links use separators; grid resources have open metadata below 12px cover stages, with white paper previews inside. Avoid turning every nested section into another card.

## Components

### Buttons

Assistant primary actions use muted plum with white text; secondary actions use transparent fill, ink, and a strong-line border. Both use 8px 12px padding, a 7px icon gap, and a 36px minimum desktop height. Primary hover deepens the plum; secondary hover uses pale lilac. Keyboard focus is a 2px accent outline with a 2px offset. Disabled controls use 0.45 opacity and remain unavailable. The global enabled press state moves buttons down 1px.

Controls transition briefly: global state changes use 150ms, assistant button and library-card changes use 180ms, and sidebar entry uses 200ms. Keep new state transitions within the approved 150–220ms range. Assistant reduced-motion styles remove transitions and animation; the global reduced-motion rule makes retained legacy motion effectively immediate.

### Chips and state

Library type and goal-state filters use quiet pill fills with `aria-pressed`; selected text is ink at weight 600. Feed tabs use an ink underline. Status badges pair text with pale lilac/plum, amber, or failure red according to the actual state. Goal status and execution/review status remain separate written values.

### Cards / Containers

Feed cards place a real result or actionable progress first. The primary action is “查看本次成果” or “查看目标进展”; discussion is adjacent and secondary, while execution details and management sit under More. Result links and previews open the exact resource revision represented by the event, and discussion retains that reference.

Goal cards show intent, written advancement, next action, and confirmed milestone progress. Goal details expose results before optional follow-up controls; each result row names its type and version and opens that displayed revision. Acceptance remains a deliberate user action.

Library cards and rows use actual stored content and type-specific Phosphor SVG icons. Image thumbnails load the stored revision; text previews are bounded excerpts; unavailable content falls back to its typed cover. Opening a file uses the format-aware viewer with download/details controls. Metadata renaming does not imply changing file bytes.

Conversation output uses a continuous reading surface without repeated assistant headers. Process details stay separate from the answer. Actual file results follow as compact cards, deduplicated against successful writes, with one copy/read toolbar per turn. Preserve Markdown and safe HTML preview boundaries.

Tool activity follows the supplied Codex reference: completed consecutive receipts share a muted,
expandable summary with counts and the first action's icon; each original receipt and output remains
accessible inside. Narration, reasoning, failures, cancellations and pending approvals break a group.
This reading projection does not change execution concurrency, risk or the backend's `groupable`
declaration. Declared tool families select icons before legacy name fallbacks: book for reads,
folder for directories, magnifier for searches, pencil for edits, terminal for commands, globe for
Web, plug for MCP, sparkle for Skills and wrench for other tools. Image viewing and tool discovery
use images and wrench icons. A running tool's icon also appears in the process heading; idle thinking
uses the amber SVG. Text shimmer applies only to live status and concurrent running tools or the
latest active thought, stopping on approval and turn termination. Tool icons stay steady rather than
pulsing in opacity. Completed summaries and narration
remain static. Reduced-motion and forced-color modes use readable static text.

Markdown contents use borderless, left-aligned entries that wrap long headings. Narrow reading
panels stack the contents above the article based on available panel width. Inline local file
paths display the filename and retain the resolved path in the tooltip; descriptive link text
stays intact. Resource names wrap in library rows and cards. File preview headings use the
resource display name, while downloads retain the original filename. Default assistant button
styles yield to component styles so reading links, contents and quiet actions keep their own treatment.

### Inputs / Fields

Assistant fields use white, a strong-line boundary, the field radius, ink caret, and 10px 12px padding. Keep visible labels, native controls, helper text, and vertically resizable textareas. The idea-plan textarea uses the pink-snow canvas within pale lilac. Preferences use canvas fields. Search controls use pill shells.

The original composer keeps its white shell, focus-within border shift, overlay editor, tools, and round send action. Model and effort settings remain one trigger and one panel; saving failures stay retryable.

### Navigation

The five main destinations are 对话、信息流、灵感、目标、资料空间. Desktop navigation pairs Phosphor SVG icons with text; the current page uses muted plum on pale lilac and `aria-current="page"`. Bottom navigation preserves all five visible labels and uses 50px minimum-height destination buttons plus bottom safe-area padding. The top-left brand and top-right activity entry use the original Fold L SVG; the favicon uses `/favicon.svg`. The brand lockup retains an accessible text wordmark. These marks never follow fox-form settings.

活动与持续跟进 is the global Fold-mark header action and remains available in goal details. Its status text reports current work or connection state. Preferences remain in Settings. Preserve mobile sidebar focus, close behavior, and the inert underlying regions while navigation/search overlays are open.

### Create and plan

The signature goal dialog starts with an intent title and optional explanation; milestones appear during editing after discussion. Its white panel has a scrollable field region and a separated footer. “创建并规划” saves the goal and starts one planning run in its fixed associated discussion. “保存修改” remains the edit action.

Keep original descriptions, source conversations, existing drafts, and request identity. A planning failure after saving keeps the goal visible with retry and associated-discussion actions. Retry uses the same request identity; refresh does not silently create another run. A late response does not take over later navigation. Continuous advancement and scheduled follow-up retain separate explicit starts.

### Idea workbench

The workbench explains the reason, source, and expected outcome before exposing a task plan. The pale lilac inset contains steps, a labeled scope field, and one “开始任务” action. Required scope, busy, failure, and retry states remain visible. Opening the plan does not execute it.

### Dialogs and recovery

Assistant panels reuse the modal focus hook, labelled dialog semantics, close control, bounded scrolling, and opener restoration. Goal/resource management reuses the anchored menu's keyboard navigation and Escape handling. Destructive actions explain what remains recoverable and use the existing confirmation flow.

Read and execution failures stay near the affected content with a recovery action. Loading and failed reads are distinct. These component observations and the reviewed fixture screenshots are not a full accessibility certification or proof of live provider execution.

### Snow-fox companion

The welcome mounts `MascotScene` with regenerated transparent fox assets. All five forms use the same character family with nine separately visible tail tips. Native sources are 1254×1254 PNGs; lossless WebP derivatives use the original size plus 960px, 480px and 160px widths without enlargement. The companion's `srcSet` describes the real 480px and 960px widths; its sizes match the 240px desktop and 168px mobile display. At desktop DPR2 the 480px derivative covers the display size; higher DPR can select 960px. Empty states use the base fox; corner L marks and favicon stay Fold. The 18px thinking icon uses `lumen-claude-amber.svg` with its original breathing and rocking motion; reduced motion keeps it static.

`fox-growth.ts` defines five independent appearance choices. A compact native five-stop slider below the welcome composer changes the background and fox immediately, following the short-track interaction observed at local port 3080. 樱粉, 杏白, 薄荷, 雾蓝 and 浅紫 map to forms 01–05. Default is pink/base, independent of model or reasoning settings. `useAppearance` applies the root theme, saves the color ID in `lumen.appearance`, restores on reload and synchronizes other tabs. Invalid values fall back to pink; unavailable storage keeps the control working and displays a persistence notice. Appearance requires no Host write, configuration save or task submission. Settings contains no appearance menu.

Image changes wait for the new responsive image to preload, then crossfade for 320ms. A failed preload retains the displayed image, and rapid changes cancel stale callbacks. Low-amplitude breathing (5.6s) and greeting motion (650ms, cleared by a 700ms timer) use CSS. The base form blinks every 6.8s for about 136ms using a clipped eye patch from its generated alternate frame; its full silhouette stays in place. Other forms retain breathing and greeting motion. Pointer attention leans by about ±2.5° within the target and resets on leave; focus adds a small attentive pose. These effects do not provide skeletal or independently animated tail motion.

Hidden/offscreen, reduced-motion and static modes stop motion and crossfades; the blink image is not mounted while inactive, static/reduced or transitioning. Initial image failure falls back to the base form. The supplied original PNGs remain as input references; earlier WebP and video/layered/Rive/three.js alternatives are archived in ignored local backups. Sources, full generation prompts and conversion are documented in `../../docs/brand/mascot/README.md`.

**The Appearance Rule.** Fox growth follows the selected light-background stop, independently of model, thinking strength, Session or task progress.

The welcome uses “陪你从一个想法开始” and an inline “外观” control, capped at 320px with a 44px-high native range target. A fine rail, five color ticks and a plum-ring thumb keep the control quiet; the current color sits at its right. Dragging, clicking the rail and arrow/Home/End keys update the same selection. The model/effort panel contains no companion. The standalone `/mascot-preview` displays the fox above the same control, with optional static mode and a full-resolution download. It previews locally without changing the saved appearance or Session settings.

## Do's and Don'ts

### Do:

- **Do** preserve the pink-snow palette, Geist, Fold L marks, and the regenerated fox's detail, nine-tail silhouette and transparency.
- **Do** put actual progress, a real result, and its primary action in the first viewport.
- **Do** keep resource ID and displayed revision together when opening or discussing results.
- **Do** use written status, source information, and explicit recovery actions.
- **Do** preserve draft/request identity, visible focus, safe-area spacing, bounded scrolling, and motion preferences.
- **Do** use real image/text previews with typed fallbacks for stored resources.

### Don't:

- **Don't** import the root Architecture Atlas theme into this app.
- **Don't** use decorative faint gray for body text or a meaning-bearing icon.
- **Don't** expand optional mobile context so it displaces the first result.
- **Don't** add a separate Memory navigation entry or encode state only with color.
- **Don't** replace the corner L marks with the companion, restore archived renderers, or infer model load from fox forms.
- **Don't** treat a prepared result, screenshot, or companion expression as proof of delivery or execution.
