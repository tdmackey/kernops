#!/usr/bin/env bash
# Build an upstream-stable line (e.g. 7.3-lts) natively on Linux: materialise the
# tree from the record, render our packaging, settle the config, build the debs.
#
# Usage: build-line.sh <line> <series> [debian/rules targets...]
#   e.g. build-line.sh 7.3-lts resolute
#        build-line.sh 7.3-lts resolute binary-headers binary-kernops   # no tools
#
# Run it on the Ubuntu series you are building for (a container, VM or WSL
# distro of that series): the toolchain is part of the result.
#
# Environment:
#   RELEASE=1   full profile (debug symbols, ABI and module checks)
#   WORK_ROOT   see scripts/lib/common.sh; must be a case-sensitive filesystem
#
# Deliberately NOT run under fakeroot. The packaging declares
# Rules-Requires-Root: no, and fakeroot funnels every file operation of every
# compiler through one daemon: measured here, it left a 24-core machine 82% idle
# and made the build about five times slower.
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"

MONO="$(cd "$(dirname "$0")/.." && pwd)"
LINE="${1:?usage: build-line.sh <line> <series> [debian/rules targets...]}"
SERIES="${2:?usage: build-line.sh <line> <series> [debian/rules targets...]}"
shift 2
KPM="$MONO/scripts/kpm"

# A fixed build path per line keeps ccache hits across runs.
OUT="$WORK_ROOT/build/$LINE"
TREE="$OUT/tree"
ARCH="$(dpkg --print-architecture)"

if [ $# -eq 0 ]; then
    set -- binary-headers "binary-$VENDOR"
    [ "$ARCH" = arm64 ] && set -- "$@" "binary-$VENDOR-64k"
    set -- "$@" binary-perarch          # tools: perf, bpftool, cpupower, Hyper-V daemons, ...
fi
PROFILE=(skipdbg=true skipabi=true skipmodule=true)
[ -n "${RELEASE:-}" ] && PROFILE=()

echo "════ [1/4] reconstruct $LINE from the record"
"$KPM" --line "$LINE" verify --strict
rm -rf "$TREE"
mkdir -p "$OUT"
rm -f "$OUT"/*.deb "$OUT"/*.buildinfo "$OUT"/*.changes
"$KPM" --line "$LINE" apply "$TREE"

echo "════ [2/4] render packaging for $SERIES"
"$KPM" --line "$LINE" packaging "$TREE" --series "$SERIES"

cd "$TREE"
# A clean PATH: on WSL the default one searches Windows directories for every command.
export PATH="/usr/lib/ccache:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export DEB_BUILD_OPTIONS="parallel=$(nproc)"
export CCACHE_DIR="${CCACHE_DIR:-$WORK_ROOT/ccache}"
# Cache hits must not depend on where the tree lives. For a RELEASE build this
# also means cached objects may carry another directory's debug-info paths.
export CCACHE_BASEDIR="$WORK_ROOT/build" CCACHE_NOHASHDIR=1
# Kconfig prompts must take their defaults; never let them read our stdin.
exec </dev/null

echo "════ [3/4] settle the config for this toolchain"
debian/rules clean >/dev/null
# The first pass records compiler-dependent values and reports them as changes
# (non-zero exit). The second pass must be clean.
debian/rules updateconfigs >"$OUT/updateconfigs.log" 2>&1 || true
if ! debian/rules updateconfigs >>"$OUT/updateconfigs.log" 2>&1; then
    echo "!! config does not settle; see $OUT/updateconfigs.log" >&2
    grep -E 'check-config: CONFIG' "$OUT/updateconfigs.log" | tail -20 >&2
    exit 1
fi

echo "════ [4/4] build: $*"
debian/rules "$@" "${PROFILE[@]}" >"$OUT/build.log" 2>&1 || {
    echo "!! build failed; see $OUT/build.log" >&2
    grep -nE ' error: |\*\*\* ' "$OUT/build.log" | head -20 >&2
    exit 1
}
ls -la "$OUT"/*.deb
