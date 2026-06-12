#!/usr/bin/env bash
# Backport upstream commit(s) to every base in kernel/upstream-base.txt.
#
# Usage: backport.sh [--dry-run] [--no-export] <sha> [<sha>...]
#
#   For each base (worktree /Volumes/Linux/build/<base>, branch gb200/<base>):
#     1. audit  — skip shas already present (ancestor / renamed stable
#                 backport by subject / content reverse-apply)
#     2. apply  — cherry-pick -x the missing ones, oldest-first
#     3. on conflict: abort that base, keep going with the others, and
#        report — conflicts mean human judgment (see docs/backporting.md,
#        usually a missing prerequisite or a partial stable backport)
#     4. export — refresh kernel/patches/gb200/<base>/ + dashboard
#
#   The script never resolves conflicts and never commits the monorepo:
#   review `git -C /Volumes/Linux/gb200 diff`, build-test, then commit.
set -euo pipefail

REPO=${REPO:-/Volumes/Linux/noble}
BUILD_ROOT=${BUILD_ROOT:-/Volumes/Linux/build}
MONO="$(cd "$(dirname "$0")/.." && pwd)"
PINS="$MONO/kernel/upstream-base.txt"

DRY_RUN=0 EXPORT=1
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --dry-run)   DRY_RUN=1 ;;
        --no-export) EXPORT=0 ;;
        *) echo "unknown flag $1" >&2; exit 2 ;;
    esac
    shift
done
[ $# -ge 1 ] || { echo "usage: backport.sh [--dry-run] [--no-export] <sha>..." >&2; exit 2; }

git() { command git -C "$REPO" "$@"; }

# --- resolve shas, fetching mainline once if something is unknown
for sha in "$@"; do
    if ! git cat-file -e "$sha^{commit}" 2>/dev/null; then
        echo ">> $sha not local; fetching upstream/master..."
        git fetch --no-tags upstream master
        git cat-file -e "$sha^{commit}" || { echo "!! $sha not found even after fetch" >&2; exit 1; }
    fi
done

# --- oldest-first application order (portable reverse; tail -r is BSD-only)
SHAS=$(git log --no-walk=sorted --format=%H "$@" | awk '{a[NR]=$0} END{for(i=NR;i>0;i--)print a[i]}')
echo ">> order:"
for s in $SHAS; do git log -1 --format='     %h %s' "$s"; done

audit() { # audit <sha> <ref> -> ancestor|subject@x|content|MISSING
    local sha=$1 ref=$2 subj hit tmpidx
    git merge-base --is-ancestor "$sha" "$ref" 2>/dev/null && { echo ancestor; return; }
    subj=$(git show -s --format=%s "$sha")
    # scope the grep to the commit's files: stable backports touch the same
    # paths, and path-scoped log is orders of magnitude faster
    hit=$(git log --format='%h' --fixed-strings --grep="$subj" "$ref" -- \
          $(git show --format= --name-only "$sha") 2>/dev/null | head -1)
    [ -n "$hit" ] && { echo "subject@$hit"; return; }
    tmpidx=$(mktemp)
    GIT_INDEX_FILE="$tmpidx" git read-tree "$ref"
    if git show "$sha" | GIT_INDEX_FILE="$tmpidx" git apply --cached --check --reverse - >/dev/null 2>&1
    then echo content; else echo MISSING; fi
    rm -f "$tmpidx"
}

CONFLICTED=() CHANGED=()

while IFS=$'\t' read -r base tag _; do
    [[ -z "$base" || "$base" == \#* ]] && continue
    branch="gb200/$base" wt="$BUILD_ROOT/$base"
    ref=$branch
    git rev-parse --verify -q "$branch" >/dev/null || ref=$tag
    echo
    echo "== $base  (base $tag, ref $ref)"

    todo=()
    for sha in $SHAS; do
        st=$(audit "$sha" "$ref")
        printf '   %-14s %s\n' "$st" "$(git log -1 --format='%h %s' "$sha")"
        [ "$st" = MISSING ] && todo+=("$sha")
    done
    [ ${#todo[@]} -eq 0 ] && { echo "   nothing to do"; continue; }
    [ $DRY_RUN -eq 1 ] && { echo "   would apply ${#todo[@]} commit(s)"; continue; }

    # worktree/branch on demand
    if [ ! -d "$wt" ]; then
        echo "   creating worktree $wt"
        if git rev-parse --verify -q "$branch" >/dev/null; then
            git worktree add "$wt" "$branch" >/dev/null
        else
            git worktree add -b "$branch" "$wt" "$tag" >/dev/null
        fi
    fi
    # make sure the worktree is ON the branch (a tag-pinned worktree is
    # detached; cherry-picking there would commit into thin air)
    if [ "$(command git -C "$wt" symbolic-ref --short -q HEAD || true)" != "$branch" ]; then
        if git rev-parse --verify -q "$branch" >/dev/null; then
            command git -C "$wt" switch "$branch" >/dev/null 2>&1
        else
            command git -C "$wt" switch -c "$branch" "$tag" >/dev/null 2>&1
        fi
    fi
    if [ -n "$(command git -C "$wt" status --porcelain --untracked-files=no)" ]; then
        echo "   !! worktree dirty — skipping $base"
        CONFLICTED+=("$base (dirty worktree)")
        continue
    fi

    ok=1
    for sha in "${todo[@]}"; do
        if command git -C "$wt" cherry-pick -x "$sha" >/dev/null 2>&1; then
            echo "   applied  $(git log -1 --format='%h %s' "$sha")"
        else
            echo "   CONFLICT $(git log -1 --format='%h %s' "$sha") — aborting this base"
            command git -C "$wt" cherry-pick --abort || true
            CONFLICTED+=("$base @ $(git log -1 --format=%h "$sha")")
            ok=0
            break
        fi
    done
    [ $ok -eq 1 ] && CHANGED+=("$base")
done < "$PINS"

# --- export
if [ $EXPORT -eq 1 ] && [ $DRY_RUN -eq 0 ] && [ ${#CHANGED[@]} -gt 0 ]; then
    echo
    for base in "${CHANGED[@]}"; do
        tag=$(awk -F'\t' -v b="$base" '$1==b{print $2}' "$PINS")
        out="$MONO/kernel/patches/gb200/$base"
        mkdir -p "$out"; rm -f "$out"/*.patch
        command git -C "$BUILD_ROOT/$base" format-patch --zero-commit -N \
            "$tag..gb200/$base" -o "$out" >/dev/null
        echo ">> exported $(ls "$out" | wc -l | tr -d ' ') patches -> kernel/patches/gb200/$base/"
    done
    python3 "$MONO/tools/dashboard/generate.py" --offline || true
fi

echo
echo "== summary"
echo "   applied cleanly : ${CHANGED[*]:-none}"
echo "   need a human    : ${CONFLICTED[*]:-none}"
[ ${#CHANGED[@]} -gt 0 ] && [ $DRY_RUN -eq 0 ] && cat <<EOF
   next: build-test each changed base, review the monorepo diff, commit.
     $MONO/scripts/build-kernel.sh $BUILD_ROOT/<base> generic-64k <series>
     git -C $MONO add -A && git -C $MONO commit
EOF
[ ${#CONFLICTED[@]} -eq 0 ]
