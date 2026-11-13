#!/usr/bin/env bash
# Run a command inside the builder container for an Ubuntu series, with this
# repo and $WORK_ROOT mounted at the same paths as outside.
#
# Usage: in-builder.sh <series> <command...>
#   e.g. in-builder.sh noble ./scripts/build-userspace.sh noble
#        in-builder.sh noble ./scripts/build-line.sh 7.3-lts noble
#
# The image comes from env/Containerfile.<series>:
#   podman build -t localhost/kernel-builder:<series> -f env/Containerfile.<series> env/
#
# With rootless podman, root inside the container is you outside, so everything
# written under $WORK_ROOT stays owned by you. The container is disposable:
# INSTALL_BUILD_DEPS=1 is set so scripts may apt-get what a build needs.
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"

MONO="$(cd "$(dirname "$0")/.." && pwd)"
SERIES="${1:?usage: in-builder.sh <series> <command...>}"
shift
[ $# -gt 0 ] || { echo "usage: in-builder.sh <series> <command...>" >&2; exit 1; }
IMAGE="${BUILDER_IMAGE:-localhost/kernel-builder:$SERIES}"

podman image exists "$IMAGE" || {
    echo "!! no image $IMAGE; build it with:" >&2
    echo "   podman build -t $IMAGE -f env/Containerfile.$SERIES env/" >&2
    exit 1
}
mkdir -p "$WORK_ROOT"

exec podman run --rm -i \
    -v "$MONO:$MONO" -v "$WORK_ROOT:$WORK_ROOT" \
    -e WORK_ROOT="$WORK_ROOT" -e VENDOR="$VENDOR" -e RELEASE="${RELEASE:-}" \
    -e KPM_MIRRORS="${KPM_MIRRORS:-}" -e KPM_LINE="${KPM_LINE:-}" \
    -e INSTALL_BUILD_DEPS=1 -e CCACHE_DIR="${CCACHE_DIR:-$WORK_ROOT/ccache}" \
    -e SBOM="${SBOM:-1}" -e LOCKED="${LOCKED:-}" -e KERNEL_LINE="${KERNEL_LINE:-}" \
    -w "$MONO" "$IMAGE" "$@" </dev/null
