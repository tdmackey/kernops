#!/usr/bin/env bash
# Build NVIDIA open GPU kernel modules against $KVER headers.
# Contract: see docs/modules.md (runs inside gb200-builder container).
set -euo pipefail
: "${KVER:?}" "${MODVER:?}" "${SRC:?}" "${OUT:?}" "${HEADERS_DEB:?}"

dpkg -i "$HEADERS_DEB" >/dev/null 2>&1 || apt-get -qq -f install -y >/dev/null

WORK=/tmp/nvidia-open
rm -rf "$WORK"
# MODVER like "580-open" -> branch/tag in the open-gpu-kernel-modules repo;
# pin an exact tag (e.g. 580.65.06) in matrix.tsv once selected.
git clone --depth 1 --branch "$MODVER" "$SRC" "$WORK"
cd "$WORK"

make modules -j"$(nproc)" KERNEL_UNAME="$KVER"

# nvidia_peermem against OFED symbols when DOCA row built first
if [ -n "${SYMVERS_EXTRA:-}" ] && [ -f "$SYMVERS_EXTRA" ]; then
    echo ">> rebuilding nvidia_peermem with OFED Module.symvers"
    make modules -j"$(nproc)" KERNEL_UNAME="$KVER" \
        KBUILD_EXTRA_SYMBOLS="$SYMVERS_EXTRA" || true
fi

find kernel-open -name '*.ko' -exec cp {} "$OUT/" \;
cp kernel-open/Module.symvers "$OUT/" 2>/dev/null || true
