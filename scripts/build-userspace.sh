#!/usr/bin/env bash
# Rebuild kernel-coupled userspace packages for an Ubuntu series, as listed in
# userspace/matrix.tsv: a no-change backport of the version a newer series ships.
#
# Usage: build-userspace.sh <target-series> [source-package...]
#   e.g. build-userspace.sh resolute makedumpfile
#        build-userspace.sh noble                  # every noble row, in file order
#
# Run it ON the target series (container, VM or WSL distro of that series): the
# packages must be built with that series' toolchain and libraries.
# Build dependencies must already be installed; the script lists what is missing
# and stops (it never calls apt itself).
#
# Output: $WORK_ROOT/userspace/<series>/*.deb
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"

MONO="$(cd "$(dirname "$0")/.." && pwd)"
SERIES="${1:?usage: build-userspace.sh <target-series> [source-package...]}"
shift
WANT=("$@")

SERIES_VER="$(sed -nE "/^\[series\]/,\$ s/^${SERIES}[[:space:]]*=[[:space:]]*\"([^\"]+)\".*/\1/p" "$MONO/kernel/identity.toml" | head -n1)"
[ -n "$SERIES_VER" ] || { echo "!! series '$SERIES' is not in kernel/identity.toml [series]" >&2; exit 1; }
HERE="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
[ "$HERE" = "$SERIES" ] || { echo "!! this machine is '$HERE'; run on a '$SERIES' system" >&2; exit 1; }

OUT="$WORK_ROOT/userspace/$SERIES"
mkdir -p "$OUT"
exec </dev/null

built=0
while IFS=$'\t' read -r series src from version _why; do
    [[ -z "$series" || "$series" == \#* ]] && continue
    [ "$series" = "$SERIES" ] || continue
    if [ ${#WANT[@]} -gt 0 ]; then
        case " ${WANT[*]} " in *" $src "*) ;; *) continue ;; esac
    fi
    echo "════ $src $version ($from -> $SERIES)"
    work="$OUT/.build-$src"
    rm -rf "$work"; mkdir -p "$work"
    (
        cd "$work"
        python3 "$MONO/scripts/lp-source.py" "$src" "$from" "$version" > urls
        while read -r url; do curl -fsSL --retry 3 -O "$url"; done < urls
        dsc=$(ls ./*.dsc)
        # dpkg-source checks every file against the checksums in the .dsc.
        dpkg-source --no-check -x "$dsc" src >/dev/null
        cd src
        newver="${version}~${SERIES_VER}.1+${VENDOR}1"
        {
            echo "$src ($newver) $SERIES; urgency=medium"
            echo
            echo "  * No-change backport of $version from $from for the $VENDOR kernel."
            echo
            echo " -- $VENDOR kernel pipeline <kernel@example.invalid>  $(date -R)"
            echo
            cat debian/changelog
        } > debian/changelog.new
        mv debian/changelog.new debian/changelog
        if ! missing=$(dpkg-checkbuilddeps 2>&1); then
            echo "!! missing build dependencies for $src:" >&2
            echo "   ${missing#*: Unmet build dependencies: }" >&2
            exit 3
        fi
        DEB_BUILD_OPTIONS="parallel=$(nproc) nocheck" dpkg-buildpackage -us -uc -b > ../build.log 2>&1 || {
            echo "!! $src failed to build; see $work/build.log" >&2
            tail -20 ../build.log >&2
            exit 1
        }
    )
    mv "$work"/*.deb "$OUT/" 2>/dev/null || true
    mv "$work"/*.ddeb "$OUT/" 2>/dev/null || true
    rm -rf "$work"
    built=$((built + 1))
done < "$MONO/userspace/matrix.tsv"

[ "$built" -gt 0 ] || { echo "!! nothing in userspace/matrix.tsv matches $SERIES ${WANT[*]}" >&2; exit 1; }
ls -la "$OUT"/*.deb
