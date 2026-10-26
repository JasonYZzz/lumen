# Desktop onboarding documentation review

Date: 2026-10-09. Disposition: **ship within the requested onboarding scope**.

Applied Impeccable's document reference as a source-first review of the existing desktop extension.
This pass records observed behavior and visual continuity; it does not regenerate the global design system.

## Evidence checked

- Read `docs/desktop.md`, the desktop validation report, `finish-review.md`, and the bootstrap's
  `app.js`, `model-presets.js`, `style.css`, and `index.html`.
- Compared the bootstrap to `src/web/src/app/globals.css`, Web's `layout.tsx`, and its Fold mark.
  The desktop and Web mark files are byte-identical. Shared warm neutrals, text, divider, danger,
  selection and caret colors match the incumbent Web values. The desktop uses macOS system fonts;
  Web loads Geist Sans. The centered 520px rail, native selectors and 44px button/disclosure targets
  are local desktop implementation facts, not new system-wide tokens.
- Inspected all nine final onboarding captures at 1180×820 and 800×600 logical sizes. Their
  2360×1640 / 1600×1200 dimensions match Retina 2× capture. The files contain JPEG bytes despite
  their `.png` names; they were readable and were preserved. Advanced top/bottom captures jointly
  expose every connection field and the action. Minimum credentials show the key input, unchecked
  Keychain opt-in, primary action and privacy explanation; secondary deletion continues below the fold.
- Checked `prepare_configuration` in `src/lumen/desktop.py`: absent configuration triggers the
  wizard, writes one selected model and the existing read-only tool template, validates before
  exclusive file creation, and keeps model configuration separate from credential values.
- Compared all nine model IDs, protocols and credential variable names in the guide to the preset
  source. The default is GPT-6.1 Sol. Presets fill endpoint/protocol/key names, advanced fields permit
  overrides, custom/local selection clears defaults and expands those fields, and the next request
  focuses its first control and resets scroll. Guide descriptions match these behaviors.
- Reopened the guide's official OpenAI, DeepSeek, Qwen and Kimi references; model names and API
  choices are supported there. The GLM reference returned a browser-tool error in this pass; its
  source-to-guide match was checked, while the earlier dated official verification was not repeated.
- Compared the validation report's final DMG SHA256 with the existing generated `SHA256SUMS.txt`:
  `20a42acfba0cfb4a64d169e77cb6608cf3407d33d370c31692d760efc4270a21` matches.

## Corrections and existing boundaries

Corrected one validation-report sentence that described OS typography as inherited from Web.
It now distinguishes the reused Web palette/Fold mark from desktop system typography and Web Geist.
No model-table or first-run instruction correction was needed.

Root `PRODUCT.md` and `DESIGN.md` describe the Architecture Atlas and its Copper Trace world.
Their existing scope is not the warm-neutral chat/bootstrap identity. `PRODUCT.md` also retains an
older Session v9 statement while current source declares v12. These pre-existing issues remain
outside this onboarding task. Both files are preserved; `.impeccable/design.json` remains absent.
No new assets, tokens, sidecar or UI edits were introduced.

Tests, builds, native recapture and the reported validation counts were not rerun. This documentation
review does not establish real Provider access, macOS 14, VoiceOver, microphone, quarantine or
Keychain acceptance; the guide and validation report continue to identify those limits.
