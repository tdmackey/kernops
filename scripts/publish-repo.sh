#!/usr/bin/env bash
# Publish built debs into the apt repo — one suite per base.
#
# Usage: publish-repo.sh <base> <deb-dir> [series] [arch]
#
# Repo layout (reprepro): $REPO_DIR with suite <base>, component main,
# arches arm64+amd64. Locally unsigned (clients use [trusted=yes]); CI signs
# with the KMS-backed key (SignWith in conf/distributions — TODO when
# signing lands).
#
# Consume on a node:
#   deb [trusted=yes] http://<host>/repo <base> main
set -euo pipefail

MONO="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:?usage: publish-repo.sh <base> <deb-dir> [series] [arch]}"
DEBDIR=$(cd "${2:?usage: publish-repo.sh <base> <deb-dir> [series] [arch]}" && pwd)
SERIES="${3:-noble}"
# shellcheck source=scripts/lib/arch.sh
. "$MONO/scripts/lib/arch.sh"
ARCH="$(gb200_arch_normalize "${4:-${ARCH:-arm64}}")"
BUILDER_IMAGE="${BUILDER_IMAGE:-localhost/gb200-builder:$SERIES}"
REPO_DIR="${REPO_DIR:-/Volumes/Linux/repo}"

mkdir -p "$REPO_DIR/conf"

# Append missing suites to conf/distributions; NEVER regenerate it —
# operators add SignWith etc. there and a publish must not destroy that.
while IFS=$'\t' read -r b _; do
    [[ -z "$b" || "$b" == \#* ]] && continue
    grep -qsx "Codename: $b" "$REPO_DIR/conf/distributions" && continue
    cat >> "$REPO_DIR/conf/distributions" <<EOF
Codename: $b
Suite: $b
Components: main
Architectures: arm64 amd64
Description: gb200 kernel pipeline — $b

EOF
done < "$MONO/kernel/upstream-base.txt"

awk -v suite="$BASE" -v arch="$ARCH" '
    /^Codename:[[:space:]]*/ { in_suite = ($2 == suite) }
    in_suite && /^Architectures:[[:space:]]*/ {
        found = 0
        for (i = 2; i <= NF; i++) {
            if ($i == arch) found = 1
        }
        if (!found) $0 = $0 " " arch
    }
    { print }
' "$REPO_DIR/conf/distributions" > "$REPO_DIR/conf/distributions.tmp"
mv "$REPO_DIR/conf/distributions.tmp" "$REPO_DIR/conf/distributions"

if [ -f "$DEBDIR/.publish-manifest" ]; then
    while IFS= read -r rel; do
        [[ -z "$rel" || "$rel" == \#* ]] && continue
        rel="${rel#./}"
        [ -f "$DEBDIR/$rel" ] || { echo "!! manifest entry missing: $rel" >&2; exit 1; }
    done < "$DEBDIR/.publish-manifest"
    grep -Ev '^[[:space:]]*(#|$)' "$DEBDIR/.publish-manifest" | grep -q . || {
        echo "!! empty publish manifest in $DEBDIR" >&2; exit 1; }
else
    find "$DEBDIR" -maxdepth 2 -name '*.deb' | grep -q . || { echo "!! no debs in $DEBDIR" >&2; exit 1; }
fi

podman run --rm -v "$REPO_DIR:/repo" -v "$DEBDIR:/debs:ro" \
    -e BASE="$BASE" \
    "$BUILDER_IMAGE" bash -c '
    set -euo pipefail
    command -v reprepro >/dev/null || { apt-get -qq update && apt-get -qq install -y reprepro >/dev/null; }
    cd /repo
    include_deb() {
        d=$1
        [ -f "$d" ] || { echo "!! missing deb: $d" >&2; exit 1; }
        reprepro -S kernel --ignore=wrongdistribution includedeb "$BASE" "$d"
    }
    if [ -f /debs/.publish-manifest ]; then
        while IFS= read -r rel; do
            case "$rel" in ""|\#*) continue ;; esac
            rel=${rel#./}
            include_deb "/debs/$rel"
        done < /debs/.publish-manifest
    else
        find /debs -maxdepth 2 -name "*.deb" -print0 | sort -z | \
        while IFS= read -r -d "" d; do
            include_deb "$d"
        done
    fi
    echo "── suite $BASE now contains:"
    reprepro list "$BASE"
'
if [ -f "$DEBDIR/gb200-provenance.json" ]; then
    run_id=$(basename "$DEBDIR")
    mkdir -p "$REPO_DIR/provenance/$BASE/$ARCH"
    cp "$DEBDIR/gb200-provenance.json" "$REPO_DIR/provenance/$BASE/$ARCH/$run_id.json"
fi
echo ">> repo at $REPO_DIR — serve with: python3 -m http.server -d $REPO_DIR"
