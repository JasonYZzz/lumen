---
name: test-runner
description: Detect and run the repository's primary test suite, then summarize failures.
scripts:
  run: scripts/run.py
---
Use the declared `run` script with `run_skill_script` when the user asks to run
the standard test suite. Report the exact command selected, its exit status, and
the smallest useful failure excerpt.
