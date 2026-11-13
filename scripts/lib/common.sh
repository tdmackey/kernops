#!/usr/bin/env bash
# Shared settings for every script: where work happens and what the product is
# called. Source this; do not execute it.

_common_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# One root holds the kernel clones, worktrees, build output and the apt repo:
#   $WORK_ROOT/noble   Ubuntu kernel clone      $WORK_ROOT/build   worktrees + output
#   $WORK_ROOT/repo    apt repository           $WORK_ROOT/kernel.git   kpm's unified repo
# It must be on a case-sensitive filesystem. On a macOS/podman host it must also
# be a path the podman VM mounts, e.g. `export WORK_ROOT=/Volumes/Linux`.
WORK_ROOT="${WORK_ROOT:-${KERNOPS_WORK:-$HOME/kernops}}"

# The product name is set once, in kernel/identity.toml.
VENDOR="${VENDOR:-$(sed -nE 's/^vendor[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' \
    "$_common_dir/../../kernel/identity.toml" | head -n1)}"
: "${VENDOR:?vendor is not set in kernel/identity.toml}"

export WORK_ROOT VENDOR
