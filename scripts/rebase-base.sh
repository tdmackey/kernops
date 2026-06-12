#!/usr/bin/env bash
# Move one base's patch stack to a new Ubuntu tag — the treadmill step.
#
# Usage:
#   rebase-base.sh <base> [target-tag]   # target defaults to the archive-
#                                        # published tag from detect.py
#   rebase-base.sh --finish <base>       # after resolving a conflict by hand
#                                        # (git rebase --continue), do the
#                                        # pin/export/dashboard half
#
# On conflict the rebase is LEFT IN PLACE in the worktree (rerere learns the
# resolution; docs/backporting.md conventions apply) — resolve, then --finish.
set -euo pipefail

MONO="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-/Volumes/Linux/noble}"
BUILD_ROOT="${BUILD_ROOT:-/Volumes/Linux/build}"
PINS="$MONO/kernel/upstream-base.txt"

FINISH=0
[ "${1:-}" = "--finish" ] && { FINISH=1; shift; }
BASE="${1:?usage: rebase-base.sh [--finish] <base> [target-tag]}"
TARGET="${2:-}"

OLD=$(awk -F'\t' -v b="$BASE" '$1==b{print $2}' "$PINS")
[ -n "$OLD" ] || { echo "!! no pin for $BASE" >&2; exit 1; }
BRANCH="gb200/$BASE" WT="$BUILD_ROOT/$BASE"
[ -d "$WT" ] || { echo "!! no worktree at $WT" >&2; exit 1; }

if [ -z "$TARGET" ]; then
    echo ">> asking the archive (detect.py)..."
    TARGET=$(python3 "$MONO/tools/treadmill/detect.py" --json 2>/dev/null \
             | python3 -c "import json,sys; \
                 print(next((r.get('expected_tag','') for r in json.load(sys.stdin) \
                            if r['base']=='$BASE'), ''))") || true
    [ -n "$TARGET" ] || { echo "!! detector gave no target for $BASE" >&2; exit 1; }
fi

git -C "$REPO" rev-parse -q --verify "$TARGET^{commit}" >/dev/null || {
    echo ">> fetching tag $TARGET"; git -C "$REPO" fetch --no-tags origin "refs/tags/$TARGET:refs/tags/$TARGET"; }

if [ $FINISH -eq 0 ]; then
    [ "$TARGET" = "$OLD" ] && { echo ">> $BASE already on $TARGET"; exit 0; }
    [ -z "$(git -C "$WT" status --porcelain --untracked-files=no)" ] || {
        echo "!! worktree dirty" >&2; exit 1; }
    echo ">> $BASE: $OLD -> $TARGET"
    OLD_HEAD=$(git -C "$WT" rev-parse "$BRANCH")
    git -C "$WT" tag -f "treadmill-prev/$BASE" "$OLD_HEAD" >/dev/null

    if ! git -C "$WT" rebase --empty=drop --onto "$TARGET" "$OLD" "$BRANCH"; then
        cat >&2 <<EOF
!! rebase conflict — state left in $WT
   resolve per docs/backporting.md, then:
     git -C $WT rebase --continue
     $0 --finish $BASE $TARGET
   or bail out:  git -C $WT rebase --abort
EOF
        exit 2
    fi
else
    # --finish: verify the manual rebase actually completed onto TARGET
    [ -z "$(git -C "$WT" status --porcelain --untracked-files=no)" ] || {
        echo "!! worktree dirty / rebase still in progress" >&2; exit 1; }
    git -C "$WT" merge-base --is-ancestor "$TARGET" "$BRANCH" || {
        echo "!! $BRANCH does not contain $TARGET — rebase not finished?" >&2; exit 1; }
    OLD_HEAD=$(git -C "$WT" rev-parse -q --verify "refs/tags/treadmill-prev/$BASE" || echo "")
fi

# --- dropped-patch report (commits whose subject vanished from the stack)
NEW_SUBJECTS=$(git -C "$WT" log --format=%s "$TARGET..$BRANCH")
if [ -n "${OLD_HEAD:-}" ]; then
    while IFS= read -r s; do
        echo "$NEW_SUBJECTS" | grep -qxF "$s" || echo "   DROPPED (landed in base): $s"
    done < <(git -C "$WT" log --format=%s "$OLD..$OLD_HEAD")
fi
echo ">> stack on $TARGET: $(git -C "$WT" rev-list --count "$TARGET..$BRANCH") patch(es)"

# --- range-diff for review
if [ -n "${OLD_HEAD:-}" ]; then
    RD="$MONO/dashboard/range-diff-$BASE.txt"
    mkdir -p "$MONO/dashboard"
    git -C "$WT" range-diff "$OLD..$OLD_HEAD" "$TARGET..$BRANCH" > "$RD" || true
    echo ">> range-diff: $RD"
fi

# --- pin, export, validate, dashboard
python3 - "$PINS" "$BASE" "$TARGET" <<'EOF'
import sys
pins, base, target = sys.argv[1:4]
out = []
for line in open(pins):
    parts = line.rstrip("\n").split("\t")
    if parts and parts[0] == base:
        parts[1] = target
        line = "\t".join(parts) + "\n"
    out.append(line)
open(pins, "w").writelines(out)
EOF
OUTDIR="$MONO/kernel/patches/gb200/$BASE"
mkdir -p "$OUTDIR"; rm -f "$OUTDIR"/*.patch
git -C "$WT" format-patch --zero-commit -N "$TARGET..$BRANCH" -o "$OUTDIR" >/dev/null
REPO="$REPO" "$MONO/scripts/apply-series.sh" --check "$BASE"
python3 "$MONO/tools/dashboard/generate.py" --offline >/dev/null || true

echo ">> done. next: build-test, review $MONO diff (+ range-diff), commit."
