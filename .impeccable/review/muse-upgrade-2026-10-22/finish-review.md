# Finish review — Lumen product upgrade

## 1. Disposition

**fix**

The approved warm Operate direction is present and the desktop composition is coherent. Three bounded defects remain: mobile feed priority, goal output version fidelity, and small text contrast on pale surfaces. No rebuild is warranted. The user-pinned code-led plan and palette are the visual authority; there is no approved comp or quality-bar card to invent or retroactively require.

## 2. Findings

1. **P1 — Mobile feed hides every result below auxiliary context.** `final-feed-mobile.png` shows the expanded “正在为你跟进” panel occupying the rest of the first viewport, with no progress or output card visible. Its summary and inner heading repeat the same title. At `assistant-workspace.tsx:702`, `<details open>` is unconditional; `assistant-workspace.css:738` puts the aside in the first grid row below 1050px. Make results lead on small screens, keep context optional/collapsed, and show the context heading once. Preserve the desktop auxiliary column. Acceptance: a 390×844 initial feed capture shows the latest progress/result and its primary action without requiring the user to dismiss the context panel.

2. **P2 — A goal output can open a different version from its label.** `assistant-workspace.tsx:896` renders “版本 {r.revision}” but calls `openResource(r.id)`, which reads the latest revision. If that resource changes after the goal detail is read, clicking the displayed output silently opens the newer bytes. Pass the displayed revision through `openResource`, as feed output links already do. Acceptance: an existing goal detail test asserts that clicking a row labeled revision N reads resource ID + revision N; discussing the opened result retains that reference.

3. **P2 — Muted text fails the stated contrast floor on the pale surface.** `--muted: #647060` against `--surface-soft: #E8EEE4` measures **4.41:1**, below the 4.5:1 floor for small text. Concrete affected uses include `.idea-launch p` inside `.idea-plan` and presence status text on its pale hover surface. The token comment in `globals.css:11` claims ≥4.6:1 and is inaccurate. Darken the muted token slightly or use an existing darker foreground for pale-surface text, preserving the four approved palette anchors. Acceptance: small muted text on white, canvas, sidebar, and pale surfaces reaches ≥4.5:1.

## 3. Preservation checks

- Warm canvas #F6F5F0, ink #232A24, green action #3F5A43, pale #E8EEE4, white reading cards, desktop sidebar and auxiliary context are present.
- Logo, Geist stack, and original nine-tail fox assets remain in use. Five mobile navigation destinations have visible labels and current-page semantics.
- Create-and-plan uses the existing associated discussion and ordinary StartRun, with a stable `goal-plan:` identity, saved-goal retry, navigation guards, and existing-draft restoration. Source and focused tests cover the intended behavior.
- Feed output links and discussion references carry fixed resource revisions; conversation file cards preview the supplied stored revision. Finding 2 identifies the remaining goal-result exception.
- Goal acceptance is explicit and distinct from execution/review state. Ideas show reason, source, expected outcome, and an explicit task start.
- Library image/text thumbnails use real stored data; other kinds have typed covers and open actual stored-file previews. HTML preview remains sandboxed.
- Inspected transitions are restrained at 150–180ms. Reduced-motion overrides are present. No request to add briefing, dependency, provider, or runtime scope is introduced by this review.

## 4. Evidence and limits

All **13 required screenshots are valid, nonblank, legible captures**. Desktop captures are 1440×1000; wide feed and activity are full 1920×1080; tablet is 820×1067; mobile captures are 390×844. Their content and layout correspond to the inspected source.

Reviewed: final-feed-desktop, final-feed-wide, final-activity-wide, final-library-desktop, final-goals-desktop, final-goal-detail-desktop, final-ideas-desktop, final-library-tablet, final-feed-mobile, final-library-mobile, final-create-mobile, final-goal-detail-mobile, final-chat-mobile (all PNG).

The Ideas capture shades the Goals navigation row; source current-page logic is consistent. Treat this as capture hover ambiguity and keep the pointer off navigation in the confirmation captures, rather than asserting an unproven routing defect.

Screenshots use labeled synthetic acceptance content and the supplied deterministic Host setup. They do not prove live external-provider behavior. This is a screenshot/source review without browser interaction. Supplied test/build results were considered but not rerun. The existing detector report was read, not rerun; outdated DESIGN.md advisories are not fresh visual defects. Physical mobile keyboard behavior, screen reader use, and an actual 200% browser zoom session remain unverified. This is not a full accessibility certification.

## 5. Implementation handoff

Apply the three findings in one batch. Keep the chosen direction and existing execution boundaries. Add only the focused goal-output revision regression assertion needed for finding 2; use the existing checks for the small CSS changes.

Return refreshed captures at the same required filenames, with the pointer off navigation, plus the focused check result and final contrast values. The confirmation review will score these three findings **resolved / partial / unresolved**, without opening a new polish hunt.

Skipped: live browser, hardware, screen reader, and repeated test/detector runs. Risk: the three findings above remain until the confirmation evidence clears them.

## Confirmation verdict

**ship** — for the reviewed product upgrade and the three listed material findings.

| Finding | Score | Confirmation evidence |
| --- | --- | --- |
| Mobile first viewport | **resolved** | Refreshed 390×844 feed shows a single collapsed context summary, latest goal progress, and its primary action in the first viewport. Responsive matchMedia state defaults the context closed below 1051px; the duplicate inner heading is hidden there. Desktop capture retains the open auxiliary column. |
| Displayed goal output revision | **resolved** | Goal result rows now call openResource(r.id, false, r.revision). The added regression checks both the fixed resource read and the subsequent discussion reference. |
| Pale-surface contrast | **resolved** | Shared muted text is #606C5D. Independently recalculated contrast: white 5.53:1, canvas 5.06:1, sidebar 4.78:1, pale 4.68:1. All four approved palette anchors remain unchanged. |

All 13 refreshed final captures retain their requested widths and valid dimensions; tablet is now a full 820×1180 capture. Confirmation directly reopened the mobile and desktop feed, inspected the narrow source changes and regression, and recalculated contrast. The implementation handoff reports 297 Web tests passing and a successful production build including typecheck; this reviewer did not rerun them. No new defect hunt or detector run was performed.

Actual acceptance scope is the supplied fixture-based screenshot evidence, inspected source behavior, and the three remediations. It does not certify live provider behavior, physical mobile keyboards, screen readers, or an actual 200% browser zoom session. No listed material finding remains unresolved.
