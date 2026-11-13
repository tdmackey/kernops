#!/usr/bin/env bash
# Interactive shell inside a builder container with the work volume mounted.
# Usage: builder-shell.sh [noble|resolute]   (default: noble)
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"
SERIES="${1:-noble}"

podman volume create --ignore "kernel-ccache-$SERIES" >/dev/null
exec podman run --rm -it \
    -v "$WORK_ROOT:$WORK_ROOT" \
    -v "kernel-ccache-$SERIES:/ccache" \
    -w "$WORK_ROOT" \
    "localhost/kernel-builder:$SERIES" \
    bash
