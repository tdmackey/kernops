#!/usr/bin/env bash
# Boot a kernel that build-line.sh just built, under virtme-ng, and run a command.
# Needs KVM for a usable speed (/dev/kvm) and the virtme-ng package.
#
# Usage: vng-boot.sh <line> [flavour] [-- command...]
#   e.g. vng-boot.sh 7.3-lts
#        vng-boot.sh 7.3-lts kernops -- sh -c 'uname -r; grep -c . /proc/modules'
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"

LINE="${1:?usage: vng-boot.sh <line> [flavour] [-- command...]}"
shift
FLAVOUR="$VENDOR"
if [ $# -gt 0 ] && [ "$1" != "--" ]; then FLAVOUR="$1"; shift; fi
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || set -- sh -c 'echo BOOT-OK; uname -r; uname -v'

SERIES="${SERIES:-$(. /etc/os-release && echo "${VERSION_CODENAME:-}")}"   # which series' build to boot
BUILD="$WORK_ROOT/build/$LINE/$SERIES/tree/debian/build/build-$FLAVOUR"
[ -d "$BUILD" ] || { echo "!! no build at $BUILD (run build-line.sh $LINE <series> first)" >&2; exit 1; }
cd "$BUILD"
exec timeout "${TIMEOUT:-300}" vng --run . -- "$@" </dev/null
