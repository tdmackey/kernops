#!/usr/bin/env bash
# Build an Ubuntu kernel source tree into .debs inside the builder container.
#
# Usage: build-kernel.sh <source-tree> [flavour] [series]
#   source-tree : path to an Ubuntu kernel git tree (checked out at the
#                 tag/branch you want to build — use `git worktree add` to
#                 build a tag without disturbing your main checkout)
#   flavour     : generic | generic-64k | nvidia | nvidia-64k | ... (default: generic)
#   series      : noble | resolute (default: noble) — picks the builder image
#
# The tree is rsynced to a VM-local named volume and built THERE: building
# directly on the virtiofs mount costs ~4-5x wall clock (measured: 3h38m vs
# ~50m of actual CPU; a third of all CPU burned in syscall overhead).
# First sync copies the whole tree; later syncs are incremental.
#
# Fast-iteration knobs are ON (skipdbg/skipabi/skipmodule, no tools packages).
# Release builds should drop them — see docs/build-pipeline.md.
#
# .debs are copied back to /Volumes/Linux/build/out/<flavour>/.
set -euo pipefail

SRC=$(cd "${1:?usage: build-kernel.sh <source-tree> [flavour] [series]}" && pwd)
FLAVOUR="${2:-generic}"
SERIES="${3:-noble}"
TREE_VOL="gb200-tree-$(basename "$SRC")"
OUT="/Volumes/Linux/build/out/$FLAVOUR"

podman volume create --ignore "gb200-ccache-$SERIES" >/dev/null
podman volume create --ignore "$TREE_VOL" >/dev/null
mkdir -p "$OUT"

exec podman run --rm \
    -v /Volumes/Linux:/Volumes/Linux \
    -v "$TREE_VOL:/build" \
    -v "gb200-ccache-$SERIES:/ccache" \
    "localhost/gb200-builder:$SERIES" \
    bash -c "
        set -euxo pipefail
        mkdir -p /build/tree
        rsync -a --delete --exclude=.git '$SRC/' /build/tree/
        cd /build/tree
        export DEB_BUILD_OPTIONS=\"parallel=\$(nproc)\"
        fakeroot debian/rules clean
        time fakeroot debian/rules binary-headers binary-$FLAVOUR \
            do_tools=false skipdbg=true skipabi=true skipmodule=true
        rsync -a /build/*.deb /build/*.buildinfo* '$OUT/' 2>/dev/null || cp /build/*.deb '$OUT/'
        ccache -s | head -4
    "
