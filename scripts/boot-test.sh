#!/usr/bin/env bash
# QEMU boot smoke test for a built kernel deb — 4k flavours, on the macOS
# host with HVF acceleration (Apple Silicon cannot virtualize 64k-page
# guests; 64k flavours are boot-tested on Graviton CI / real hardware).
#
# Usage: boot-test.sh <linux-image-*.deb> [timeout-seconds]
#
# What it does:
#   1. extracts the PE vmlinuz from the deb (builder container does the
#      dpkg work; macOS has no dpkg)
#   2. builds a minimal busybox initramfs whose /init prints BOOT-SMOKE-OK
#      and powers off
#   3. boots it under qemu-system-aarch64 (-accel hvf) behind edk2 UEFI
#      firmware — the PE/zboot path, same binary format the PXE/UKI flow
#      will boot, so this also regression-tests the zboot packaging fix
#   4. pass = BOOT-SMOKE-OK seen before timeout
#
# Needs on the host: brew install qemu
set -euo pipefail

DEB=$(cd "$(dirname "${1:?usage: boot-test.sh <linux-image-*.deb> [timeout]}")" && pwd)/$(basename "$1")
TIMEOUT="${2:-120}"
WORK=$(mktemp -d /tmp/gb200-boot.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

QEMU=qemu-system-aarch64
EDK2=$(ls /opt/homebrew/share/qemu/edk2-aarch64-code.fd 2>/dev/null || true)
command -v $QEMU >/dev/null || { echo "!! $QEMU not found — brew install qemu" >&2; exit 1; }
[ -n "$EDK2" ] || { echo "!! edk2-aarch64-code.fd not found (comes with brew qemu)" >&2; exit 1; }

echo ">> extracting kernel + building initramfs (builder container)"
podman run --rm -v "$DEB:/in.deb:ro" -v "$WORK:/out" localhost/gb200-builder:noble bash -c '
    set -euo pipefail
    apt-get -qq update >/dev/null 2>&1 || true
    dpkg -s busybox-static >/dev/null 2>&1 || apt-get -qq install -y busybox-static >/dev/null
    dpkg-deb --fsys-tarfile /in.deb | tar -x -C /tmp ./boot
    cp /tmp/boot/vmlinuz-* /out/vmlinuz
    mkdir -p /tmp/ird/{bin,proc,sys,dev}
    cp /bin/busybox /tmp/ird/bin/busybox
    cat > /tmp/ird/init <<EOF
#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox uname -a
echo BOOT-SMOKE-OK
/bin/busybox poweroff -f
EOF
    chmod +x /tmp/ird/init
    (cd /tmp/ird && find . | cpio -o -H newc --quiet | gzip) > /out/initrd.gz
    file /out/vmlinuz
'

echo ">> booting under qemu/hvf (timeout ${TIMEOUT}s)"
set +e
LOG="$WORK/console.log"
$QEMU -M virt -accel hvf -cpu host -smp 2 -m 2048 \
      -bios "$EDK2" \
      -kernel "$WORK/vmlinuz" -initrd "$WORK/initrd.gz" \
      -append "console=ttyAMA0 panic=-1" \
      -nographic -no-reboot >"$LOG" 2>&1 &
QPID=$!
SECS=0
while kill -0 $QPID 2>/dev/null && [ $SECS -lt "$TIMEOUT" ]; do
    sleep 2; SECS=$((SECS+2))
    grep -q BOOT-SMOKE-OK "$LOG" && break
done
kill $QPID 2>/dev/null
set -e

if grep -q BOOT-SMOKE-OK "$LOG"; then
    echo ">> PASS: $(grep -m1 'Linux version' "$LOG" | cut -c1-100)"
    exit 0
fi
echo ">> FAIL — last console output:"
tail -25 "$LOG"
exit 1
