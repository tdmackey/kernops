#!/usr/bin/env bash
# The end-to-end pipeline for one base, from the monorepo record to an apt
# suite: kernel debs -> module matrix -> publish.
#
# Usage: build-all.sh <base> [flavour] [series] [arch]
#   e.g. build-all.sh hwe-7.0 generic-64k noble arm64
#        build-all.sh hwe-7.0 generic noble amd64
#
# Steps:
#   1. validate + reconstruct the tree purely from the record (apply-series)
#   2. build kernel debs (VM-local tree, ccache, fast profile by default;
#      RELEASE=1 drops skipdbg)
#   3. PE vmlinuz gate (LP#2098111)
#   4. build the module matrix for the base (DOCA -> nvidia-open, ordered)
#   5. publish everything into the apt repo suite <base>
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"

MONO="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:?usage: build-all.sh <base> [flavour] [series] [arch]}"
SERIES="${3:-noble}"
# shellcheck source=scripts/lib/arch.sh
. "$MONO/scripts/lib/arch.sh"
ARCH="$(arch_normalize "${4:-${ARCH:-arm64}}")"
FLAVOUR="${2:-$(arch_default_flavour "$BASE" "$ARCH")}"
# WORK_ROOT must be a path the podman VM mounts (macOS: /Volumes/Linux —
# NOT /tmp, which lives outside the VM). CI runners override this.
REPO="${REPO:-$WORK_ROOT/noble}"
PUBLISH="${PUBLISH:-1}"
MODULE_SOURCE_PREFLIGHT="${MODULE_SOURCE_PREFLIGHT:-1}"
BUILDER_IMAGE="${BUILDER_IMAGE:-localhost/kernel-builder:$SERIES}"
OUT_ROOT="$WORK_ROOT/build/out/$BASE/$ARCH"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
OUT="$OUT_ROOT/$RUN_ID"
[ -e "$OUT" ] && { echo "!! output dir already exists: $OUT" >&2; exit 1; }

echo "════ [1/5] reconstruct from record ($BASE $ARCH)"
REPO="$REPO" "$MONO/scripts/apply-series.sh" --check "$BASE"
TREE="$WORK_ROOT/build/.tree-$BASE-$ARCH"
git -C "$REPO" worktree remove --force "$TREE" 2>/dev/null || rm -rf "$TREE"
REPO="$REPO" "$MONO/scripts/apply-series.sh" "$BASE" "$TREE"
trap 'git -C "$REPO" worktree remove --force "$TREE" 2>/dev/null || true' EXIT

if [ "$MODULE_SOURCE_PREFLIGHT" = 1 ]; then
    echo "════ [preflight] module sources ($BASE $ARCH)"
    "$MONO/scripts/check-module-sources.sh" "$BASE" "$ARCH" "$SERIES"
fi

echo "════ [2/5] kernel debs ($FLAVOUR $ARCH)"
mkdir -p "$OUT"
podman volume create --ignore "kernel-ccache-$SERIES-$ARCH" >/dev/null
podman volume create --ignore "kernel-tree-$BASE-$ARCH" >/dev/null
podman run --rm \
    -v "$WORK_ROOT:$WORK_ROOT" \
    -v "kernel-tree-$BASE-$ARCH:/build" -v "kernel-ccache-$SERIES-$ARCH:/ccache" \
    "$BUILDER_IMAGE" bash -c "
    set -euo pipefail
    mkdir -p /build/tree
    rsync -a --delete --exclude=.git '$TREE/' /build/tree/
    cd /build/tree
    export DEB_BUILD_OPTIONS=\"parallel=\$(nproc)\"
    fakeroot debian/rules clean
    SKIP='skipdbg=true'; [ -n '${RELEASE:-}' ] && SKIP=''
    fakeroot debian/rules binary-headers binary-$FLAVOUR \
        do_tools=false \$SKIP skipabi=true skipmodule=true
    # release builds keep the ddebs (dbg artifacts ship in the bundle)
    cp /build/*.deb '$OUT/'
    cp /build/*.ddeb /build/*.buildinfo* '$OUT/' 2>/dev/null || true
    rm -f /build/*.deb /build/*.ddeb /build/*.buildinfo* /build/*.changes 2>/dev/null || true
"

echo "════ [3/5] PE vmlinuz gate"
ARCH_FILE_GREP="$(arch_file_grep "$ARCH")"
podman run --rm -v "$OUT:$OUT:ro" "$BUILDER_IMAGE" bash -c "
    deb=\$(ls $OUT/linux-image-*_${ARCH}.deb | head -1)
    dpkg-deb --fsys-tarfile \"\$deb\" | tar -xO --wildcards './boot/vmlinuz-*' > /tmp/v
    file /tmp/v | grep -Eq '$ARCH_FILE_GREP' || { echo '!! unexpected kernel image:'; file /tmp/v; exit 1; }
    echo \"kernel image ok: \$(basename \$deb)\"
"

echo "════ [4/5] module matrix"
"$MONO/scripts/build-modules.sh" "$BASE" "$OUT" "$SERIES" "$ARCH"
(
    cd "$OUT"
    find . -maxdepth 2 -name '*.deb' -print | sort > .publish-manifest
)
python3 "$MONO/scripts/write-provenance.py" \
    --repo-root "$MONO" --kernel-repo "$REPO" --tree "$TREE" --out-dir "$OUT" \
    --base "$BASE" --arch "$ARCH" --flavour "$FLAVOUR" --series "$SERIES" --run-id "$RUN_ID"

if [ "$PUBLISH" = 1 ]; then
    echo "════ [5/5] publish apt suite $BASE ($ARCH)"
    "$MONO/scripts/publish-repo.sh" "$BASE" "$OUT" "$SERIES" "$ARCH"
else
    echo "════ [5/5] publish skipped (PUBLISH=$PUBLISH)"
fi

echo "════ done: suite $BASE arch=$ARCH artifacts=$OUT"
