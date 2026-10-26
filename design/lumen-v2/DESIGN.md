---
name: Lumen v2 personal assistant proposal
description: Current Lumen identity with inspectable conversations, updates, ideas, goals, and resources.
colors:
  paper: "#fdfdfc"
  surface: "#fdfdfc"
  sidebar: "#fafafa"
  surface-soft: "#f0f0ee"
  ink: "#242422"
  ink-soft: "#555552"
  muted: "#6b6b67"
  line: "#e5e5e2"
  line-strong: "#d5d5d1"
  nav-active: "#eaeae7"
  green: "#426047"
  green-bg: "#f0f3ee"
  warn: "#805527"
  warn-bg: "#f8f5ef"
  danger: "#a63f4d"
typography:
  headline:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "27px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "-0.02em"
  title:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "19px"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "-0.02em"
  body:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.65
  composer:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.7
  label:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.65
  metadata:
    fontFamily: 'Geist, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.65
rounded:
  label: "5px"
  field: "7px"
  nav: "8px"
  control: "9px"
  suggestion: "10px"
  surface: "14px"
  card: "16px"
  composer: "24px"
spacing:
  tight: "8px"
  control-gap: "10px"
  label-gap: "12px"
  regular: "20px"
  section: "24px"
  wide: "28px"
  column-gap: "32px"
  page: "40px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 15px"
  button-secondary:
    textColor: "{colors.ink-soft}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 15px"
  field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    padding: "10px 12px"
  nav-active:
    backgroundColor: "{colors.nav-active}"
    textColor: "{colors.ink}"
    rounded: "{rounded.nav}"
    padding: "9px 12px"
  status-label:
    backgroundColor: "{colors.green-bg}"
    textColor: "{colors.green}"
    typography: "{typography.metadata}"
    rounded: "{rounded.label}"
    padding: "3px 7px"
  feed-card:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "23px 24px"
  composer:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.composer}"
    rounded: "{rounded.composer}"
    padding: "17px 20px 14px"
---

# Design System: Lumen v2 personal assistant proposal

## Overview

**Creative North Star: "The familiar Lumen workspace"**

This system describes the r2 prototype in this directory. Its visual authority is the current Lumen product, especially [globals.css](../../src/web/src/app/globals.css), with the final cascade in [style.css](style.css). The scoped proposal does not replace the repository's Architecture Atlas identity. [research.md](research.md) records the product decisions and evidence.

The interface keeps Lumen's white and gray surfaces, dark text, local Geist, original Fold logo, and existing animated nine-tail fox. Calm hierarchy makes conversations, readable updates, suggestions, ongoing goals, and produced resources easy to inspect. Content and familiar controls carry the work; the fox provides companionship.

**Key Characteristics:**

- White and gray surfaces with dark primary actions and fine boundaries.
- Local Geist and Chinese system fallbacks preserve the current product identity.
- Conversations lead; updates, ideas, goals, and resources have distinct places.
- Existing authored fox video supplies companionship without certifying task success.
- Example data and simulated actions remain explicitly disclosed.

## Colors

The working palette inherits Lumen's neutral canvas and ink hierarchy. Frontmatter owns the implemented light tokens; color names here describe their application.

### Primary

- **Lumen ink** (`ink`): filled primary actions, selected navigation text and icons, current tabs, focus outlines, and the send control. Confirmation uses the same ink treatment.
- The original Fold logo retains its own amber artwork. That asset color does not become a new interface theme.

### Secondary

- **Source green** (`green`, `green-bg`): available-source and positive state labels.
- **Source caution** (`warn`, `warn-bg`): missing coverage and unresolved conditions, paired with an explanation.
- **Error red** (`danger`): form validation messages.

### Neutral

- **Page white** (`paper`, `surface`): main canvas, fields, composer, and feed cards.
- **Sidebar gray** (`sidebar`): the actual persistent navigation background and selected secondary containers.
- **Soft gray** (`surface-soft`): user messages, icon wells, and hover fills.
- **Secondary ink / muted gray** (`ink-soft`, `muted`): descriptions and metadata.
- **Fine / strong line** (`line`, `line-strong`): content boundaries and field strokes.
- **Selected gray** (`nav-active`): the current navigation row.

The settings dialog offers an additional dark preview through `body.dark` semantic overrides. It is a prototype theme; the default identity and this token reference describe the current light product. Check the actual dark cascade before extending that preview.

**The Neutral Action Rule.** Primary actions, active navigation, and focus use ink; the logo's amber and semantic state colors stay local to their purpose.

**The State Has Words Rule.** Color never replaces a state label, missing-source explanation, or delivery evidence.

## Typography

**Display / Body Font:** Geist with PingFang SC, Microsoft YaHei, and sans-serif fallbacks. Regular and semibold WOFF2 files are bundled locally in `assets/`.

The same family serves navigation, reading, and controls. Weight, size, and spacing establish hierarchy. Preserve this user-selected product typography rather than substituting a decorative face.

### Hierarchy

- **Headline:** General page headings use the frontmatter role and step down to 25px at compact widths.
- **Title:** Feed headings use the title role; idea headings use 20px, resource titles 17px, and operational subheadings 16px.
- **Body:** General text uses the body role; feed summaries use a 1.8 line height, and conversation messages use 15px, reducing to 14px on mobile.
- **Composer:** The main input uses the composer role and reduces to 15px on mobile.
- **Label / Metadata:** Controls use the label role; dates, source details, and small status labels use the metadata role. Preserve legibility rather than shrinking these to accommodate more content.
- **Welcome:** The centered greeting is a quiet 17px medium-weight line, reducing to 16px on mobile; it is not a large promotional headline.

## Layout

The desktop shell uses a 260px fixed sidebar, a 58px header, and a centered main region capped at 1240px. Default main padding is 33px above and 40px on the sides and below. Navigation uses five primary destinations: 对话、信息流、灵感、目标、资源库. 自动化 is separated as a supporting management entry. Settings contains 个性化偏好 and 连接与权限; there is no independent memory or identity button.

Conversation and welcome content are capped at 760px. On desktop the welcome row balances a 168px fox stage with centered greeting text before the composer. Feed content has a main stream and a 224px supporting column with a 32px gap. Idea and resource grids use two columns; the leading idea spans both columns. Goals retain their two-column layout where space permits.

| Implemented breakpoint | Behavior |
| --- | --- |
| At least 1500px | Main padding becomes 40px 60px; feed aside grows to 250px with a 45px gap; resource grid uses three columns. |
| At most 1100px | Sidebar narrows to 226px; main padding becomes 28px; feed aside becomes 200px; fox stage becomes 130px wide. |
| At most 900px | Feed aside moves below the stream; page headings become 25px. |
| At most 850px | Existing goals and general two-column layouts stack. |
| At most 620px | Sidebar becomes a 260px off-canvas region; workspace offset disappears; main padding becomes 25px 20px 30px. Fox and greeting stack above the composer. Idea and resource grids become single-column, automation rows stack, form columns stack, and drawers fill the viewport. |

## Elevation & Depth

Thin boundaries and slight tonal differences separate persistent surfaces. Feed, idea, and resource cards are flat bordered containers; normal rows and controls do not lift on hover. Shadow belongs primarily to temporary layers.

### Shadow Vocabulary

- **Overlay:** `0 18px 70px #24242226`, shared by dialogs and mobile navigation.
- **Toast:** `0 5px 20px #0002`, used for brief feedback.
- The original logo has its small amber drop shadow; it is an asset treatment, not a container-elevation pattern.

**The Quiet Surface Rule.** Reading surfaces stay flat; depth identifies a temporary layer requiring attention.

## Shapes

Use restrained rounded geometry with fine 1px strokes. Compact labels, fields, navigation, buttons, and suggestion controls use the smaller radii in frontmatter. Feed, idea, and library cards use the card radius; goals and callouts retain the surface radius. The large composer has the most generous curve, reducing to 22px on mobile. Round send controls and profile images are local affordances.

## Components

### Buttons

Compact ink-filled actions have a 40px minimum height, with a transparent outlined alternative. Hover uses a slightly lighter ink fill or soft neutral background. Disabled actions reduce opacity and use the disabled cursor. The shared visible keyboard focus is a 2px ink outline with a 4px offset. New conversation and sidebar search are quiet text-and-icon controls rather than filled primary buttons.

### Chips

Small status labels pair semantic fills with explicit text. Tabs and category filters are actual buttons with pressed state; static status labels do not pretend to be controls. Waiting remains quiet.

### Cards / Containers

Feed cards group one update, source coverage, and its next action. A missing-source warning stays beside the update. Idea cards explain why a suggestion appears and offer start or dismiss actions. Resource cards expose type, title, summary, open action, and synchronized bookmark state. Goal containers retain explicit milestones; decorative progress must not imply an undefined measure of completion.

### Inputs / Fields

Fields use a fine strong-line boundary, visible label, and 42px minimum height. The welcome composer is a distinct wide input surface with an accessible multiline field, tools, and circular send action. Suggestions fill the input; they do not send automatically. Preserve native fieldsets, legends, validation text, and visible focus.

### Navigation

Native links expose `aria-current="page"`. Settings provides preference and connection access. Mobile closed navigation is inert; its toggle exposes expanded state, opening moves focus to the close control, and Escape closes it. A skip link and route changes move focus into the main region. These implemented behaviors do not assert a complete accessibility audit.

### Dialogs and feedback

Named native dialogs hold search, settings, source details, resource editing, and confirmations. Closing restores focus to the connected opener or active navigation. Toast and storage fallback messages use status announcements. Page arrival is disabled in r2; keep navigation immediate. The reduced-motion CSS removes animations, transitions, and smooth scrolling.

### Existing nine-tail companion

The welcome page mounts the original `VideoMascotRenderer` through [mascot-entry.tsx](mascot-entry.tsx), reusing the original idle/react clips, motion preference, and visibility hooks. Greeting clicks and prompt focus use the existing renderer's interactions. System reduced motion uses the original poster; hidden or offscreen playback pauses. Route changes unmount the renderer and release its resources before mounting again.

Only the welcome page instantiates the player. The top activity control uses the original poster thumbnail. A screenshot records one frame of the real player; it does not make the animation a new static asset. The r1 generated chibi image and identity presentation are historical assets, not current interface references.

### Simulation and state honesty

Conversations, filtering, bookmarks, edits, approvals, connection controls, and run status are local demonstrations. No Provider, account, scheduler, or real delivery is connected. The real reused animation does not change that boundary. Preserve the visible prototype label and local-demo note, and keep enabled, running, prepared result, partial coverage, and confirmed delivery distinct.

The current visual evidence is `evidence/r2-*.jpg`. Older numbered proposal screenshots do not override the r2 implementation.

## Do's and Don'ts

### Do:

- **Do** preserve the current Lumen palette, local Geist, original Fold logo, and existing animated fox.
- **Do** keep updates, suggestions, goals, and resources distinguishable and their shared references inspectable.
- **Do** keep source coverage, simulation disclosures, native controls, and visible keyboard focus readable.
- **Do** preserve reduced motion, offscreen pause, and renderer cleanup.
- **Do** use the fox as a quiet companion while operational text carries status and risk.

### Don't:

- **Don't** apply this scoped proposal to the root Architecture Atlas design system.
- **Don't** restore the r1 orange theme, generated chibi asset, or separate memory and identity navigation.
- **Don't** use a fox expression as execution evidence or normal waiting as a pulsing activity state.
- **Don't** present a prepared result as confirmed delivery, or missing sources as no new information.
- **Don't** treat screenshots or local checks as proof of production automation, complete accessibility, or real-device acceptance.
