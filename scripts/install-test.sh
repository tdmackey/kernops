#!/usr/bin/env bash
# Install everything we built for a series into a clean system of that series,
# and check that it works.
#
# Usage: install-test.sh <line> <series>
#   e.g. install-test.sh 7.3-lts noble
#
# What it installs, from one local apt repository:
#   the kernel packages     $WORK_ROOT/build/<line>/<series>/*.deb
#   the module packages     $WORK_ROOT/build/<line>/<series>/modules/*.deb
#   the userspace packages  $WORK_ROOT/userspace/<series>/*.deb
#
# Stage 1 runs in a plain container of the series (the official image, not the
# builder): every package must install and configure, the initramfs must be
# made, the modules must resolve, and each tool must run (tests/install/smoke.tsv).
#
# Stage 2 boots the kernel in a VM with virtme-ng and enters the installed
# system there (chroot). Modules are then loaded from the installed packages by
# the series' own kmod, and the tools that need a running kernel are run
# (tests/install/in-vm.sh). BOOT=0 skips it.
# virtme-ng starts the kernel from the build tree, because it needs the tree to
# find the modules for its own start-up. The script first checks that the
# image in the package is the same file as the image in the tree.
# SIG_ENFORCE=1 turns on module signature enforcement before it enters the
# installed system: from there only signed modules load.
#
# Not covered: the bootloader, the initramfs at boot, and Secure Boot firmware.
#
# The result is written to $WORK_ROOT/build/<line>/<series>/install-test.json,
# which the status page shows.
set -euo pipefail
# shellcheck source=scripts/lib/common.sh
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"

MONO="$(cd "$(dirname "$0")/.." && pwd)"
LINE="${1:?usage: install-test.sh <line> <series>}"
SERIES="${2:?usage: install-test.sh <line> <series>}"
SERIES_VER="$(sed -nE "/^\[series\]/,\$ s/^${SERIES}[[:space:]]*=[[:space:]]*\"([^\"]+)\".*/\1/p" "$MONO/kernel/identity.toml" | head -n1)"
[ -n "$SERIES_VER" ] || { echo "!! series '$SERIES' is not in kernel/identity.toml [series]" >&2; exit 1; }
IMAGE="${INSTALL_TEST_IMAGE:-docker.io/library/ubuntu:$SERIES_VER}"

KDIR="$WORK_ROOT/build/$LINE/$SERIES"
UDIR="$WORK_ROOT/userspace/$SERIES"
T="$WORK_ROOT/install-test/$LINE-$SERIES"
ls "$KDIR"/linux-image-*.deb >/dev/null 2>&1 || { echo "!! no kernel packages in $KDIR (run build-line.sh $LINE $SERIES)" >&2; exit 1; }
exec </dev/null

# An unpacked system has directories that their owner cannot write; make them removable first.
clean() { [ ! -e "$1" ] || { chmod -R u+rwX "$1" 2>/dev/null || true; rm -rf "$1"; }; }
clean "$T"; mkdir -p "$T/repo" "$T/out"
cp "$KDIR"/*.deb "$T/repo/"
cp "$KDIR"/modules/*.deb "$T/repo/" 2>/dev/null || true
cp "$UDIR"/*.deb "$T/repo/" 2>/dev/null || true
echo "════ install test: $LINE on $SERIES ($(find "$T/repo" -name '*.deb' | wc -l) packages, image $IMAGE)"

rc=0
podman run --rm --name "install-test-$LINE-$SERIES-$$" \
    -v "$MONO:$MONO:ro" -v "$T/repo:/repo" -v "$T/out:/out" \
    -e MONO="$MONO" -e SERIES="$SERIES" -e VENDOR="$VENDOR" -e KEEP_ROOT="${BOOT:-1}" \
    "$IMAGE" bash "$MONO/tests/install/in-container.sh" > "$T/container.log" 2>&1 || rc=$?
grep -aE '^(ok|FAIL|skip|──|!!)' "$T/container.log" || true
if [ "$rc" -ne 0 ]; then
    echo "!! stage 1 failed; log: $T/container.log" >&2
    tail -15 "$T/container.log" | cut -c1-200 >&2
fi

boot="skipped"
if [ "$rc" -eq 0 ] && [ "${BOOT:-1}" != 0 ]; then
    if ! command -v vng >/dev/null; then
        echo "skip  boot of the installed system: virtme-ng (vng) is not installed"
        boot="no vng"
    else
        echo "── boot the installed system"
        clean "$T/root"; mkdir -p "$T/root" "$HOME/.cache/virtme-ng"
        # The container wrote its filesystem as a tar file; unpack it as ourselves.
        tar -xf "$T/out/rootfs.tar" -C "$T/root" --no-same-owner --exclude='./dev/*' --exclude='./proc/*' --exclude='./sys/*' 2>/dev/null || true
        kver="$(cat "$T/out/kver")"
        brc=0
        build="" image=""
        for b in "$KDIR"/tree/debian/build/build-*; do
            if [ "$(cat "$b/include/config/kernel.release" 2>/dev/null || true)" = "$kver" ]; then build="$b"; fi
        done
        for f in "$build"/arch/*/boot/bzImage "$build"/arch/*/boot/vmlinuz.efi; do
            if [ -n "$build" ] && [ -f "$f" ]; then image="$f"; break; fi
        done
        if [ -z "$build" ] || [ -z "$image" ]; then
            echo "!! no build tree for $kver in $KDIR/tree: cannot boot" >&2; brc=1
        elif ! cmp -s "$image" "$T/root/boot/vmlinuz-$kver"; then
            # A signed image differs from the tree's by its signature. Then the
            # unsigned package, which the signed one was made from, must be the tree's image.
            unsigned="$(ls "$T/repo"/linux-image-unsigned-"$kver"_*.deb 2>/dev/null | head -n1)"
            if [ -n "$unsigned" ] && ls "$T/repo"/linux-image-"$kver"_*.deb >/dev/null 2>&1 \
                    && dpkg-deb --fsys-tarfile "$unsigned" | tar -xO "./boot/vmlinuz-$kver" 2>/dev/null | cmp -s - "$image"; then
                echo "ok    the installed image is the signed form of the image of the build tree"
            else
                echo "!! the image in the package is not the image of the build tree" >&2; brc=1
            fi
        else
            echo "ok    the image in the package is the image of the build tree"
        fi
        if [ "$brc" -eq 0 ]; then
            chmod u+rwx "$T/root"
            mkdir -p "$T/root/proc" "$T/root/sys" "$T/root/dev" "$T/root/tmp" "$T/root/run"
            cp "$MONO/tests/install/in-vm.sh" "$MONO/tests/install/known-warnings.tsv" "$T/root/"
            append=(--append crashkernel=256M)                # makedumpfile and kexec checks need a reservation
            # The VM starts on the host's files, then enters the installed system:
            # from there on, modules, kmod and every tool are the installed ones.
            {
                echo "mount -t proc proc '$T/root/proc' && mount -t sysfs sys '$T/root/sys' && mount --bind /dev '$T/root/dev' || exit 1"
                echo "mount -t tracefs tracefs '$T/root/sys/kernel/tracing' 2>/dev/null"
                echo "mount -t debugfs debugfs '$T/root/sys/kernel/debug' 2>/dev/null"
                echo "mount -t bpf bpf '$T/root/sys/fs/bpf' 2>/dev/null"
                echo "mount -t tmpfs tmpfs '$T/root/tmp'; mount -t tmpfs tmpfs '$T/root/run'"
                # Enforcement starts here, not at boot: virtme-ng loads its own
                # modules from the build tree, where they are not signed yet
                # (the packaging signs them). After this line only signed
                # modules load, and they come from the installed packages.
                [ "${SIG_ENFORCE:-0}" != 1 ] || echo "echo 1 > /sys/module/module/parameters/sig_enforce || exit 1"
                echo "exec chroot '$T/root' /bin/sh /in-vm.sh '$kver'"
            } > "$T/root/enter.sh"
            ( cd "$build" && timeout "${TIMEOUT:-600}" vng --run . --user root --memory 4G --rwdir="$T/root" "${append[@]}" \
                -- sh "$T/root/enter.sh" ) > "$T/vm.log" 2>&1 || brc=$?
        fi
        grep -aE '^(ok|FAIL|skip|──|!!)' "$T/vm.log" 2>/dev/null || true
        if [ "$brc" -eq 0 ] && grep -q '^── vm checks done' "$T/vm.log"; then
            boot="ok"
        else
            boot="failed"; rc=1
            echo "!! stage 2 failed; log: $T/vm.log" >&2
            tail -15 "$T/vm.log" | cut -c1-200 >&2
        fi
        clean "$T/root"
    fi
fi
rm -f "$T/out/rootfs.tar"

python3 - "$KDIR/install-test.json" "$LINE" "$SERIES" "$rc" "$boot" "$T/container.log" "$T/vm.log" <<'EOF'
import datetime, json, os, re, sys
path, line, series, rc, boot, clog, vlog = sys.argv[1:]
def rows(p):
    out = {"ok": [], "FAIL": [], "skip": []}
    if os.path.exists(p):
        for l in open(p, encoding="utf-8", errors="replace"):
            m = re.match(r"^(ok|FAIL|skip)\s+(.*)", l)
            if m:
                out[m.group(1)].append(m.group(2).strip())
    return out
c, v = rows(clog), rows(vlog)
json.dump({"line": line, "series": series, "ok": rc == "0", "boot": boot,
           "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "passed": len(c["ok"]) + len(v["ok"]), "failed": c["FAIL"] + v["FAIL"], "skipped": c["skip"] + v["skip"]},
          open(path, "w", encoding="utf-8", newline="\n"), indent=1)
EOF
echo "════ install test $LINE/$SERIES: $([ "$rc" -eq 0 ] && echo passed || echo FAILED) (boot: $boot); record: $KDIR/install-test.json"
exit "$rc"
