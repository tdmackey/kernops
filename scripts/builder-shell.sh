#!/usr/bin/env bash
# Interactive shell inside a builder container with the work volume mounted.
# Usage: builder-shell.sh [noble|resolute]   (default: noble)
set -euo pipefail
SERIES="${1:-noble}"

podman volume create --ignore "gb200-ccache-$SERIES" >/dev/null
exec podman run --rm -it \
    -v /Volumes/Linux:/Volumes/Linux \
    -v "gb200-ccache-$SERIES:/ccache" \
    -w /Volumes/Linux \
    "localhost/gb200-builder:$SERIES" \
    bash
