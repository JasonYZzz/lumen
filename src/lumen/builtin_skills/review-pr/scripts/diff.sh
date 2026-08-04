#!/bin/sh
set -eu
base="${1:-HEAD}"
git -C "$LUMEN_WORKSPACE" diff --find-renames "$base"
