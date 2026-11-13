#!/usr/bin/env bash
# Build NVIDIA open GPU kernel modules against $KVER headers.
# Contract: see docs/modules.md (runs inside kernel-builder container).
set -euo pipefail
: "${KVER:?}" "${MODVER:?}" "${SRC:?}" "${OUT:?}" "${HEADERS_DIR:?}"

# The flavour's headers and the common headers they depend on. For a Canonical
# kernel apt could fetch the common ones; for our own they exist only here.
dpkg -i "$HEADERS_DIR"/linux-headers-"$KVER"_*.deb "$HEADERS_DIR"/linux-*-headers-*_all.deb >/dev/null 2>&1 || true
apt-get -qq update >/dev/null; apt-get -qq -f install -y >/dev/null
[ -d "/lib/modules/$KVER/build" ] || { echo "!! kernel headers for $KVER did not install from $HEADERS_DIR" >&2; exit 1; }

WORK=/tmp/nvidia-open
rm -rf "$WORK"
# MODVER = exact release tag, e.g. 620.70.06
git clone --depth 1 --branch "$MODVER" "$SRC" "$WORK"
cd "$WORK"

make modules -j"$(nproc)" KERNEL_UNAME="$KVER"

# nvidia_peermem against OFED symbols when DOCA row built first
if [ -n "${SYMVERS_EXTRA:-}" ] || [ "${REQUIRE_OFED_SYMVERS:-0}" = 1 ]; then
    [ -f "${SYMVERS_EXTRA:-}" ] || {
        echo "!! required OFED Module.symvers is missing; refusing to build nvidia_peermem against in-tree RDMA" >&2
        exit 1
    }
    echo ">> rebuilding nvidia_peermem with OFED Module.symvers"
    make modules -j"$(nproc)" KERNEL_UNAME="$KVER" \
        KBUILD_EXTRA_SYMBOLS="$SYMVERS_EXTRA"
fi

find kernel-open -name '*.ko' -exec cp {} "$OUT/" \;
cp kernel-open/Module.symvers "$OUT/" 2>/dev/null || true
