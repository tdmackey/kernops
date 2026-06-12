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
# Fast-iteration knobs are ON (skipdbg/skipabi/skipmodule, no tools packages).
# Release builds should drop them — see docs/build-pipeline.md.
#
# .debs land in the PARENT directory of <source-tree> (dpkg convention).
set -euo pipefail

SRC=$(cd "${1:?usage: build-kernel.sh <source-tree> [flavour] [series]}" && pwd)
FLAVOUR="${2:-generic}"
SERIES="${3:-noble}"

podman volume create --ignore "gb200-ccache-$SERIES" >/dev/null

exec podman run --rm -it \
    -v /Volumes/Linux:/Volumes/Linux \
    -v "gb200-ccache-$SERIES:/ccache" \
    -w "$SRC" \
    "localhost/gb200-builder:$SERIES" \
    bash -c "
        set -euxo pipefail
        export DEB_BUILD_OPTIONS=\"parallel=\$(nproc)\"
        fakeroot debian/rules clean
        fakeroot debian/rules binary-headers binary-$FLAVOUR \
            do_tools=false skipdbg=true skipabi=true skipmodule=true
        ccache -s | head -6
    "
