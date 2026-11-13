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
set -euo pipefail
: "${KVER:?}" "${MODVER:?}" "${SRC:?}" "${OUT:?}" "${HEADERS_DIR:?}"
DOCA_DKMS_PKGS="${DOCA_DKMS_PKGS:-mlnx-ofed-kernel-dkms}"

# The flavour's headers and the common headers they depend on. For a Canonical
# kernel apt could fetch the common ones; for our own they exist only here.
dpkg -i "$HEADERS_DIR"/linux-headers-"$KVER"_*.deb "$HEADERS_DIR"/linux-*-headers-*_all.deb >/dev/null 2>&1 || true
apt-get -qq update >/dev/null
apt-get -qq -f install -y >/dev/null
[ -d "/lib/modules/$KVER/build" ] || { echo "!! kernel headers for $KVER did not install from $HEADERS_DIR" >&2; exit 1; }

echo "deb [trusted=yes] $SRC ./" > /etc/apt/sources.list.d/doca.list
apt-get -qq update >/dev/null
apt-get -qq install -y dkms

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
# Installing a DKMS package builds it for the installed headers. If that build
# fails, the reason is in its make.log, and the container is about to go away.
apt-get -qq install -y "${install_pkgs[@]}" || {
    echo "!! a DKMS package of DOCA $MODVER does not build for $KVER. Compiler errors:" >&2
    for log in /var/lib/dkms/*/*/build/make.log; do
        [ -f "$log" ] || continue
        echo "   --- $log" >&2
        grep -n -m 25 -E 'error:|Error [0-9]|No such file|undeclared|implicit declaration|incompatible' "$log" >&2 || tail -30 "$log" >&2
    done
    exit 1
}

# Build every dkms source the packages registered, for exactly $KVER
while read -r mod ver; do
    echo ">> dkms build $mod/$ver -k $KVER"
    dkms build "$mod/$ver" -k "$KVER" || { cat "/var/lib/dkms/$mod/$ver/build/make.log" 2>/dev/null | tail -40; exit 1; }
done < <(dkms status | awk -F'[/,]' '{print $1, $2}' | sort -u)

find /var/lib/dkms -path "*/$KVER/*" -name '*.ko' -exec cp {} "$OUT/" \;
# OFED symvers for nvidia_peermem (later matrix rows)
find /var/lib/dkms/mlnx-ofed-kernel -path "*$KVER*" -name Module.symvers \
    -exec cp {} "$OUT/Module.symvers" \; -quit || true
[ -f "$OUT/Module.symvers" ] || { echo "!! OFED Module.symvers not found for $KVER" >&2; exit 1; }
ls "$OUT"
