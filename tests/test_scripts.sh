#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/gb200-script-tests.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

fail() {
    echo "!! $*" >&2
    exit 1
}

make_deb() {
    mkdir -p "$(dirname "$1")"
    : > "$1"
}

test_build_modules_rejects_ambiguous_headers() {
    local debdir="$TMP/ambiguous"
    make_deb "$debdir/linux-headers-6.8.0-124-generic_6.8.0-124.124_arm64.deb"
    make_deb "$debdir/linux-headers-6.8.0-125-generic_6.8.0-125.125_arm64.deb"
    make_deb "$debdir/linux-image-6.8.0-124-generic_6.8.0-124.124_arm64.deb"
    if "$ROOT/scripts/build-modules.sh" __fixture__ "$debdir" noble >"$TMP/ambiguous.log" 2>&1; then
        fail "build-modules accepted ambiguous headers"
    fi
    grep -q "multiple kernels" "$TMP/ambiguous.log" || fail "missing ambiguity error"
}

test_build_modules_requires_matching_image_deb() {
    local debdir="$TMP/no-image"
    make_deb "$debdir/linux-headers-6.8.0-124-generic_6.8.0-124.124_arm64.deb"
    if "$ROOT/scripts/build-modules.sh" __fixture__ "$debdir" noble >"$TMP/no-image.log" 2>&1; then
        fail "build-modules accepted headers without matching image"
    fi
    grep -q "no linux-image-6.8.0-124-generic deb" "$TMP/no-image.log" ||
        fail "missing image error"
}

test_build_modules_rejects_missing_headers() {
    local debdir="$TMP/no-headers"
    mkdir -p "$debdir"
    if "$ROOT/scripts/build-modules.sh" __fixture__ "$debdir" noble >"$TMP/no-headers.log" 2>&1; then
        fail "build-modules accepted an empty deb directory"
    fi
    grep -q "no linux-headers deb" "$TMP/no-headers.log" ||
        fail "missing headers error"
}

test_build_modules_extracts_kernel_package_version() {
    local debdir="$TMP/one-kernel"
    make_deb "$debdir/linux-headers-6.8.0-124-generic_6.8.0-124.124+gb200.1_arm64.deb"
    make_deb "$debdir/linux-image-6.8.0-124-generic_6.8.0-124.124+gb200.1_arm64.deb"
    ALLOW_EMPTY_MODULE_MATRIX=1 \
        "$ROOT/scripts/build-modules.sh" __fixture__ "$debdir" noble >"$TMP/one-kernel.log"
    grep -q "kernel package version=6.8.0-124.124+gb200.1" "$TMP/one-kernel.log" ||
        fail "kernel package version not parsed"
}

test_build_modules_accepts_x86_64_alias() {
    local debdir="$TMP/one-kernel-amd64"
    make_deb "$debdir/linux-headers-6.8.0-124-generic_6.8.0-124.124+gb200.1_amd64.deb"
    make_deb "$debdir/linux-image-6.8.0-124-generic_6.8.0-124.124+gb200.1_amd64.deb"
    ALLOW_EMPTY_MODULE_MATRIX=1 \
        "$ROOT/scripts/build-modules.sh" __fixture__ "$debdir" noble x86_64 >"$TMP/one-kernel-amd64.log"
    grep -q "arch=amd64" "$TMP/one-kernel-amd64.log" ||
        fail "x86_64 alias was not normalized to amd64"
    grep -q "kernel package version=6.8.0-124.124+gb200.1" "$TMP/one-kernel-amd64.log" ||
        fail "amd64 kernel package version not parsed"
}

test_build_modules_rejects_missing_matrix_rows() {
    local debdir="$TMP/no-matrix"
    make_deb "$debdir/linux-headers-6.8.0-124-generic_6.8.0-124.124_arm64.deb"
    make_deb "$debdir/linux-image-6.8.0-124-generic_6.8.0-124.124_arm64.deb"
    if "$ROOT/scripts/build-modules.sh" missing-base "$debdir" noble >"$TMP/no-matrix.log" 2>&1; then
        fail "build-modules accepted a target without module rows"
    fi
    grep -q "no module rows for base=missing-base arch=arm64" "$TMP/no-matrix.log" ||
        fail "missing matrix row error"
}

test_check_module_sources_rejects_missing_matrix_rows() {
    if "$ROOT/scripts/check-module-sources.sh" missing-base arm64 noble >"$TMP/source-preflight.log" 2>&1; then
        fail "module source preflight accepted a target without module rows"
    fi
    grep -q "no module rows for base=missing-base arch=arm64" "$TMP/source-preflight.log" ||
        fail "missing source preflight matrix row error"

    ALLOW_EMPTY_MODULE_MATRIX=1 \
        "$ROOT/scripts/check-module-sources.sh" missing-base arm64 noble >"$TMP/source-preflight-allowed.log"
    grep -q "allowed" "$TMP/source-preflight-allowed.log" ||
        fail "source preflight allow-empty mode did not report allowed"
}

test_publish_repo_validates_manifest_before_podman() {
    local debdir="$TMP/publish"
    local repo="$TMP/repo"
    mkdir -p "$debdir" "$repo"
    printf './missing.deb\n' > "$debdir/.publish-manifest"
    if REPO_DIR="$repo" "$ROOT/scripts/publish-repo.sh" noble-6.8 "$debdir" noble >"$TMP/publish.log" 2>&1; then
        fail "publish-repo accepted missing manifest entry"
    fi
    grep -q "manifest entry missing" "$TMP/publish.log" ||
        fail "missing manifest validation error"
}

test_publish_repo_invokes_podman_for_valid_manifest() {
    local debdir="$TMP/publish-ok"
    local repo="$TMP/repo-ok"
    local fakebin="$TMP/bin"
    mkdir -p "$debdir/modules" "$repo" "$fakebin"
    make_deb "$debdir/linux-image-6.8.0-124-generic_6.8.0-124.124_arm64.deb"
    make_deb "$debdir/modules/gb200-modules-doca-6.8.0-124-generic_3.4.0+kernel.6.8.0-124.124_arm64.deb"
    printf './linux-image-6.8.0-124-generic_6.8.0-124.124_arm64.deb\n./modules/gb200-modules-doca-6.8.0-124-generic_3.4.0+kernel.6.8.0-124.124_arm64.deb\n' > "$debdir/.publish-manifest"
    cat > "$fakebin/podman" <<'EOF'
#!/usr/bin/env bash
echo "$*" > "${GB200_PODMAN_LOG:?}"
exit 0
EOF
    chmod +x "$fakebin/podman"
    GB200_PODMAN_LOG="$TMP/podman.log" PATH="$fakebin:$PATH" REPO_DIR="$repo" \
        "$ROOT/scripts/publish-repo.sh" noble-6.8 "$debdir" noble >/dev/null
    grep -q "gb200-builder:noble" "$TMP/podman.log" || fail "podman was not invoked"
}

test_publish_repo_copies_provenance_after_success() {
    local debdir="$TMP/publish-provenance/run-1"
    local repo="$TMP/repo-provenance"
    local fakebin="$TMP/bin-provenance"
    mkdir -p "$debdir" "$repo" "$fakebin"
    make_deb "$debdir/linux-image-6.8.0-124-generic_6.8.0-124.124_arm64.deb"
    printf './linux-image-6.8.0-124-generic_6.8.0-124.124_arm64.deb\n' > "$debdir/.publish-manifest"
    printf '{"schema":"gb200.provenance.v1"}\n' > "$debdir/gb200-provenance.json"
    cat > "$fakebin/podman" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$fakebin/podman"
    PATH="$fakebin:$PATH" REPO_DIR="$repo" \
        "$ROOT/scripts/publish-repo.sh" noble-6.8 "$debdir" noble >/dev/null
    [ -f "$repo/provenance/noble-6.8/arm64/run-1.json" ] ||
        fail "provenance was not copied after publish success"
}

test_publish_repo_does_not_copy_provenance_after_failure() {
    local debdir="$TMP/publish-provenance-fail/run-1"
    local repo="$TMP/repo-provenance-fail"
    local fakebin="$TMP/bin-provenance-fail"
    mkdir -p "$debdir" "$repo" "$fakebin"
    make_deb "$debdir/linux-image-6.8.0-124-generic_6.8.0-124.124_arm64.deb"
    printf './linux-image-6.8.0-124-generic_6.8.0-124.124_arm64.deb\n' > "$debdir/.publish-manifest"
    printf '{"schema":"gb200.provenance.v1"}\n' > "$debdir/gb200-provenance.json"
    cat > "$fakebin/podman" <<'EOF'
#!/usr/bin/env bash
exit 42
EOF
    chmod +x "$fakebin/podman"
    if PATH="$fakebin:$PATH" REPO_DIR="$repo" \
        "$ROOT/scripts/publish-repo.sh" noble-6.8 "$debdir" noble >/dev/null 2>&1; then
        fail "publish-repo accepted failing podman publish"
    fi
    [ ! -e "$repo/provenance/noble-6.8/arm64/run-1.json" ] ||
        fail "provenance was copied before publish success"
}

test_publish_repo_copies_amd64_provenance_path() {
    local debdir="$TMP/publish-provenance-amd64/run-1"
    local repo="$TMP/repo-provenance-amd64"
    local fakebin="$TMP/bin-provenance-amd64"
    mkdir -p "$debdir" "$repo" "$fakebin"
    make_deb "$debdir/linux-image-6.8.0-124-generic_6.8.0-124.124_amd64.deb"
    printf './linux-image-6.8.0-124-generic_6.8.0-124.124_amd64.deb\n' > "$debdir/.publish-manifest"
    printf '{"schema":"gb200.provenance.v1","arch":"amd64"}\n' > "$debdir/gb200-provenance.json"
    cat > "$fakebin/podman" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$fakebin/podman"
    PATH="$fakebin:$PATH" REPO_DIR="$repo" \
        "$ROOT/scripts/publish-repo.sh" noble-6.8 "$debdir" noble amd64 >/dev/null
    [ -f "$repo/provenance/noble-6.8/amd64/run-1.json" ] ||
        fail "amd64 provenance was not copied after publish success"
    grep -q "Architectures: arm64 amd64" "$repo/conf/distributions" ||
        fail "repo distributions did not include amd64"
}

test_validate_apt_repo_invokes_podman_after_host_checks() {
    local repo="$TMP/validate-repo"
    local fakebin="$TMP/bin-validate"
    mkdir -p "$repo/conf" "$repo/provenance/noble-6.8/arm64" "$fakebin"
    cat > "$repo/conf/distributions" <<'EOF'
Codename: noble-6.8
Suite: noble-6.8
Components: main
Architectures: arm64 amd64
EOF
    printf '{"schema":"gb200.provenance.v1"}\n' > "$repo/provenance/noble-6.8/arm64/run-1.json"
    cat > "$fakebin/podman" <<'EOF'
#!/usr/bin/env bash
echo "$*" > "${GB200_PODMAN_LOG:?}"
exit 0
EOF
    chmod +x "$fakebin/podman"
    GB200_PODMAN_LOG="$TMP/validate-podman.log" PATH="$fakebin:$PATH" \
        "$ROOT/scripts/validate-apt-repo.sh" "$repo" noble >/dev/null
    grep -q "gb200-builder:noble" "$TMP/validate-podman.log" ||
        fail "validate-apt-repo did not invoke the builder"
}

test_module_abi_metadata_writes_json_for_ko_files() {
    local kodir="$TMP/ko"
    local out="$TMP/abi/doca-abi.json"
    mkdir -p "$kodir" "$(dirname "$out")"
    printf 'not a real module\nModule signature appended' > "$kodir/doca.ko"
    python3 "$ROOT/scripts/module-abi-metadata.py" \
        --name doca --version 3.4.0 --kver 6.8.0-124-generic --arch arm64 \
        --kernel-package-version 6.8.0-124.124 --ko-dir "$kodir" --output "$out" \
        >/dev/null
    python3 - "$out" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data["schema"] == "gb200.module_abi.v1"
assert data["name"] == "doca"
assert data["modules"][0]["file"] == "doca.ko"
assert data["modules"][0]["sha256"]
assert data["modules"][0]["signature_appended"] is True
PY
}

test_write_provenance_records_release_inputs() {
    local mono="$TMP/mono"
    local kernel="$TMP/kernel"
    local out="$TMP/provenance-out"
    mkdir -p "$mono/kernel/patches/gb200/fixture" "$mono/modules" "$kernel" "$out"

    (
        cd "$mono"
        git init -q
        git config user.email test@example.invalid
        git config user.name test
        printf 'fixture\tUbuntu-test-1.0.0-1.1\n' > kernel/upstream-base.txt
        printf 'patch body\n' > kernel/patches/gb200/fixture/0001-test.patch
        printf 'fixture\tarm64\tdoca\t3.4.0\trepo\nfixture\tamd64\tdoca\t3.4.0\trepo-amd64\n' > modules/matrix.tsv
        git add .
        git commit -q -m init
    )
    (
        cd "$kernel"
        git init -q
        git config user.email test@example.invalid
        git config user.name test
        printf 'kernel\n' > README
        git add README
        git commit -q -m kernel
        git tag Ubuntu-test-1.0.0-1.1
    )
    make_deb "$out/linux-image-test_1.0_arm64.deb"
    mkdir -p "$out/modules"
    printf './linux-image-test_1.0_arm64.deb\n' > "$out/.publish-manifest"
    cat > "$out/modules/doca-abi.json" <<'EOF'
{"schema":"gb200.module_abi.v1","name":"doca","modules":[{"file":"doca.ko","modinfo":{"vermagic":"test"}}]}
EOF

    python3 "$ROOT/scripts/write-provenance.py" \
        --repo-root "$mono" --kernel-repo "$kernel" --tree "$kernel" \
        --out-dir "$out" --base fixture --arch arm64 --flavour generic --series noble \
        --run-id run-1 >"$TMP/provenance.log"

    python3 - "$out/gb200-provenance.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data["schema"] == "gb200.provenance.v1"
assert data["arch"] == "arm64"
assert data["upstream_tag"] == "Ubuntu-test-1.0.0-1.1"
assert data["patches"] and data["patches"][0]["sha256"]
assert data["modules"][0]["name"] == "doca"
assert data["module_abi"][0]["name"] == "doca"
assert data["artifacts"][0]["path"] == "linux-image-test_1.0_arm64.deb"
assert data["artifacts"][0]["sha256"]
PY
}

test_release_summary_reads_provenance() {
    local root="$TMP/release-summary"
    mkdir -p "$root/run"
    cat > "$root/run/gb200-provenance.json" <<'EOF'
{
  "schema": "gb200.provenance.v1",
  "base": "noble-6.8",
  "arch": "arm64",
  "upstream_tag": "Ubuntu-6.8.0-124.124",
  "flavour": "generic",
  "run_id": "run-1",
  "builder": {"image": "localhost/gb200-builder:noble", "digest": "sha256:test"},
  "patches": [],
  "artifacts": [{"path": "linux-image-test.deb", "bytes": 1, "sha256": "abcdef0123456789"}],
  "module_abi": [{"name": "doca", "modules": [{"modinfo": {"vermagic": "test", "signer": "gb200"}}]}]
}
EOF
    python3 "$ROOT/scripts/write-release-summary.py" "$root" > "$TMP/release-summary.md"
    grep -q "noble-6.8 / arm64" "$TMP/release-summary.md" ||
        fail "release summary missing target"
    grep -q "doca" "$TMP/release-summary.md" ||
        fail "release summary missing module ABI"
}

test_build_modules_rejects_ambiguous_headers
test_build_modules_requires_matching_image_deb
test_build_modules_rejects_missing_headers
test_build_modules_extracts_kernel_package_version
test_build_modules_accepts_x86_64_alias
test_build_modules_rejects_missing_matrix_rows
test_check_module_sources_rejects_missing_matrix_rows
test_publish_repo_validates_manifest_before_podman
test_publish_repo_invokes_podman_for_valid_manifest
test_publish_repo_copies_provenance_after_success
test_publish_repo_does_not_copy_provenance_after_failure
test_publish_repo_copies_amd64_provenance_path
test_validate_apt_repo_invokes_podman_after_host_checks
test_module_abi_metadata_writes_json_for_ko_files
test_write_provenance_records_release_inputs
test_release_summary_reads_provenance

echo "script fixtures: ok"
