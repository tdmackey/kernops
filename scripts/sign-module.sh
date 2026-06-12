#!/usr/bin/env bash
# Sign kernel modules (sha512) with the module signing key — runs the
# kernel's sign-file INSIDE the builder container (the binary is Linux-only
# and ships in the linux-headers package).
#
# Usage: sign-module.sh <key> <cert> <ko-dir> <headers-dir> [series]
#   key         private key path or pkcs11: URI (KMS in CI)
#   cert        x509 cert (DER/PEM) matching CONFIG_SYSTEM_TRUSTED_KEYS
#   ko-dir      directory of .ko files to sign in place
#   headers-dir directory containing the linux-headers-*.deb of the TARGET
#               kernel (its sign-file matches the kernel's signature format)
#
# Fails loudly if anything is missing — an unsigned module reaching a
# sig_enforce kernel is a boot failure, not a warning.
set -euo pipefail

KEY="${1:?usage: sign-module.sh <key> <cert> <ko-dir> <headers-dir> [series]}"
CERT="${2:?cert required}"
KODIR=$(cd "${3:?ko-dir required}" && pwd)
HDRDIR=$(cd "${4:?headers-dir required}" && pwd)
SERIES="${5:-noble}"

[ -e "$KEY" ] || [[ "$KEY" == pkcs11:* ]] || { echo "!! key not found: $KEY" >&2; exit 1; }
[ -e "$CERT" ] || { echo "!! cert not found: $CERT" >&2; exit 1; }
ls "$HDRDIR"/linux-headers-*_arm64.deb >/dev/null 2>&1 || { echo "!! no headers deb in $HDRDIR" >&2; exit 1; }
ls "$KODIR"/*.ko >/dev/null 2>&1 || { echo "!! no .ko files in $KODIR" >&2; exit 1; }

KEYMOUNT=""
[[ "$KEY" == pkcs11:* ]] || KEYMOUNT="-v $(cd "$(dirname "$KEY")" && pwd)/$(basename "$KEY"):/sign/key:ro"

podman run --rm \
    -v "$KODIR:/sign/ko" -v "$HDRDIR:/sign/hdr:ro" \
    -v "$(cd "$(dirname "$CERT")" && pwd)/$(basename "$CERT"):/sign/cert:ro" \
    $KEYMOUNT \
    "localhost/gb200-builder:$SERIES" bash -c '
    set -euo pipefail
    dpkg -i /sign/hdr/linux-headers-*_arm64.deb >/dev/null 2>&1 || true
    apt-get -qq -f install -y >/dev/null 2>&1 || true
    SF=$(ls /usr/src/linux-headers-*/scripts/sign-file | head -1)
    [ -x "$SF" ] || { echo "!! sign-file not found in headers" >&2; exit 1; }
    KEY=/sign/key; [ -e "$KEY" ] || KEY="'"$KEY"'"   # pkcs11 URI passes through
    n=0
    for ko in /sign/ko/*.ko; do
        "$SF" sha512 "$KEY" /sign/cert "$ko"
        n=$((n+1))
    done
    echo ">> signed $n module(s)"
    # verify: every module now carries a signature marker
    for ko in /sign/ko/*.ko; do
        tail -c 28 "$ko" | grep -q "Module signature appended" || {
            echo "!! $ko missing signature marker" >&2; exit 1; }
    done
'
