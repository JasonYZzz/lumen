# Requirement matrix

| Requirement | Result | Evidence |
| --- | --- | --- |
| One clear project action | Pass | Start captures present “选择项目文件夹” as the sole primary action. |
| Centered 520px Operate form rail | Pass | CSS and both window sizes retain the same centered, readable form composition. |
| GPT-6, DeepSeek, Qwen, Kimi, GLM options | Pass | `model-presets.js` supplies all five providers; GPT-6.1 Sol is the first default. Dated official references are in `docs/desktop.md`. |
| Provider/model selection with automatic connection settings | Pass | Shared selection logic fills model ID, endpoint, protocol and credential variable; UI tests cover each preset. |
| Progressive advanced settings | Pass | Closed in the ordinary path; expanded Qwen captures expose complete fields and reachable actions via intentional scrolling. Custom/local selection opens the fields. |
| Persist one model; preserve existing configuration | Pass | Backend creates one model only when sources are absent; exclusive creation and existing-config regression coverage preserve user files. |
| Own API Key on the next page | Pass | Credential captures identify Qwen by its readable provider name and show the variable as secondary help. Password input remains separate from model configuration. |
| Keychain opt-in unchecked | Pass | Both credential captures and the UI contract show an unchecked checkbox. |
| Preserve Lumen identity and macOS typography | Pass | Warm neutral surface, dark action, amber focus and existing mark remain coherent; system typography and native selectors fit the desktop task. |

# Craft-floor findings

No material craft-floor defect found in the nine supplied final native WKWebView captures. Primary actions remain visible at 800×600. The expanded advanced form is deliberately taller than the viewport; its paired captures establish access to all fields and the final action. At minimum size the secondary credential-deletion action continues below the fold, while the credential input, opt-in, primary action and privacy explanation stay visible.

Source-derived text contrast against `#fdfdfc`: primary 15.28:1, body/help 7.35:1, muted/placeholder 5.26:1, error 6.00:1. The amber focus color is 5.10:1 against the surface. Labels, focus rings, selection/caret styling, reduced-motion loading state and native form validation are present. Loading/error/disabled behavior is supported by source and reported automated coverage; those states are not visually captured here. No decorative card scaffold, eyebrow, gradient text or competing visual treatment appears.

# Disposition

**Ship within the requested desktop onboarding scope.** No rebuild, recapture or additional polish pass is warranted by this evidence. Reused the parent's clean detector result rather than repeating the scan. Reported validation: 12 onboarding UI tests, 231 Web tests, Web typecheck/build and 9 backend tests passed.

# Material fixes

None required by this review. The prior inherited-scroll defect is resolved in source with `focus({preventScroll: true})` and `scrollTo(0, 0)`; final credential captures show the heading and focused input in their expected positions. No implementation edits were made by the reviewer.

# Keep

Keep the single project action, two simple model selectors, provider-specific guidance, collapsed advanced settings, readable credential names, separate key-entry step and unchecked Keychain opt-in. Keep the incumbent warm neutral visual language and native controls; adding a new visual world or more onboarding choices would weaken this task flow.

Not independently re-run: tests, live Provider requests or model availability checks. VoiceOver and real-account/network acceptance remain unverified; this review establishes UI finish, not end-to-end Provider acceptance.
