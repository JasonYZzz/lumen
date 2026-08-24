---
name: Lumen Copper Trace Atlas
description: A source-audited architecture atlas rendered as an inspectable control circuit.
colors:
  phenolic-board: "#06130f"
  deep-board: "#04100c"
  inset-board: "#081711"
  paper: "#e6d8b8"
  paper-soft: "#b9ae91"
  paper-faint: "#9b9279"
  copper: "#e97835"
  copper-dim: "#8f4f2c"
  signal: "#73cdaa"
  fault: "#db4f5d"
  provider-violet: "#a9a1dc"
  copper-line: "rgba(233, 120, 53, 0.42)"
  paper-line: "rgba(230, 216, 184, 0.16)"
typography:
  display:
    fontFamily: "Avenir Next Condensed, Noto Sans SC, PingFang SC, sans-serif"
    fontSize: "clamp(24px, 2.3vw, 36px)"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "0.025em"
  headline:
    fontFamily: "Avenir Next Condensed, Noto Sans SC, PingFang SC, sans-serif"
    fontSize: "clamp(30px, 3vw, 48px)"
    fontWeight: 500
    lineHeight: 1.08
    letterSpacing: "-0.025em"
  title:
    fontFamily: "Avenir Next Condensed, Noto Sans SC, PingFang SC, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.2
  body:
    fontFamily: "Avenir Next, Noto Sans SC, PingFang SC, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontFamily: "SFMono-Regular, Cascadia Code, Liberation Mono, monospace"
    fontSize: "10px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0.06em"
rounded:
  square: "0"
  terminal: "50%"
spacing:
  trace: "8px"
  compact: "12px"
  panel: "20px"
  section: "30px"
  chapter: "108px"
components:
  button-primary:
    backgroundColor: "{colors.copper}"
    textColor: "{colors.phenolic-board}"
    typography: "{typography.label}"
    rounded: "{rounded.square}"
    padding: "0 14px"
    height: "38px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.paper}"
    typography: "{typography.label}"
    rounded: "{rounded.square}"
    padding: "0 14px"
    height: "38px"
  search-field:
    backgroundColor: "{colors.inset-board}"
    textColor: "{colors.paper}"
    typography: "{typography.body}"
    rounded: "{rounded.square}"
    padding: "0 12px"
    height: "40px"
  reader-control:
    backgroundColor: "transparent"
    textColor: "{colors.paper}"
    typography: "{typography.label}"
    rounded: "{rounded.square}"
    padding: "0 12px"
    minHeight: "44px"
  chapter-item:
    backgroundColor: "transparent"
    textColor: "{colors.paper-soft}"
    typography: "{typography.label}"
    rounded: "{rounded.square}"
    padding: "0 8px"
    height: "41px"
  topology-module:
    backgroundColor: "{colors.inset-board}"
    textColor: "{colors.paper}"
    rounded: "{rounded.square}"
    padding: "16px 10px"
  diagram-panel:
    backgroundColor: "{colors.phenolic-board}"
    textColor: "{colors.paper}"
    rounded: "{rounded.square}"
    padding: "20px"
---

# Design System: Lumen Copper Trace Atlas

## Overview

**Creative North Star: "The Inspectable Control Circuit"**

Copper Trace Atlas treats architecture as a circuit that can be inspected, followed, and audited: one state concept, one authoritative trace. Its world borrows the precision of a 1970s electronics service manual and a PCB photo-plot without becoming a photographed object or ornamental skeuomorph. The result is dense, flat, archival, and unmistakably a working documentation surface.

The topology is the hero. Copper routes carry execution and authority, mint marks capability and verified state, and fault red names blocked or unsafe conditions. Every expressive device also communicates structure; source paths, contract cues, line forms, labels, and textual legends keep meaning available without color. The approved reference and shipping implementation are the Copper Trace Atlas direction, seed `991d4b73`; the independent finish review disposition is ship, with fidelity accepted and no material fixes remaining.

**Key Characteristics:**

- Near-black phenolic field with paper-toned bilingual text.
- Oxidized-copper traces, calibrated hairlines, terminal dots, and square ledger panels.
- Dense topology-first composition with source evidence adjacent to architectural claims.
- Compact engineering typography with monospaced identifiers and tabular numerals.
- Flat, semantic interaction with restrained signal motion and complete static equivalents.

## Colors

The palette is a functional electronics legend: warm copper establishes authority and routes, mint confirms state, fault red interrupts unsafe paths, and paper neutrals preserve long-form legibility on the phenolic field.

### Primary

- **Oxidized Copper:** The scarce routing accent for primary actions, active chapter rails, execution paths, indices, and source-led affordances.

### Secondary

- **Signal Mint:** Verified state, capability links, focus outlines, selections, successful gates, and active terminal points.

### Tertiary

- **Fault Red:** Denial, unknown risk, blocked completion, and interrupted traces; never use it as ambient decoration.
- **Provider Violet:** A narrow semantic exception for provider-facing or externally adapted nodes.

### Neutral

- **Phenolic Board:** The canonical page field and root surface.
- **Deep Board:** The deepest scrollbar and recessed utility field.
- **Inset Board:** Nodes, search fields, and locally raised tonal panels.
- **Paper:** Primary text and high-importance labels.
- **Soft Paper:** Explanatory prose and secondary information.
- **Faint Paper:** Metadata, captions, and tertiary labels.
- **Copper Line / Paper Line:** Structural and secondary hairlines; these organize density without creating card chrome.

### Named Rules

**The One Trace Rule.** Copper identifies the authoritative path or primary action; it must not become a general-purpose highlight wash.

**The Red Means Stop Rule.** Fault red is reserved for denied, unknown, unavailable, or completion-blocking states.

**The Double-Encoding Rule.** Color never carries architecture meaning alone; pair it with a line form, label, symbol, or state word.

## Typography

**Display Font:** Avenir Next Condensed with Noto Sans SC, PingFang SC, and sans-serif fallbacks  
**Body Font:** Avenir Next with Noto Sans SC, PingFang SC, and sans-serif fallbacks  
**Label/Mono Font:** SFMono-Regular with Cascadia Code, Liberation Mono, and monospace fallbacks

**Character:** Condensed headings evoke calibrated service-manual titles while the humanist body stack keeps Chinese and English prose readable. Monospaced labels distinguish source paths, contracts, coordinates, state values, and numeric evidence from narrative text without importing an external font dependency.

### Hierarchy

- **Display:** Bold, condensed, uppercase atlas title; compact enough to keep the topology dominant.
- **Headline:** Medium-weight condensed chapter headings with tight tracking and a strong vertical rhythm.
- **Title:** Semibold condensed panel and figure titles.
- **Body:** Regular bilingual prose with a relaxed line height; explanatory passages generally stay within roughly 68–74 characters.
- **Label:** Monospaced metadata, identifiers, indices, source references, and state annotations; uppercase and tracked only for compact control labels.

### Named Rules

**The Identifier Register Rule.** Use mono for machine-facing facts and condensed sans for conceptual hierarchy; do not render long explanatory prose as terminal text.

**The Bilingual Legibility Rule.** Chinese prose and English identifiers share a hierarchy but retain appropriate fallbacks, line height, and word-breaking behavior.

## Layout

The desktop shell is a fixed top trace rail over a two-column body: a sticky chapter bus at the left and the atlas content at the right. The opening section divides into a dominant topology board and a narrow authority ledger. Content can expand to a 1640px shell, while long diagrams intentionally preserve minimum widths and scroll inside their own panels rather than compressing labels into illegibility.

Spacing follows a tight engineering rhythm: 8px trace gaps, 12px compact insets, 20px panels, 30px shell and heading gaps, and 108px chapter separation. Hairlines and alignment do most of the grouping; separate boxes are used only where the information model needs a distinct boundary.

At 1180px, the chapter bus narrows, the hero ledger moves below the topology, and high-cardinality grids reduce columns. At 760px, the chapter bus becomes a horizontally scrollable sticky strip, hero topology rows reflow into a single vertical circuit, split layouts stack, and wide technical diagrams remain locally scrollable. At 460px, action links and compact data grids become one column. Print removes navigation and actions, switches to a light paper palette, suppresses shadows, and preserves source-oriented content.

**The Topology First Rule.** The largest and earliest region must explain runtime authority or flow; never spend the first viewport on a marketing composition.

**The Local Overflow Rule.** Preserve technical labels and topology relationships by scrolling wide diagrams locally instead of shrinking type below a readable size.

## Elevation & Depth

The system is flat by default. Depth comes from tonal nesting, line weight, terminal markers, and a small set of structural shadows under fixed or sticky rails. Surfaces do not blur their backdrop, and the historically named “glass” containers resolve to near-opaque phenolic panels rather than glassmorphism. The only background gradient technique is a pair of low-contrast, hard-line grids that behaves as drafting paper, not a luminous color wash.

### Shadow Vocabulary

- **Top Rail Shadow:** A broad downward shadow separates the fixed application rail from the atlas without making it float like a card.
- **Chapter Bus Shadow:** A restrained right-and-down shadow reinforces the sticky reading rail.
- **Active Terminal Halo:** A compact mint halo confirms the current chapter marker.

### Named Rules

**The Flat Board Rule.** A resting content panel earns separation through tone, trace, and alignment; shadows belong only to persistent navigation structure or state feedback.

## Shapes

Panels, fields, buttons, nodes, and tabs are square. Borders are calibrated one-pixel hairlines, with two-pixel strokes reserved for authority or hero nodes. Small circular terminals appear only at trace endpoints and status positions; they are connectors, not a general rounded style. Corner markers and clipped trace interruptions may punctuate ledgers, but large soft radii and pill silhouettes do not belong in this world.

**The Terminal Exception Rule.** Circular geometry is reserved for endpoints, calibration marks, and state indicators; containers remain square.

## Components

### Buttons

- **Shape:** Square, compact route controls with a 38px minimum height.
- **Primary:** Copper field with phenolic text, strong label weight, and an inline route arrow. Hover shifts the field to signal mint.
- **Hover / Focus:** A restrained trace sweep may cross the primary action only when reduced motion is not requested; keyboard focus always receives a visible mint outline.
- **Secondary:** Transparent field, paper text, and a quiet paper hairline; hover follows the global mint link state.

### Cards / Containers

- **Corner Style:** Square panels with no general radius.
- **Background:** Phenolic or inset-board tonal surfaces, normally near opaque.
- **Shadow Strategy:** Flat for content; structural shadows only for the top rail and sticky chapter bus.
- **Border:** Copper or paper hairlines; authority nodes use a stronger stroke.
- **Internal Padding:** Compact topology nodes use 10–16px; general diagram and information panels use 20–24px.

### Inputs / Fields

- **Style:** The search field is an inset-board rectangle with a copper border, copper search glyph, paper text, and a visible slash shortcut badge on larger screens.
- **Focus:** A two-pixel signal-mint outline with offset; the text caret also uses signal mint.
- **State:** Empty-result search sets an invalid semantic state and reveals a dedicated no-results panel; Escape clears and exits the field.

### Navigation

The chapter bus uses numbered, monospaced rows separated by hairlines. Hover creates a faint copper tonal fill, visited terminals fill with dim copper, and the active item adds a copper edge trace plus a mint terminal and halo. Intersection-based section tracking updates the active item; on narrow screens the same navigation becomes a sticky horizontal bus without losing numbering or state.

### Topology Modules

Authority modules are square inset nodes with copper terminal markers, uppercase semantic labels, condensed titles, and monospaced implementation details. Execution, capability, event, and persistence links use distinct solid, double, dashed, or terminal-ended forms, all explained by a visible legend.

### Source Ledgers

Ledger rows align a subdued key with a brighter monospaced value and divide entries with paper hairlines. Verified summaries add mint state words or terminals and remain adjacent to their source, schema, or contract evidence.

### Trace Reader

Trace Reader is the in-page inspection pattern for Markdown, source, tests, configuration examples, and generated contracts. On desktop it unfolds from the right as a service-manual leaf while preserving Atlas context; at 760px and below it becomes a full-width modal reading plane. It is one continuous content surface, not a stack of cards. Rendered/raw tabs, document-local find, source line numbers, back history, evidence metadata, and the direct original-file escape hatch all remain available without leaving the Atlas.

The generated reader corpus is a deterministic evidence snapshot, never a second documentation authority. Source, tests, schema, generated contracts, and Accepted decisions remain authoritative; freshness checks must fail when included evidence changes without regeneration.

Reader controls use a 44px minimum touch target and explicit signal-mint `:focus-visible` treatment. Rendered/raw selection follows the ARIA tabs pattern. When the mobile reader is open, it exposes dialog semantics, makes the Atlas behind it inert, traps keyboard focus within the reader, and restores focus to the invoking control on close.

**The Current-Page Evidence Rule.** Local Markdown and source references open in Trace Reader by default so the reader keeps architectural context; modifier-click and “打开原文件” preserve direct-file navigation.

**The Snapshot Is Not Authority Rule.** A generated corpus may improve offline inspection and search, but it cannot own facts or mask drift from its authoritative inputs.

## Do's and Don'ts

### Do:

- **Do** place source paths, schemas, contracts, or tested evidence beside important architecture claims.
- **Do** use square panels, calibrated hairlines, terminal dots, and named trace forms to express topology.
- **Do** preserve keyboard search (`/`), clear-and-exit (`Escape`), visible focus, reduced motion, mobile reflow, and printable output.
- **Do** preserve current-page evidence inspection, ARIA tab behavior, focus containment on mobile, focus restoration, and 44px Reader targets.
- **Do** keep Chinese prose and English identifiers readable together with semantic headings and static text equivalents.
- **Do** treat seed `991d4b73` and the approved Copper Trace Atlas raster as provenance for this visual world, not as runtime evidence.

### Don't:

- **Don't** replace the authority map with a generic SaaS hero, bento dashboard, or interchangeable rounded cards.
- **Don't** use glass blur, neon glow, tonal gradient washes, or decorative circuitry that obscures labels.
- **Don't** invent customer claims, benchmarks, deployment facts, pricing, testimonials, or unaudited status metrics.
- **Don't** shrink complex diagrams until labels become microtext; preserve their structure with local overflow and responsive stacking.
- **Don't** animate a trace without a static label, symbol, or state word that communicates the same meaning.
