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
# userspace/sources.lock records the SHA-256 of every .dsc. The .dsc lists the
# checksum of every other source file, and dpkg-source checks those, so one
# hash pins the whole source. A download that does not match fails the build.
# A source seen for the first time is added to the lock, unless LOCKED=1.
#
# Usage: build-userspace.sh <target-series> [source-package...]
#   e.g. build-userspace.sh resolute makedumpfile
#        build-userspace.sh noble                  # every noble row, in file order
#
# Environment:
#   LOCKED=1        fail on a source that is not in sources.lock (set it in CI
#                   and for releases; RELEASE=1 implies it)
#   KERNEL_LINE     the kernel line whose build supplies bpftool and the kernel
#                   headers (default: the newest build for this series)
#
# Run it ON the target series (container, VM or WSL distro of that series): the
# packages must be built with that series' toolchain and libraries.
# Build dependencies must already be installed; the script lists what is missing
# and stops. Inside the disposable builder container (scripts/in-builder.sh sets
# INSTALL_BUILD_DEPS=1) it installs them itself. It also serves the packages it
# has already built as a local apt source, so a later package builds against an
# earlier one, and it installs bpftool and the kernel headers of our own kernel
# build for the series, when there is one.
#
# Output in $WORK_ROOT/userspace/<series>/:
#   *.deb               the packages
#   buildinfo/          the build environment of each package
#   .built/<pkg>.json   what was built from which source (read by the status page)
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
WORK="$WORK_ROOT/userspace/.work/$SERIES"      # outside $OUT: a half-built package must not be served
mkdir -p "$OUT/buildinfo" "$OUT/.built" "$WORK"
exec </dev/null

LOCK="$MONO/userspace/sources.lock"
AUTO="${INSTALL_BUILD_DEPS:-}"
LOCKED="${LOCKED:-${RELEASE:-}}"
KERNEL_HEADERS="the series' own"                # what the packages were compiled against; recorded

refresh_local_repo() {
    [ -n "$AUTO" ] || return 0
    ( cd "$OUT" && dpkg-scanpackages -m . > Packages 2>/dev/null )
    echo "deb [trusted=yes] file:$OUT ./" > /etc/apt/sources.list.d/local-userspace.list
    apt-get update -qq
    # What the image already has installed must give way to our build of it, or a
    # package compiles against our headers and links the series' old library.
    # Only our repository is consulted: the builder does not drift with the archive.
    apt-get -y -qq --with-new-pkgs upgrade \
        -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/local-userspace.list -o Dir::Etc::sourceparts=- >/dev/null
}

# bpfcc and bpftrace generate BPF skeletons with bpftool at build time, and use
# kernel interfaces by name (bpftrace: BPF_TRACE_KPROBE_SESSION). Ubuntu builds
# bpftool only from its own `linux` source, and a series' linux-libc-dev is as
# old as its kernel. Both therefore come from our own kernel build for this
# series (scripts/build-line.sh), installed into the builder only:
#   - bpftool, as a build-only package (at run time it comes from linux-<vendor>-tools)
#   - the kernel's userspace API headers, over the builder's copy
# The two always come from the same build.
kernel_build_dir() {
    local arch pattern
    arch="$(dpkg --print-architecture)"
    pattern="linux-$VENDOR-tools-[0-9]*_*_$arch.deb"
    if [ -n "${KERNEL_LINE:-}" ]; then
        # shellcheck disable=SC2086
        ls "$WORK_ROOT/build/$KERNEL_LINE/$SERIES"/$pattern >/dev/null 2>&1 && echo "$WORK_ROOT/build/$KERNEL_LINE/$SERIES"
        return 0
    fi
    # shellcheck disable=SC2086
    ls -t "$WORK_ROOT"/build/*/"$SERIES"/$pattern 2>/dev/null | head -n1 | xargs -r dirname || true
}

provide_kernel_tools() {
    [ -n "$AUTO" ] || return 0
    local dir arch tools tmp bin ver multiarch
    dir="$(kernel_build_dir)"
    if [ -z "$dir" ] || [ ! -f "$dir/tree/Makefile" ]; then
        echo "   (no $VENDOR kernel build for $SERIES: no bpftool, and $SERIES's own kernel headers)"
        return 0
    fi
    arch="$(dpkg --print-architecture)"
    # shellcheck disable=SC2012
    tools="$(ls -t "$dir"/linux-"$VENDOR"-tools-[0-9]*_*_"$arch".deb | head -n1)"
    tmp="$(mktemp -d)"

    dpkg-deb -x "$tools" "$tmp/x"
    bin="$(find "$tmp/x" -type f -name bpftool | head -n1)"
    [ -n "$bin" ] || { echo "!! $tools contains no bpftool" >&2; exit 1; }
    ver="$(dpkg-deb -f "$tools" Version)"
    mkdir -p "$tmp/p/DEBIAN" "$tmp/p/usr/sbin"
    install -m 0755 "$bin" "$tmp/p/usr/sbin/bpftool"
    # The epoch keeps a bpftool from the archive from replacing this one.
    printf '%s\n' "Package: bpftool" "Version: 99:$ver+buildonly" "Architecture: $arch" \
        "Maintainer: $VENDOR kernel pipeline <kernel@example.invalid>" \
        "Replaces: linux-tools-common" "Section: devel" "Priority: optional" \
        "Description: bpftool from the $VENDOR kernel, for building packages only" \
        > "$tmp/p/DEBIAN/control"
    dpkg-deb --build "$tmp/p" "$tmp/bpftool.deb" >/dev/null
    dpkg -i "$tmp/bpftool.deb" >/dev/null
    bpftool version >/dev/null || { echo "!! bpftool from $tools does not run here:" >&2; ldd /usr/sbin/bpftool | grep 'not found' >&2; exit 1; }

    make -s -C "$dir/tree" O="$tmp/obj" INSTALL_HDR_PATH="$tmp/hdr" headers_install >"$tmp/log" 2>&1 \
        || { echo "!! cannot install kernel headers from $dir/tree" >&2; tail -5 "$tmp/log" >&2; exit 1; }
    multiarch="$(dpkg-architecture -qDEB_HOST_MULTIARCH)"
    mkdir -p "/usr/include/$multiarch/asm"
    cp -a "$tmp/hdr/include/asm/." "/usr/include/$multiarch/asm/"
    rm -rf "$tmp/hdr/include/asm"
    cp -a "$tmp/hdr/include/." /usr/include/
    KERNEL_HEADERS="$(make -s -C "$dir/tree" kernelversion 2>/dev/null) (${dir#"$WORK_ROOT"/build/})"
    echo "   from our kernel build ${dir#"$WORK_ROOT"/}: $(bpftool version | head -n1), kernel headers ${KERNEL_HEADERS%% *}"
    rm -rf "$tmp"
}

if [ -n "$AUTO" ]; then
    export DEBIAN_FRONTEND=noninteractive
    refresh_local_repo
    command -v curl >/dev/null || apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null
    provide_kernel_tools
fi

# Every package named on the command line must be a row for this series.
for w in "${WANT[@]}"; do
    awk -F'\t' -v s="$SERIES" -v p="$w" '$1==s && $2==p {found=1} END {exit !found}' "$MONO/userspace/matrix.tsv" \
        || { echo "!! userspace/matrix.tsv has no '$w' row for $SERIES" >&2; exit 1; }
done

built=0
# The matrix is read on descriptor 3: the builds below must not see it as their input.
while IFS=$'\t' read -r -u 3 series src from version _why; do
    [[ -z "$series" || "$series" == \#* ]] && continue
    [ "$series" = "$SERIES" ] || continue
    if [ ${#WANT[@]} -gt 0 ]; then
        case " ${WANT[*]} " in *" $src "*) ;; *) continue ;; esac
    fi
    echo "════ $src $version ($from -> $SERIES)"
    work="$WORK/$src"
    rm -rf "$work"; mkdir -p "$work"
    (
        cd "$work"
        python3 "$MONO/scripts/lp-source.py" "$src" "$from" "$version" > urls
        while read -r url; do
            curl -fsSL --retry 3 --proto '=https' --proto-redir '=https' -O "$url"
        done < urls
        dsc=$(ls ./*.dsc)
        sum=$(sha256sum "$dsc" | cut -d' ' -f1)
        # "" forces a string comparison: 1.10 must not equal 1.1.
        want=$(awk -F'\t' -v s="$src" -v v="$version" '$1==s && $2"" == v"" {print $3}' "$LOCK")
        if [ -z "$want" ]; then
            [ -z "$LOCKED" ] || { echo "!! $src $version is not in userspace/sources.lock (LOCKED)" >&2; exit 4; }
            # Record it now, so a failed build does not lose the pin. The lock is
            # shared by every series' build: one writer at a time.
            (
                flock 9
                tmp="$(mktemp "$LOCK.XXXXXX")"
                { cat "$LOCK"; printf '%s\t%s\t%s\n' "$src" "$version" "$sum"; } | LC_ALL=C sort -u > "$tmp"
                mv "$tmp" "$LOCK"
            ) 9>>"$LOCK.lock"
            rm -f "$LOCK.lock"
            echo "   new source: recorded sha256 $sum (commit userspace/sources.lock)"
        elif [ "$want" != "$sum" ]; then
            echo "!! $dsc sha256 $sum does not match userspace/sources.lock ($want)" >&2
            exit 4
        fi
        # Checks every file against the checksums in the .dsc. Its signature is
        # not checked (the builder has no keyring); the lock is what we trust.
        dpkg-source -x "$dsc" src >/dev/null 2>dpkg-source.log || { cat dpkg-source.log >&2; exit 4; }
        cd src
        upstream_date="$(dpkg-parsechangelog -S Date)"

        # Compose our layers on top of Ubuntu's source.
        packaging="" patched=""
        for layer in "$MONO/userspace/$src" "$MONO/userspace/$src/$SERIES"; do
            if [ -d "$layer/debian" ]; then
                cp -a "$layer/debian/." debian/
                packaging=1
            fi
        done
        for layer in "$MONO/userspace/$src" "$MONO/userspace/$src/$SERIES"; do
            for p in "$layer"/patches/*.patch; do
                [ -e "$p" ] || continue
                patch -p1 -N --no-backup-if-mismatch < "$p" > /dev/null || {
                    echo "!! ${p#"$MONO"/} does not apply to $src $version" >&2
                    exit 5
                }
                patched=1
            done
        done
        what="No-change backport"
        [ -z "$packaging$patched" ] || what="Backport with changes (see userspace/$src/ in the $VENDOR kernel repo)"
        # Our revision of the package: increase it when our layers change for the same source version.
        rev="$(cat "$MONO/userspace/$src/revision" 2>/dev/null || true)"
        rev="${rev:-1}"
        [[ "$rev" =~ ^[0-9]+$ ]] || { echo "!! userspace/$src/revision must be a number, not '$rev'" >&2; exit 5; }
        newver="${version}~${SERIES_VER}.1+${VENDOR}${rev}"
        {
            echo "$src ($newver) $SERIES; urgency=medium"
            echo
            echo "  * $what of $version from $from for the $VENDOR kernel."
            echo
            # Ubuntu's date, not today's: the same inputs give the same package.
            echo " -- $VENDOR kernel pipeline <kernel@example.invalid>  $upstream_date"
            echo
            cat debian/changelog
        } > debian/changelog.new
        mv debian/changelog.new debian/changelog
        echo "$newver" > ../our-version

        if ! missing=$(dpkg-checkbuilddeps 2>&1); then
            if [ -n "$AUTO" ]; then
                mk-build-deps -i -r -t "apt-get -y --no-install-recommends" debian/control > ../deps.log 2>&1 </dev/null || {
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
        DEB_BUILD_OPTIONS="parallel=$(nproc) nocheck" dpkg-buildpackage -us -uc -b > ../build.log 2>&1 </dev/null || {
            echo "!! $src failed to build; see $work/build.log" >&2
            tail -20 ../build.log >&2
            exit 1
        }
    )
    # Replace what an earlier build of this source left: two versions of one
    # package in the local repository would both be offered to later builds.
    record="$OUT/.built/$src.json"
    if [ -f "$record" ]; then
        python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).get("packages", [])))' "$record" \
            | while read -r old; do [ -z "$old" ] || rm -f "$OUT/$old"; done
    fi
    ls "$work"/*.deb >/dev/null 2>&1 || { echo "!! $src built no package" >&2; exit 1; }
    debs=()
    for f in "$work"/*.deb "$work"/*.ddeb; do
        [ -e "$f" ] || continue
        mv "$f" "$OUT/"
        debs+=("${f##*/}")
    done
    mv "$work"/*.buildinfo "$OUT/buildinfo/" 2>/dev/null || true
    python3 - "$record" "$src" "$version" "$from" "$(cat "$work/our-version")" "$KERNEL_HEADERS" "${debs[@]}" <<'EOF'
import datetime, json, sys
path, src, version, frm, ours, headers, *debs = sys.argv[1:]
json.dump({"source": src, "version": version, "from": frm, "our_version": ours, "kernel_headers": headers,
           "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "packages": sorted(debs)},
          open(path, "w", encoding="utf-8", newline="\n"), indent=1)
EOF
    rm -rf "$work"
    built=$((built + 1))
    refresh_local_repo
done 3< "$MONO/userspace/matrix.tsv"

[ "$built" -gt 0 ] || { echo "!! nothing in userspace/matrix.tsv matches $SERIES ${WANT[*]}" >&2; exit 1; }
echo "$built source package(s) built for $SERIES; packages are in $OUT"
