#!/usr/bin/env bash
# Build the out-of-tree module matrix for one base against freshly built
# kernel debs. See docs/modules.md for the contract.
#
# Usage: build-modules.sh <base> <deb-dir> [series] [arch]
#   deb-dir: where the base's linux-headers-*.deb live (build output dir)
#
# Produces gb200-modules-<name>-<kver>_<modver>+kernel.<kernel-version>_<arch>.deb
# in <deb-dir>/modules/.
# Signing: if MODULE_SIGN_KEY (+_CERT) is set, every .ko is signed; local
# dev builds run unsigned (enforcement is phased — see docs/modules.md).
set -euo pipefail

MONO="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:?usage: build-modules.sh <base> <deb-dir> [series] [arch]}"
DEBDIR=$(cd "${2:?usage: build-modules.sh <base> <deb-dir> [series] [arch]}" && pwd)
SERIES="${3:-noble}"
# shellcheck source=scripts/lib/arch.sh
. "$MONO/scripts/lib/arch.sh"
ARCH="$(gb200_arch_normalize "${4:-${ARCH:-arm64}}")"
BUILDER_IMAGE="${BUILDER_IMAGE:-localhost/gb200-builder:$SERIES}"
OUT="$DEBDIR/modules"
mkdir -p "$OUT"

# KVER must be unambiguous: a dir holding two kernels' headers would let us
# silently build modules against the wrong one (worst failure mode: boots
# elsewhere). Derive it, and refuse to guess.
KVERS=$(find "$DEBDIR" -maxdepth 1 -name "linux-headers-*_${ARCH}.deb" -print \
        | sed -E 's/.*linux-headers-([^_]+)_.*/\1/' | sort -u)
if [ -z "${KVER:-}" ]; then
    case $(echo "$KVERS" | grep -c .) in
        0) echo "!! no linux-headers deb in $DEBDIR" >&2; exit 1 ;;
        1) KVER="$KVERS" ;;
        *) echo "!! multiple kernels in $DEBDIR — set KVER explicitly:" >&2
           echo "$KVERS" >&2; exit 1 ;;
    esac
fi
echo ">> base=$BASE arch=$ARCH kver=$KVER"

IMAGE_DEBS=()
while IFS= read -r deb; do IMAGE_DEBS+=("$deb"); done < <(
    find "$DEBDIR" -maxdepth 1 -name "linux-image-${KVER}_*_${ARCH}.deb" -print | sort
)
case ${#IMAGE_DEBS[@]} in
    0) echo "!! no linux-image-$KVER deb in $DEBDIR" >&2; exit 1 ;;
    1) ;;
    *) echo "!! multiple linux-image-$KVER debs in $DEBDIR" >&2
       printf '%s\n' "${IMAGE_DEBS[@]}" >&2
       exit 1 ;;
esac
IMAGE_DEB=$(basename "${IMAGE_DEBS[0]}")
KERNEL_PKGVER=${IMAGE_DEB#linux-image-${KVER}_}
KERNEL_PKGVER=${KERNEL_PKGVER%_${ARCH}.deb}
echo ">> kernel package version=$KERNEL_PKGVER"

mapfile -t MATRIX_ROWS < <(
    awk -F'\t' -v b="$BASE" -v a="$ARCH" '
        !/^#/ && NF>=5 && $1==b && ($2==a || $2=="all") {print $3"\t"$4"\t"$5}
        !/^#/ && NF==4 && $1==b && a=="arm64" {print $2"\t"$3"\t"$4}
    ' "$MONO/modules/matrix.tsv"
)
if [ "${#MATRIX_ROWS[@]}" -eq 0 ]; then
    if [ "${ALLOW_EMPTY_MODULE_MATRIX:-0}" = 1 ]; then
        echo ">> no module rows for base=$BASE arch=$ARCH (allowed)"
        exit 0
    fi
    echo "!! no module rows for base=$BASE arch=$ARCH in modules/matrix.tsv" >&2
    exit 1
fi

SYMVERS_EXTRA=""
for row in "${MATRIX_ROWS[@]}"; do
    IFS=$'\t' read -r name modver src <<< "$row"
    echo "== module $name $modver"
    [ -x "$MONO/modules/$name/build.sh" ] || { echo "!! no modules/$name/build.sh" >&2; exit 1; }

    KO_OUT="$OUT/$name-ko"
    rm -rf "$KO_OUT"; mkdir -p "$KO_OUT"
    REQUIRE_OFED_SYMVERS=0
    [ "$name" = "nvidia-open" ] && [ -n "$SYMVERS_EXTRA" ] && REQUIRE_OFED_SYMVERS=1

    podman run --rm \
        -v "$MONO:$MONO:ro" -v "$DEBDIR:$DEBDIR" -v "$KO_OUT:/ko-out" \
        -e KVER="$KVER" -e HEADERS_DIR="$DEBDIR" -e MODVER="$modver" \
        -e SRC="$src" -e SYMVERS_EXTRA="$SYMVERS_EXTRA" \
        -e REQUIRE_OFED_SYMVERS="$REQUIRE_OFED_SYMVERS" -e OUT=/ko-out \
        "$BUILDER_IMAGE" \
        bash "$MONO/modules/$name/build.sh"
    if [ "$name" = "doca" ] && [ ! -f "$KO_OUT/Module.symvers" ]; then
        echo "!! doca did not produce Module.symvers; nvidia_peermem would link against the wrong RDMA symbols" >&2
        exit 1
    fi

    # sign every .ko if a key is configured (CI: KMS/PKCS#11; local: skip)
    if [ -n "${MODULE_SIGN_KEY:-}" ]; then
        "$MONO/scripts/sign-module.sh" "$MODULE_SIGN_KEY" "${MODULE_SIGN_CERT:?}" \
            "$KO_OUT" "$DEBDIR" "$SERIES" "$ARCH"
    else
        echo "   (unsigned — local dev build)"
    fi

    podman run --rm \
        -v "$MONO:$MONO:ro" -v "$KO_OUT:/ko-out:ro" -v "$OUT:/out" \
        "$BUILDER_IMAGE" \
        python3 "$MONO/scripts/module-abi-metadata.py" \
            --name "$name" --version "$modver" --kver "$KVER" --arch "$ARCH" \
            --kernel-package-version "$KERNEL_PKGVER" \
            --ko-dir /ko-out --output "/out/$name-abi.json"

    # package: our mkbmdeb replacement
    # (stage under $OUT — host mktemp dirs live outside the podman VM mounts)
    PKG="gb200-modules-$name-$KVER"
    PKGVER="$modver+kernel.$KERNEL_PKGVER"
    STAGE=$(mktemp -d "$OUT/.stage.XXXXXX")
    mkdir -p "$STAGE/lib/modules/$KVER/updates/$name" "$STAGE/DEBIAN"
    find "$KO_OUT" -name '*.ko' -exec cp {} "$STAGE/lib/modules/$KVER/updates/$name/" \;
    NKO=$(find "$STAGE" -name '*.ko' | wc -l | tr -d ' ')
    [ "$NKO" -gt 0 ] || { echo "!! $name produced no .ko files" >&2; exit 1; }
    cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $PKGVER
Architecture: $ARCH
Maintainer: gb200 kernel pipeline
Depends: linux-image-$KVER (= $KERNEL_PKGVER)
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
    podman run --rm -v "$STAGE:/stage" -v "$OUT:/out" "$BUILDER_IMAGE" \
        dpkg-deb --build --root-owner-group /stage "/out/${PKG}_${PKGVER}_${ARCH}.deb"
    rm -rf "$STAGE"

    # export symvers for later rows (nvidia_peermem needs OFED's)
    if [ -f "$KO_OUT/Module.symvers" ]; then
        SYMVERS_EXTRA="$KO_OUT/Module.symvers"
    fi
    echo "   -> ${PKG}_${PKGVER}_${ARCH}.deb ($NKO .ko)"
done

ls -lh "$OUT"/*.deb 2>/dev/null || true
