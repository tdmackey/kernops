#!/usr/bin/env bash
# Is an upstream commit (or an adapted backport of it) present in each ref?
#
# Usage: check-present.sh <sha> <ref> [<ref>...]
#   REPO=/path/to/kernel/tree overrides the default noble clone.
#
# Three signals per ref, strongest first:
#   ancestor     — the exact commit is in the ref's history
#   subject@X    — a commit with the same subject exists (stable backport)
#   content      — the diff reverse-applies against the ref's tree (catches
#                  adapted backports that changed both SHA and subject)
#   MISSING      — none of the above
set -euo pipefail
SHA="${1:?usage: check-present.sh <sha> <ref> [<ref>...]}"
shift
REPO="${REPO:-/Volumes/Linux/noble}"
git() { command git -C "$REPO" "$@"; }

subj=$(git show -s --format=%s "$SHA")
files=$(git show --format= --name-only "$SHA")
printf 'commit  %s  %s\n' "$(git rev-parse --short "$SHA")" "$subj"

for ref in "$@"; do
    printf '%-45s ' "$ref"
    if git merge-base --is-ancestor "$SHA" "$ref" 2>/dev/null; then
        echo "ancestor"
        continue
    fi
    hit=$(git log --format='%h' --fixed-strings --grep="$subj" "$ref" -- 2>/dev/null | head -1)
    if [ -n "$hit" ]; then
        echo "subject@$hit"
        continue
    fi
    # Reverse-apply test against the ref's tree via a throwaway index —
    # no worktree or checkout needed.
    tmpidx=$(mktemp)
    trap 'rm -f "$tmpidx"' EXIT
    GIT_INDEX_FILE="$tmpidx" git read-tree "$ref"
    if git show "$SHA" | GIT_INDEX_FILE="$tmpidx" git apply --cached --check --reverse - >/dev/null 2>&1; then
        echo "content"
    else
        echo "MISSING"
    fi
    rm -f "$tmpidx"
done

echo
echo "partial-series hint — compare full file history per base:"
for f in $files; do echo "  git -C $REPO log --oneline <ref> -- $f"; done
