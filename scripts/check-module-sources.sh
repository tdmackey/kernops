#!/usr/bin/env bash
# Fast network preflight for module pins before starting expensive kernel builds.
set -euo pipefail

MONO="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:?usage: check-module-sources.sh <base> [arch] [series]}"
# shellcheck source=scripts/lib/arch.sh
. "$MONO/scripts/lib/arch.sh"
ARCH="$(gb200_arch_normalize "${2:-${ARCH:-arm64}}")"
SERIES="${3:-noble}"
BUILDER_IMAGE="${BUILDER_IMAGE:-localhost/gb200-builder:$SERIES}"
DOCA_DKMS_PKGS="${DOCA_DKMS_PKGS:-mlnx-ofed-kernel-dkms}"

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

for row in "${MATRIX_ROWS[@]}"; do
    IFS=$'\t' read -r name modver src <<< "$row"
    echo "== preflight $BASE/$ARCH $name $modver"
    case "$name" in
        doca)
            podman run --rm \
                -e SRC="$src" -e MODVER="$modver" -e DOCA_DKMS_PKGS="$DOCA_DKMS_PKGS" \
                "$BUILDER_IMAGE" bash -c '
                set -euo pipefail
                echo "deb [trusted=yes] $SRC ./" > /etc/apt/sources.list.d/doca-preflight.list
                apt-get -qq update >/dev/null
                for pkg in $DOCA_DKMS_PKGS; do
                    ver=$(apt-cache madison "$pkg" | awk -v want="$MODVER" "
                        \$3 == want || index(\$3, want \"-\") == 1 ||
                        index(\$3, want \"+\") == 1 || index(\$3, want \"~\") == 1 { print \$3; exit }
                    ")
                    if [ -z "$ver" ]; then
                        echo "!! no $pkg version matching DOCA pin $MODVER in $SRC" >&2
                        apt-cache policy "$pkg" >&2 || true
                        exit 1
                    fi
                    echo "   $pkg=$ver"
                done
            '
            ;;
        nvidia-open)
            git ls-remote --exit-code --tags "$src" "refs/tags/$modver" >/dev/null || {
                echo "!! no nvidia-open tag $modver in $src" >&2
                exit 1
            }
            echo "   tag exists"
            ;;
        *)
            if [ -e "$src" ] || [ -e "$MONO/$src" ]; then
                echo "   local source exists"
            else
                echo "!! no preflight handler for module '$name' source '$src'" >&2
                exit 1
            fi
            ;;
    esac
done

echo ">> module source preflight ok: base=$BASE arch=$ARCH"
