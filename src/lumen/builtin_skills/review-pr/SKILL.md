---
name: review-pr
description: Review the current branch diff for correctness, safety, and missing tests.
scripts:
  diff: scripts/diff.sh
---
Run the declared `diff` script with `run_skill_script`, inspect the resulting
patch, and report actionable findings ordered by severity with file references.
If there are no findings, say so and mention any residual testing risk.
