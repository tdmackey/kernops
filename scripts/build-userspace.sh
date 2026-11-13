#!/usr/bin/env bash
# Rebuild kernel-coupled userspace packages for an Ubuntu series, as listed in
# userspace/matrix.tsv: a backport of the version a newer series ships.
#
# The pinned Ubuntu source is the base. Optional layers in the repo are composed
# on top, for any package, in this order:
#   userspace/<pkg>/debian/            packaging files that replace Ubuntu's
#   userspace/<pkg>/<series>/debian/   the same, for one series only
#   userspace/<pkg>/patches/*.patch            source patches (-p1)
#   userspace/<pkg>/<series>/patches/*.patch   the same, for one series only
#   userspace/<pkg>/revision                   our revision of the package (default 1)
# With no userspace/<pkg>/ directory the package is a no-change backport.
#
# userspace/sources.lock records the SHA-256 of every .dsc. A download that does
# not match fails the build; a source seen for the first time is added to it.
#
# Usage: build-userspace.sh <target-series> [source-package...]
#   e.g. build-userspace.sh resolute makedumpfile
#        build-userspace.sh noble                  # every noble row, in file order
#
# Run it ON the target series (container, VM or WSL distro of that series): the
# packages must be built with that series' toolchain and libraries.
# Build dependencies must already be installed; the script lists what is missing
# and stops. Inside the disposable builder container (scripts/in-builder.sh sets
# INSTALL_BUILD_DEPS=1) it installs them itself, and it serves the packages it
# has already built as a local apt source, so a later package can build against
# an earlier one (bpfcc and bpftrace against our libbpf).
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

LOCK="$MONO/userspace/sources.lock"
AUTO="${INSTALL_BUILD_DEPS:-}"
refresh_local_repo() {
    [ -n "$AUTO" ] || return 0
    ( cd "$OUT" && dpkg-scanpackages -m . > Packages 2>/dev/null )
    echo "deb [trusted=yes] file:$OUT ./" > /etc/apt/sources.list.d/local-userspace.list
    apt-get update -qq
    # Whatever the image already has installed must give way to our build of it,
    # or a package compiles against our headers and links the series' old library.
    apt-get -y -qq upgrade >/dev/null
}
# bpfcc and bpftrace generate BPF skeletons with bpftool at build time. Ubuntu
# only builds bpftool from its own `linux` source, so a series whose kernel is
# older than ours has no usable one. Take it from our kernel's tools package for
# this series (scripts/build-line.sh) and install it in the builder as a
# build-only `bpftool` package. It is not shipped: at run time bpftool comes
# from linux-<vendor>-tools.
provide_bpftool() {
    [ -n "$AUTO" ] || return 0
    dpkg -s bpftool >/dev/null 2>&1 && return 0
    local arch tools tmp bin ver
    arch="$(dpkg --print-architecture)"
    tools="$(ls -t "$WORK_ROOT"/build/*/linux-"$VENDOR"-tools-[0-9]*_*~"$SERIES_VER".*_"$arch".deb 2>/dev/null | head -n1 || true)"
    [ -n "$tools" ] || { echo "   (no $VENDOR kernel tools package for $SERIES yet: no bpftool for the builder)"; return 0; }
    tmp="$(mktemp -d)"
    dpkg-deb -x "$tools" "$tmp/x"
    bin="$(find "$tmp/x" -type f -name bpftool | head -n1)"
    [ -n "$bin" ] || { echo "!! $tools contains no bpftool" >&2; exit 1; }
    ver="$(dpkg-deb -f "$tools" Version)"
    mkdir -p "$tmp/p/DEBIAN" "$tmp/p/usr/sbin"
    install -m 0755 "$bin" "$tmp/p/usr/sbin/bpftool"
    printf '%s\n' "Package: bpftool" "Version: $ver+buildonly" "Architecture: $arch" \
        "Maintainer: $VENDOR kernel pipeline <kernel@example.invalid>" \
        "Replaces: linux-tools-common" "Section: devel" "Priority: optional" \
        "Description: bpftool from the $VENDOR kernel, for building packages only" \
        > "$tmp/p/DEBIAN/control"
    dpkg-deb --build "$tmp/p" "$tmp/bpftool.deb" >/dev/null
    dpkg -i "$tmp/bpftool.deb" >/dev/null
    bpftool version >/dev/null || { echo "!! bpftool from $tools does not run here:" >&2; ldd /usr/sbin/bpftool | grep 'not found' >&2; exit 1; }
    echo "   bpftool for the builder: $(bpftool version | head -n1) (from ${tools##*/})"
    rm -rf "$tmp"
}

# The same packages use kernel interfaces by name (bpftrace: BPF_TRACE_KPROBE_SESSION),
# so they need our kernel's userspace API headers, not the series' linux-libc-dev.
# Install them over the builder's copy, from the tree the kernel was built from.
provide_uapi_headers() {
    [ -n "$AUTO" ] || return 0
    local tree tmp multiarch
    tree="$(ls -dt "$WORK_ROOT"/build/*/tree 2>/dev/null | head -n1 || true)"
    [ -n "$tree" ] && [ -f "$tree/Makefile" ] || { echo "   (no $VENDOR kernel tree: the builder keeps $SERIES's kernel headers)"; return 0; }
    tmp="$(mktemp -d)"
    make -s -C "$tree" O="$tmp/obj" INSTALL_HDR_PATH="$tmp/hdr" headers_install >"$tmp/log" 2>&1         || { echo "!! cannot install kernel headers from $tree" >&2; tail -5 "$tmp/log" >&2; exit 1; }
    multiarch="$(dpkg-architecture -qDEB_HOST_MULTIARCH)"
    mkdir -p "/usr/include/$multiarch/asm"
    cp -a "$tmp/hdr/include/asm/." "/usr/include/$multiarch/asm/"
    rm -rf "$tmp/hdr/include/asm"
    cp -a "$tmp/hdr/include/." /usr/include/
    echo "   kernel headers for the builder: $(make -s -C "$tree" kernelversion 2>/dev/null) (from ${tree#"$WORK_ROOT"/})"
    rm -rf "$tmp"
}
if [ -n "$AUTO" ]; then
    export DEBIAN_FRONTEND=noninteractive
    refresh_local_repo
    command -v curl >/dev/null || apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null
    provide_bpftool
    provide_uapi_headers
fi

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
        # Pin the source: the .dsc lists the checksum of every other file, and
        # dpkg-source verifies those, so one hash covers the whole download.
        sum=$(sha256sum "$dsc" | cut -d' ' -f1)
        want=$(awk -F'\t' -v s="$src" -v v="$version" '$1==s && $2==v {print $3}' "$LOCK" 2>/dev/null || true)
        if [ -z "$want" ]; then
            # Record it now, so a failed build does not lose the pin.
            { [ -e "$LOCK" ] && cat "$LOCK"; printf '%s\t%s\t%s\n' "$src" "$version" "$sum"; } \
                | LC_ALL=C sort -u > "$LOCK.tmp" && mv "$LOCK.tmp" "$LOCK"
            echo "   new source: recorded sha256 $sum (commit userspace/sources.lock)"
        elif [ "$want" != "$sum" ]; then
            echo "!! $dsc sha256 $sum does not match userspace/sources.lock ($want)" >&2
            exit 4
        fi
        dpkg-source --no-check -x "$dsc" src >/dev/null
        cd src
        # Compose our layers on top of Ubuntu's source.
        changed=""
        for layer in "$MONO/userspace/$src" "$MONO/userspace/$src/$SERIES"; do
            if [ -d "$layer/debian" ]; then
                cp -a "$layer/debian/." debian/
                changed="packaging"
            fi
        done
        for layer in "$MONO/userspace/$src" "$MONO/userspace/$src/$SERIES"; do
            for p in "$layer"/patches/*.patch; do
                [ -e "$p" ] || continue
                patch -p1 --no-backup-if-mismatch < "$p" > /dev/null || {
                    echo "!! ${p#"$MONO"/} does not apply to $src $version" >&2
                    exit 5
                }
                changed="${changed:+$changed and }source"
            done
        done
        what="No-change backport"
        [ -n "$changed" ] && what="Backport with $changed changes (see userspace/$src/ in the $VENDOR kernel repo)"
        # Bump userspace/<pkg>/revision when our layers change for the same source version.
        rev="$(cat "$MONO/userspace/$src/revision" 2>/dev/null || echo 1)"
        newver="${version}~${SERIES_VER}.1+${VENDOR}${rev}"
        {
            echo "$src ($newver) $SERIES; urgency=medium"
            echo
            echo "  * $what of $version from $from for the $VENDOR kernel."
            echo
            echo " -- $VENDOR kernel pipeline <kernel@example.invalid>  $(date -R)"
            echo
            cat debian/changelog
        } > debian/changelog.new
        mv debian/changelog.new debian/changelog
        if ! missing=$(dpkg-checkbuilddeps 2>&1); then
            if [ -n "$AUTO" ]; then
                mk-build-deps -i -r -t "apt-get -y --no-install-recommends" debian/control > ../deps.log 2>&1 || {
                    echo "!! cannot satisfy build dependencies for $src on $SERIES:" >&2
                    grep -E 'Depends:|Breaks:|Conflicts:|not installable|is to be installed|^E:' ../deps.log | head -12 >&2
                    exit 3
                }
                rm -f ./*build-deps*.buildinfo ./*build-deps*.changes
            else
                echo "!! missing build dependencies for $src:" >&2
                echo "   ${missing#*: }" >&2
                exit 3
            fi
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
    refresh_local_repo
done < "$MONO/userspace/matrix.tsv"

[ "$built" -gt 0 ] || { echo "!! nothing in userspace/matrix.tsv matches $SERIES ${WANT[*]}" >&2; exit 1; }
ls -la "$OUT"/*.deb
