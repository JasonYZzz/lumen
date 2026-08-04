---
name: commit
description: Inspect the current Git changes and prepare a concise commit message without committing automatically.
scripts:
  check: scripts/check.sh
---
Review the repository status and diff before proposing a commit. Run the declared
`check` script with `run_skill_script`, summarize the exact scope, and propose a
short imperative commit subject. Never create the commit unless the user asks.
