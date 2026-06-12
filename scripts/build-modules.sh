#!/usr/bin/env bash
# Build the out-of-tree module matrix for one base against freshly built
# kernel debs. See docs/modules.md for the contract.
#
# Usage: build-modules.sh <base> <deb-dir> [series]
#   deb-dir: where the base's linux-headers-*.deb live (build output dir)
#
# Produces gb200-modules-<name>-<kver>_<modver>_arm64.deb in <deb-dir>/modules/.
# Signing: if MODULE_SIGN_KEY (+_CERT) is set, every .ko is signed; local
# dev builds run unsigned (enforcement is phased — see docs/modules.md).
set -euo pipefail

MONO="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:?usage: build-modules.sh <base> <deb-dir> [series]}"
DEBDIR=$(cd "${2:?usage: build-modules.sh <base> <deb-dir> [series]}" && pwd)
SERIES="${3:-noble}"
OUT="$DEBDIR/modules"
mkdir -p "$OUT"

HEADERS=$(ls "$DEBDIR"/linux-headers-*_arm64.deb 2>/dev/null | head -1)
[ -n "$HEADERS" ] || { echo "!! no linux-headers deb in $DEBDIR" >&2; exit 1; }
KVER=$(basename "$HEADERS" | sed -E 's/^linux-headers-([^_]+)_.*/\1/')
echo ">> base=$BASE kver=$KVER"

SYMVERS_EXTRA=""
awk -F'\t' -v b="$BASE" '!/^#/ && $1==b {print $2"\t"$3"\t"$4}' "$MONO/modules/matrix.tsv" |
while IFS=$'\t' read -r name modver src; do
    echo "== module $name $modver"
    [ -x "$MONO/modules/$name/build.sh" ] || { echo "!! no modules/$name/build.sh" >&2; exit 1; }

    KO_OUT="$OUT/$name-ko"
    rm -rf "$KO_OUT"; mkdir -p "$KO_OUT"

    podman run --rm \
        -v "$MONO:$MONO:ro" -v "$DEBDIR:$DEBDIR" -v "$KO_OUT:/ko-out" \
        -e KVER="$KVER" -e HEADERS_DIR="$DEBDIR" -e MODVER="$modver" \
        -e SRC="$src" -e SYMVERS_EXTRA="$SYMVERS_EXTRA" -e OUT=/ko-out \
        "localhost/gb200-builder:$SERIES" \
        bash "$MONO/modules/$name/build.sh"

    # sign every .ko if a key is configured (CI: KMS/PKCS#11; local: skip)
    if [ -n "${MODULE_SIGN_KEY:-}" ]; then
        find "$KO_OUT" -name '*.ko' -exec \
            "$MONO/scripts/sign-module.sh" "$MODULE_SIGN_KEY" "${MODULE_SIGN_CERT:?}" {} \;
    else
        echo "   (unsigned — local dev build)"
    fi

    # package: our mkbmdeb replacement
    PKG="gb200-modules-$name-$KVER"
    STAGE=$(mktemp -d)
    mkdir -p "$STAGE/lib/modules/$KVER/updates/$name" "$STAGE/DEBIAN"
    find "$KO_OUT" -name '*.ko' -exec cp {} "$STAGE/lib/modules/$KVER/updates/$name/" \;
    NKO=$(find "$STAGE" -name '*.ko' | wc -l | tr -d ' ')
    [ "$NKO" -gt 0 ] || { echo "!! $name produced no .ko files" >&2; exit 1; }
    cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $modver
Architecture: arm64
Maintainer: gb200 kernel pipeline
Depends: linux-image-$KVER
Section: kernel
Priority: optional
Description: prebuilt $name modules ($modver) for $KVER
EOF
    cat > "$STAGE/DEBIAN/postinst" <<EOF
#!/bin/sh
set -e
depmod -a "$KVER" || true
EOF
    chmod 755 "$STAGE/DEBIAN/postinst"
    podman run --rm -v "$STAGE:/stage" -v "$OUT:/out" "localhost/gb200-builder:$SERIES" \
        dpkg-deb --build --root-owner-group /stage "/out/${PKG}_${modver}_arm64.deb"
    rm -rf "$STAGE"

    # export symvers for later rows (nvidia_peermem needs OFED's)
    if [ -f "$KO_OUT/Module.symvers" ]; then
        SYMVERS_EXTRA="$KO_OUT/Module.symvers"
    fi
    echo "   -> ${PKG}_${modver}_arm64.deb ($NKO .ko)"
done

ls -lh "$OUT"/*.deb 2>/dev/null || true
