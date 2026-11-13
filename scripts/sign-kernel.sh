#!/usr/bin/env bash
# Sign the kernel image of a build for Secure Boot, and make the signed image
# package.
#
# Usage: sign-kernel.sh <line> <series> <key> <cert.pem>
#   key   private key file, or a pkcs11: URI
#
# The build makes linux-image-unsigned-<release>. This script signs the image
# inside it with sbsign and writes linux-image-<release> next to it: the same
# package with the signed image, which replaces the unsigned one. That is the
# layout Ubuntu uses, so everything that depends on "linux-image-<release>"
# (the module packages, a metapackage) works with it.
#
# It runs in the series' builder container, which has no access to anything
# but the build directory and the key.
#
# Not done here: enrolling the certificate on a machine, and a shim. A machine
# trusts this image only after the certificate is in its Secure Boot database
# or enrolled as a Machine Owner Key.
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"

LINE="${1:?usage: sign-kernel.sh <line> <series> <key> <cert.pem>}"
SERIES="${2:?usage: sign-kernel.sh <line> <series> <key> <cert.pem>}"
KEY="${3:?key required}"
CERT="$(cd "$(dirname "${4:?cert required}")" && pwd)/$(basename "$4")"
D="$WORK_ROOT/build/$LINE/$SERIES"
BUILDER_IMAGE="${BUILDER_IMAGE:-localhost/kernel-builder:$SERIES}"
[ -e "$CERT" ] || { echo "!! cert not found: $CERT" >&2; exit 1; }
ls "$D"/linux-image-unsigned-*.deb >/dev/null 2>&1 || { echo "!! no linux-image-unsigned package in $D" >&2; exit 1; }

KEYMOUNT=() KEYARG="$KEY"
if [[ "$KEY" != pkcs11:* ]]; then
    [ -e "$KEY" ] || { echo "!! key not found: $KEY" >&2; exit 1; }
    KEYMOUNT=(-v "$(cd "$(dirname "$KEY")" && pwd)/$(basename "$KEY"):/sign/key:ro")
    KEYARG=/sign/key
fi

podman run --rm -v "$D:/build" -v "$CERT:/sign/cert.pem:ro" "${KEYMOUNT[@]}" -e KEYARG="$KEYARG" \
    "$BUILDER_IMAGE" bash -c '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    command -v sbsign >/dev/null || { apt-get -qq update >/dev/null; apt-get -qq install -y sbsigntool >/dev/null; }
    engine=(); [[ "$KEYARG" == pkcs11:* ]] && engine=(--engine pkcs11)
    for deb in /build/linux-image-unsigned-*.deb; do
        name="$(dpkg-deb -f "$deb" Package)"; release="${name#linux-image-unsigned-}"
        ver="$(dpkg-deb -f "$deb" Version)"; arch="$(dpkg-deb -f "$deb" Architecture)"
        work="$(mktemp -d)"
        dpkg-deb -R "$deb" "$work"
        img="$work/boot/vmlinuz-$release"
        [ -f "$img" ] || { echo "!! $deb has no boot/vmlinuz-$release" >&2; exit 1; }
        sbsign "${engine[@]}" --key "$KEYARG" --cert /sign/cert.pem --output "$img.signed" "$img" 2>/tmp/sbsign.log \
            || { cat /tmp/sbsign.log >&2; exit 1; }
        sbverify --cert /sign/cert.pem "$img.signed" >/dev/null
        mv "$img.signed" "$img"
        # The same package under the signed name. It takes the place of the unsigned one.
        # The unsigned package conflicts with the signed name; the signed one is the reverse.
        sed -i -e "s/^Package: .*/Package: linux-image-$release/" \
               -e "/^Conflicts:/d" -e "/^Replaces:/d" -e "/^Installed-Size:/d" "$work/DEBIAN/control"
        printf "Conflicts: linux-image-unsigned-%s\nReplaces: linux-image-unsigned-%s\n" "$release" "$release" >> "$work/DEBIAN/control"
        ( cd "$work" && find . -type f ! -path "./DEBIAN/*" -printf "%P\0" | sort -z | xargs -0 md5sum > DEBIAN/md5sums )
        out="/build/linux-image-${release}_${ver}_${arch}.deb"
        dpkg-deb --root-owner-group -Zzstd --build "$work" "$out" >/dev/null
        echo "signed: ${out##*/}"
        sbverify --list "$img" 2>/dev/null | sed -n "s/^ - subject: /  signer: /p" | head -n 1
        rm -rf "$work"
    done
'
