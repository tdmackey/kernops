#!/usr/bin/env bash
# Reconstruct a patched kernel tree purely from the monorepo record
# (kernel/upstream-base.txt pin + kernel/patches/gb200/<base>/).
#
# This is what CI builds from — if this fails, the artifact-of-record is
# incomplete, regardless of what local branches contain.
#
# Usage:
#   apply-series.sh --check <base>            # fast: validate series applies
#                                             # onto the pin (index-only)
#   apply-series.sh <base> <workdir>          # full: detached worktree at the
#                                             # pin + git am the series
#
# REPO=/path/to/kernel/clone overrides the default noble clone.
set -euo pipefail

MONO="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-/Volumes/Linux/noble}"

CHECK=0
[ "${1:-}" = "--check" ] && { CHECK=1; shift; }
BASE="${1:?usage: apply-series.sh [--check] <base> [workdir]}"

TAG=$(awk -F'\t' -v b="$BASE" '$1==b{print $2}' "$MONO/kernel/upstream-base.txt")
[ -n "$TAG" ] || { echo "!! no pin for '$BASE' in upstream-base.txt" >&2; exit 1; }
PDIR="$MONO/kernel/patches/gb200/$BASE"
PATCHES=()
[ -d "$PDIR" ] && while IFS= read -r p; do PATCHES+=("$p"); done < <(ls "$PDIR"/*.patch 2>/dev/null || true)

echo ">> $BASE: $TAG + ${#PATCHES[@]} patch(es)"

if [ $CHECK -eq 1 ]; then
    # Apply the series into a throwaway index — sequential, so later patches
    # see earlier ones; no worktree needed; takes seconds.
    tmpidx=$(mktemp)
    trap 'rm -f "$tmpidx"' EXIT
    GIT_INDEX_FILE="$tmpidx" git -C "$REPO" read-tree "$TAG"
    for p in "${PATCHES[@]}"; do
        if GIT_INDEX_FILE="$tmpidx" git -C "$REPO" apply --cached "$p" 2>/dev/null; then
            echo "   ok    $(basename "$p")"
        else
            echo "   FAIL  $(basename "$p")" >&2
            exit 1
        fi
    done
    echo ">> series applies cleanly onto $TAG"
    exit 0
fi

WORKDIR="${2:?usage: apply-series.sh <base> <workdir>}"
[ -e "$WORKDIR" ] && { echo "!! $WORKDIR already exists" >&2; exit 1; }
git -C "$REPO" worktree add --detach "$WORKDIR" "$TAG"
if [ ${#PATCHES[@]} -gt 0 ]; then
    git -C "$WORKDIR" am "${PATCHES[@]}" || {
        echo "!! git am failed — series does not apply to $TAG" >&2
        git -C "$WORKDIR" am --abort || true
        exit 1
    }
fi
echo ">> reconstructed: $WORKDIR ($(git -C "$WORKDIR" rev-parse --short HEAD))"
