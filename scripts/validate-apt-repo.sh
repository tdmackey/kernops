#!/usr/bin/env bash
# Validate a freshly assembled apt repository before it is synced live.
set -euo pipefail

REPO_DIR=$(cd "${1:?usage: validate-apt-repo.sh <repo-dir> [series]}" && pwd)
SERIES="${2:-noble}"
BUILDER_IMAGE="${BUILDER_IMAGE:-localhost/gb200-builder:$SERIES}"

[ -f "$REPO_DIR/conf/distributions" ] || {
    echo "!! no reprepro conf/distributions in $REPO_DIR" >&2
    exit 1
}
find "$REPO_DIR/provenance" -name '*.json' -print -quit 2>/dev/null | grep -q . || {
    echo "!! no provenance JSON copied into $REPO_DIR/provenance" >&2
    exit 1
}

podman run --rm -v "$REPO_DIR:/repo" "$BUILDER_IMAGE" bash -s <<'EOF'
set -euo pipefail
command -v reprepro >/dev/null || { apt-get -qq update && apt-get -qq install -y reprepro >/dev/null; }
cd /repo
reprepro check

mapfile -t suites < <(awk '/^Codename:/ {print $2}' conf/distributions)
[ "${#suites[@]}" -gt 0 ] || { echo "!! no suites in conf/distributions" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for suite in "${suites[@]}"; do
    echo "== repo smoke: $suite"
    reprepro list "$suite" | tee "$tmp/list-$suite.txt"
    [ -s "$tmp/list-$suite.txt" ] || { echo "!! suite $suite is empty" >&2; exit 1; }
    for arch in arm64 amd64; do
        pkgfile="dists/$suite/main/binary-$arch/Packages"
        [ -s "$pkgfile" ] || continue
        mkdir -p "$tmp/apt-$suite-$arch"/{etc/apt/sources.list.d,etc/apt/preferences.d,var/lib/apt/lists/partial,var/cache/apt/archives/partial,state}
        cat > "$tmp/apt-$suite-$arch/etc/apt/sources.list" <<SRC
deb [trusted=yes arch=$arch] file:/repo $suite main
SRC
        apt-get \
            -o Dir="$tmp/apt-$suite-$arch" \
            -o Dir::Etc::sourcelist="etc/apt/sources.list" \
            -o Dir::Etc::sourceparts="etc/apt/sources.list.d" \
            -o Dir::State::status="/dev/null" \
            -o APT::Architecture="$arch" \
            -o APT::Architectures::="$arch" \
            update >/dev/null
        awk '
            /^Package:/ {pkg=$2}
            /^Version:/ {ver=$2}
            /^Architecture:/ {
                if ($2 == arch && pkg ~ /^(linux-image|linux-headers|gb200-modules-)/) {
                    print pkg "=" ver
                }
            }
        ' arch="$arch" "$pkgfile" > "$tmp/packages-$suite-$arch.txt"
        [ -s "$tmp/packages-$suite-$arch.txt" ] || continue
        xargs -r apt-get \
            -o Dir="$tmp/apt-$suite-$arch" \
            -o Dir::Etc::sourcelist="etc/apt/sources.list" \
            -o Dir::Etc::sourceparts="etc/apt/sources.list.d" \
            -o Dir::State::status="/dev/null" \
            -o APT::Architecture="$arch" \
            -o APT::Architectures::="$arch" \
            --download-only --no-install-recommends install -y \
            < "$tmp/packages-$suite-$arch.txt" >/dev/null
        echo "   $arch apt download smoke ok"
    done
done
EOF

echo ">> apt repo validation ok: $REPO_DIR"
