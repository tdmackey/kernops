#!/usr/bin/env bash
# Publish built debs into the apt repo — one suite per base.
#
# Usage: publish-repo.sh <base> <deb-dir> [series]
#
# Repo layout (reprepro): $REPO_DIR with suite <base>, component main,
# arches arm64+all. Locally unsigned (clients use [trusted=yes]); CI signs
# with the KMS-backed key (SignWith in conf/distributions — TODO when
# signing lands).
#
# Consume on a node:
#   deb [trusted=yes] http://<host>/repo <base> main
set -euo pipefail

MONO="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:?usage: publish-repo.sh <base> <deb-dir> [series]}"
DEBDIR=$(cd "${2:?usage: publish-repo.sh <base> <deb-dir> [series]}" && pwd)
SERIES="${3:-noble}"
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
Architectures: arm64
Description: gb200 kernel pipeline — $b

EOF
done < "$MONO/kernel/upstream-base.txt"

find "$DEBDIR" -maxdepth 2 -name '*.deb' | grep -q . || { echo "!! no debs in $DEBDIR" >&2; exit 1; }

podman run --rm -v "$REPO_DIR:/repo" -v "$DEBDIR:/debs:ro" \
    -e BASE="$BASE" \
    "localhost/gb200-builder:$SERIES" bash -c '
    set -euo pipefail
    command -v reprepro >/dev/null || { apt-get -qq update && apt-get -qq install -y reprepro >/dev/null; }
    cd /repo
    find /debs -maxdepth 2 -name "*.deb" -print0 | sort -z | \
        while IFS= read -r -d "" d; do
            reprepro -S kernel --ignore=wrongdistribution includedeb "$BASE" "$d"
        done
    echo "── suite $BASE now contains:"
    reprepro list "$BASE"
'
echo ">> repo at $REPO_DIR — serve with: python3 -m http.server -d $REPO_DIR"
