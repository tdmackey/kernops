#!/usr/bin/env bash
# The end-to-end pipeline for one base, from the monorepo record to an apt
# suite: kernel debs -> module matrix -> publish.
#
# Usage: build-all.sh <base> [flavour] [series]
#   e.g. build-all.sh noble-6.8 generic-64k noble
#
# Steps:
#   1. validate + reconstruct the tree purely from the record (apply-series)
#   2. build kernel debs (VM-local tree, ccache, fast profile by default;
#      RELEASE=1 drops skipdbg)
#   3. PE vmlinuz gate (LP#2098111)
#   4. build the module matrix for the base (DOCA -> nvidia-open, ordered)
#   5. publish everything into the apt repo suite <base>
set -euo pipefail

MONO="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:?usage: build-all.sh <base> [flavour] [series]}"
FLAVOUR="${2:-generic-64k}"
SERIES="${3:-noble}"
OUT="/Volumes/Linux/build/out/$BASE"

echo "════ [1/5] reconstruct from record"
"$MONO/scripts/apply-series.sh" --check "$BASE"
TREE=$(mktemp -d /tmp/gb200-tree.XXXXXX); rmdir "$TREE"
"$MONO/scripts/apply-series.sh" "$BASE" "$TREE"
trap 'git -C "${REPO:-/Volumes/Linux/noble}" worktree remove --force "$TREE" 2>/dev/null || true' EXIT

echo "════ [2/5] kernel debs ($FLAVOUR)"
mkdir -p "$OUT"
podman volume create --ignore "gb200-ccache-$SERIES" >/dev/null
podman volume create --ignore "gb200-tree-$BASE" >/dev/null
podman run --rm \
    -v /Volumes/Linux:/Volumes/Linux -v "$TREE:$TREE" \
    -v "gb200-tree-$BASE:/build" -v "gb200-ccache-$SERIES:/ccache" \
    "localhost/gb200-builder:$SERIES" bash -c "
    set -euo pipefail
    mkdir -p /build/tree
    rsync -a --delete --exclude=.git '$TREE/' /build/tree/
    cd /build/tree
    export DEB_BUILD_OPTIONS=\"parallel=\$(nproc)\"
    fakeroot debian/rules clean
    SKIP='skipdbg=true'; [ -n '${RELEASE:-}' ] && SKIP=''
    fakeroot debian/rules binary-headers binary-$FLAVOUR \
        do_tools=false \$SKIP skipabi=true skipmodule=true
    rm -f /build/tree/../*.ddeb 2>/dev/null || true
    cp /build/*.deb /build/*.buildinfo* '$OUT/' 2>/dev/null || cp /build/*.deb '$OUT/'
    rm -f /build/*.deb /build/*.buildinfo* /build/*.changes 2>/dev/null || true
"

echo "════ [3/5] PE vmlinuz gate"
podman run --rm -v "$OUT:$OUT:ro" "localhost/gb200-builder:$SERIES" bash -c "
    deb=\$(ls $OUT/linux-image-*_arm64.deb | head -1)
    dpkg-deb --fsys-tarfile \"\$deb\" | tar -xO --wildcards './boot/vmlinuz-*' > /tmp/v
    file /tmp/v | grep -q 'PE32+.*Aarch64' || { echo '!! NOT PE:'; file /tmp/v; exit 1; }
    echo \"PE ok: \$(basename \$deb)\"
"

echo "════ [4/5] module matrix"
"$MONO/scripts/build-modules.sh" "$BASE" "$OUT" "$SERIES"

echo "════ [5/5] publish apt suite $BASE"
"$MONO/scripts/publish-repo.sh" "$BASE" "$OUT" "$SERIES"

echo "════ done: suite $BASE"
