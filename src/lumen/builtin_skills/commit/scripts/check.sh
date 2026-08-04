#!/bin/sh
set -eu
git -C "$LUMEN_WORKSPACE" status --short
git -C "$LUMEN_WORKSPACE" diff --stat
