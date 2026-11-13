#!/usr/bin/env bash
# Build DOCA/OFED kernel modules against $KVER headers by driving dkms
# directly on the DKMS source packages from the pinned repo.
# Contract: see docs/modules.md (runs inside kernel-builder container).
#
# The pinned public repo (flat apt layout: Packages + pool/) ships the DKMS
# sources; there is no doca-kernel-support tool in the ubuntu24.04 repo.
# Production uses a custom MRC DOCA build — same contract, different $SRC.
#
# DOCA_DKMS_PKGS selects which DKMS sources to build (default: the OFED
# core). Extend per fleet needs (mlnx-nvme-dkms, xpmem-dkms, ...).
#
# Our patches to a DKMS source are in
#   modules/doca/patches/<DOCA release>/<dkms module>/NNNN-name.patch
# A patch can have a condition, NNNN-name.when, with one line:
#   <header file in the kernel build tree><TAB><extended regular expression>
# The patch is applied only if the kernel's header matches. This is how one
# DOCA release builds for kernels before and after an interface change.
set -euo pipefail
: "${KVER:?}" "${MODVER:?}" "${SRC:?}" "${OUT:?}" "${HEADERS_DIR:?}"
DOCA_DKMS_PKGS="${DOCA_DKMS_PKGS:-mlnx-ofed-kernel-dkms}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive

# The vendor's server ends some downloads in the middle of the file. apt tries
# a failed download again; without this it stops at the first one.
echo 'Acquire::Retries "6";' > /etc/apt/apt.conf.d/80kernops-retries

echo "deb [trusted=yes] $SRC ./" > /etc/apt/sources.list.d/doca.list
apt-get -qq update >/dev/null
apt-get -qq install -y dkms >/dev/null

# MODVER is the DOCA release. The DKMS packages have version numbers of their
# own (mlnx-ofed-kernel-dkms 2607.0.100 in DOCA 3.5.0), so the release is checked
# on the doca-ofed package, and each DKMS package is taken at the version this
# repository has: a repository holds one release.
doca_ofed=$(apt-cache madison doca-ofed | awk -v s="$SRC" 'index($0, s) { print $3; exit }')
case "$doca_ofed" in
    "$MODVER"|"$MODVER"-*|"$MODVER"+*|"$MODVER"~*) ;;
    *) echo "!! $SRC has doca-ofed '${doca_ofed:-none}', not DOCA $MODVER" >&2; exit 1 ;;
esac
install_pkgs=()
for pkg in $DOCA_DKMS_PKGS; do
    ver=$(apt-cache madison "$pkg" | awk -v s="$SRC" 'index($0, s) { print $3; exit }')
    [ -n "$ver" ] || { echo "!! $SRC has no $pkg" >&2; exit 1; }
    install_pkgs+=("$pkg=$ver")
done
echo ">> DOCA $doca_ofed: ${install_pkgs[*]}"

# Order matters. A DKMS package builds itself for every kernel whose headers
# are installed, and installing headers builds every DKMS package. Neither may
# happen before our patches are in the source. So: the sources first, while no
# headers are installed; then the headers, with dkms's hook switched off; then
# the patches; then the build, started by us.
#
# "No headers are installed" must be made true. On resolute, apt installs
# linux-headers-generic with the DKMS package, because the package recommends
# kernel headers. The package then builds itself for Ubuntu's own kernel, and
# its installation fails when that build fails. So: no recommended packages,
# and stop if another kernel's headers are there all the same.
apt-get -qq install -y --no-install-recommends "${install_pkgs[@]}" >/dev/null
# (dpkg-query also lists names that it only knows about: take the installed ones.)
other_headers="$(dpkg-query -W -f='${db:Status-Abbrev}${Package}\n' 'linux-headers-*' 2>/dev/null \
                 | awk '/^i/ { printf "%s ", $2 }' || true)"
[ -z "$other_headers" ] || { echo "!! headers of another kernel are installed: $other_headers" >&2; exit 1; }
chmod -x /etc/kernel/header_postinst.d/dkms 2>/dev/null || true

# The flavour's headers and the common headers they depend on. For a Canonical
# kernel apt could fetch the common ones; for our own they exist only here.
dpkg -i "$HEADERS_DIR"/linux-headers-"$KVER"_*.deb "$HEADERS_DIR"/linux-*-headers-*_all.deb >/dev/null 2>&1 || true
apt-get -qq -f install -y >/dev/null
KBUILD="/lib/modules/$KVER/build"
[ -d "$KBUILD" ] || { echo "!! kernel headers for $KVER did not install from $HEADERS_DIR" >&2; exit 1; }

while read -r mod ver; do
    [ -n "$mod" ] || continue
    for p in "$HERE/patches/$MODVER/$mod"/*.patch; do
        [ -e "$p" ] || continue
        when="${p%.patch}.when"
        if [ -f "$when" ]; then
            IFS=$'\t' read -r header pattern < "$when"
            if ! grep -Eqs -- "$pattern" "$KBUILD/$header"; then
                echo ">> patch ${p##*/}: not needed for $KVER"
                continue
            fi
        fi
        patch -p1 -N --no-backup-if-mismatch -d "/usr/src/$mod-$ver" < "$p" >/dev/null \
            || { echo "!! ${p#"$HERE"/} does not apply to $mod $ver" >&2; exit 1; }
        echo ">> patch ${p##*/}: applied to $mod $ver"
    done
done < <(dkms status | awk -F'[/,:]' '{gsub(/ /, "", $1); gsub(/ /, "", $2); print $1, $2}' | sort -u)

# Build every dkms source the packages registered, for exactly $KVER
while read -r mod ver; do
    [ -n "$mod" ] || continue
    echo ">> dkms build $mod/$ver -k $KVER"
    dkms build "$mod/$ver" -k "$KVER" >/tmp/dkms-build.log 2>&1 || {
        log="/var/lib/dkms/$mod/$ver/build/make.log"
        echo "!! $mod $ver (DOCA $MODVER) does not build for $KVER. Compiler errors:" >&2
        grep -n -m 25 -E 'error:|Error [0-9]|No such file|undeclared|implicit declaration|incompatible' "$log" >&2 \
            || tail -30 "$log" >&2 || tail -30 /tmp/dkms-build.log >&2
        exit 1
    }
done < <(dkms status | awk -F'[/,:]' '{gsub(/ /, "", $1); gsub(/ /, "", $2); print $1, $2}' | sort -u)

find /var/lib/dkms -path "*/$KVER/*" -name '*.ko' -exec cp {} "$OUT/" \;
# OFED symvers for nvidia_peermem (later matrix rows)
find /var/lib/dkms/mlnx-ofed-kernel -path "*$KVER*" -name Module.symvers \
    -exec cp {} "$OUT/Module.symvers" \; -quit || true
[ -f "$OUT/Module.symvers" ] || { echo "!! OFED Module.symvers not found for $KVER" >&2; exit 1; }
ls "$OUT"
