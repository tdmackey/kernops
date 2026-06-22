#!/usr/bin/env bash
# Create a signed release tag with notes generated from release provenance.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: tag-release.sh <tag> <artifact-root> [--push]

Creates a signed git tag using release notes generated from
gb200-provenance.json files below <artifact-root>. Use --push to push the tag
after it is created.
EOF
}

[ "${1:-}" = "--help" ] && { usage; exit 0; }
TAG="${1:-}"
ARTIFACT_ROOT="${2:-}"
PUSH="${3:-}"
[ -n "$TAG" ] && [ -n "$ARTIFACT_ROOT" ] || { usage; exit 1; }
case "$TAG" in
    *[!A-Za-z0-9._/-]*|"") echo "!! suspicious tag name: $TAG" >&2; exit 1 ;;
esac
[ -d "$ARTIFACT_ROOT" ] || { echo "!! artifact root not found: $ARTIFACT_ROOT" >&2; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "!! tag already exists: $TAG" >&2
    exit 1
fi

ROOT="$(git rev-parse --show-toplevel)"
NOTES=$(mktemp "${TMPDIR:-/tmp}/gb200-release-notes.XXXXXX.md")
trap 'rm -f "$NOTES"' EXIT
python3 "$ROOT/scripts/write-release-summary.py" "$ARTIFACT_ROOT" \
    --title "gb200 release $TAG" > "$NOTES"

echo "=== release notes preview ==="
cat "$NOTES"
echo "============================="
read -r -p "Create signed tag $TAG? [y/N] " confirm
case "$confirm" in
    y|Y|yes|YES) ;;
    *) echo "aborted"; exit 0 ;;
esac

git tag -s "$TAG" -F "$NOTES"
echo ">> created signed tag $TAG"

if [ "$PUSH" = "--push" ]; then
    git push origin "$TAG"
    echo ">> pushed $TAG"
elif [ -n "$PUSH" ]; then
    echo "!! unknown option: $PUSH" >&2
    exit 1
fi
