#!/usr/bin/env bash
# Build DOCA/OFED kernel modules against $KVER headers, using NVIDIA's
# doca-kernel-support DKMS sources (we use their build, not their install).
# Contract: see docs/modules.md (runs inside gb200-builder container).
#
# STATUS: skeleton — the doca-kernel-support invocation needs validating
# against the pinned DOCA repo for each base (and LP #2139667 may hand us
# prebuilt debs for noble eventually). Do not trust until exercised.
set -euo pipefail
: "${KVER:?}" "${MODVER:?}" "${SRC:?}" "${OUT:?}" "${HEADERS_DEB:?}"

dpkg -i "$HEADERS_DEB" >/dev/null 2>&1 || apt-get -qq -f install -y >/dev/null

echo "deb [trusted=yes] $SRC ./" > /etc/apt/sources.list.d/doca.list
apt-get -qq update
apt-get -qq install -y doca-kernel-support dkms

# doca-kernel-support drives dkms builds of mlnx-ofed modules for a target
# kernel; module signing env is supported (WITH_MOD_SIGN=1 + key paths) but
# we sign centrally in build-modules.sh instead.
/opt/mellanox/doca/tools/doca-kernel-support --kernel "$KVER" || {
    echo "!! doca-kernel-support failed — check tool path/flags for DOCA $MODVER" >&2
    exit 1
}

# collect built .kos + symvers (paths vary by DOCA version — validate)
find /var/lib/dkms -path "*/$KVER/*" -name '*.ko' -exec cp {} "$OUT/" \;
find /var/lib/dkms -path "*$KVER*" -name Module.symvers -exec cp {} "$OUT/Module.symvers" \; -quit || true
