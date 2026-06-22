#!/usr/bin/env bash
# Build DOCA/OFED kernel modules against $KVER headers by driving dkms
# directly on the DKMS source packages from the pinned repo.
# Contract: see docs/modules.md (runs inside gb200-builder container).
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

dpkg -i "$HEADERS_DIR"/linux-headers-*.deb >/dev/null 2>&1 || true
apt-get -qq update >/dev/null
apt-get -qq -f install -y >/dev/null

echo "deb [trusted=yes] $SRC ./" > /etc/apt/sources.list.d/doca.list
apt-get -qq update >/dev/null
apt-get -qq install -y dkms

install_pkgs=()
for pkg in $DOCA_DKMS_PKGS; do
    ver=$(apt-cache madison "$pkg" | awk -v want="$MODVER" '
        $3 == want || index($3, want "-") == 1 ||
        index($3, want "+") == 1 || index($3, want "~") == 1 { print $3; exit }
    ')
    if [ -z "$ver" ]; then
        echo "!! no $pkg version matching DOCA pin $MODVER in $SRC" >&2
        apt-cache policy "$pkg" >&2 || true
        exit 1
    fi
    install_pkgs+=("$pkg=$ver")
done
apt-get -qq install -y "${install_pkgs[@]}"

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
