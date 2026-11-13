#!/usr/bin/env bash
# Stage 1 of scripts/install-test.sh. Runs as root in a clean container of the
# series. /repo has every package we built. Prints one line per check:
#   ok <what>   FAIL <what>   skip <what>
set -uo pipefail
exec </dev/null
export DEBIAN_FRONTEND=noninteractive
fail=0
ok()   { echo "ok    $*"; }
bad()  { echo "FAIL  $*"; fail=1; }
skip() { echo "skip  $*"; }

apt-get -qq update >/dev/null
# What a real system of the series has before a kernel is installed.
apt-get -qq install -y --no-install-recommends dpkg-dev initramfs-tools linux-base kmod zstd ca-certificates >/dev/null 2>&1
( cd /repo && dpkg-scanpackages -m . > Packages 2>/dev/null )
echo "deb [trusted=yes] file:/repo ./" > /etc/apt/sources.list.d/ours.list
apt-get -qq update >/dev/null

# Every binary package we built, each at our version. Packages named in
# tests/install/exclude.tsv for this series are left out, with the reason.
mapfile -t all < <(awk '/^Package:/{p=$2} /^Version:/{print p"="$2}' /repo/Packages | sort -u)
want=()
for pv in "${all[@]}"; do
    # A signed image (scripts/sign-kernel.sh) takes the place of the unsigned one.
    case "$pv" in linux-image-unsigned-*)
        signed="linux-image-${pv#linux-image-unsigned-}"
        if printf '%s\n' "${all[@]}" | grep -Fxq "$signed"; then skip "install ${pv%%=*}: the signed image is installed instead"; continue; fi ;;
    esac
    why="$(awk -F'\t' -v s="$SERIES" -v p="${pv%%=*}" '!/^#/ && ($1==s || $1=="*") && $2==p {print $3; exit}' "$MONO/tests/install/exclude.tsv")"
    if [ -n "$why" ]; then skip "install ${pv%%=*}: $why"; else want+=("$pv"); fi
done
echo "── install ${#want[@]} packages"
if apt-get install -y --no-install-recommends "${want[@]}" > /tmp/install.log 2>&1; then
    ok "all ${#want[@]} packages install and configure"
else
    bad "apt-get install of our packages"
    grep -E '^E:|Depends:|Conflicts:|Breaks:|but it is not|is to be installed|dpkg: error|run-parts|returned error' /tmp/install.log | head -25
fi
audit="$(dpkg --audit 2>&1)"
[ -z "$audit" ] && ok "no package is left half-installed" || { bad "dpkg --audit reports problems"; echo "$audit" | head -10; }

kver="$(ls /boot | sed -n 's/^vmlinuz-//p' | sort -V | tail -n1)"
echo "── kernel $kver"
echo "$kver" > /out/kver
[ -n "$kver" ] && ok "kernel image is in /boot" || bad "no /boot/vmlinuz-*"
if dpkg -s "linux-image-$kver" >/dev/null 2>&1; then
    apt-get -qq install -y --no-install-recommends sbsigntool >/dev/null 2>&1
    signer="$(sbverify --list "/boot/vmlinuz-$kver" 2>/dev/null | sed -n 's/^ - subject: //p' | head -n1)"
    [ -n "$signer" ] && ok "the kernel image is signed for Secure Boot ($signer)" || bad "linux-image-$kver is installed but its image has no signature"
fi
[ -s "/boot/initrd.img-$kver" ] && ok "initramfs was generated" || bad "no /boot/initrd.img-$kver"
[ -s "/boot/config-$kver" ] && [ -s "/boot/System.map-$kver" ] && ok "config and System.map are in /boot" || bad "config or System.map missing in /boot"
[ -f "/lib/modules/$kver/modules.dep" ] && ok "depmod ran" || bad "no modules.dep for $kver"
[ -d "/lib/modules/$kver/build/include" ] && ok "kernel headers are usable (/lib/modules/$kver/build)" || bad "kernel headers not usable"
n="$(lsinitramfs "/boot/initrd.img-$kver" 2>/tmp/lsinitramfs.err | grep -c "lib/modules/$kver/.*\.ko")"
if [ "$n" -gt 0 ]; then ok "initramfs contains $n modules of $kver"; else bad "initramfs has no modules of $kver"; head -3 /tmp/lsinitramfs.err; fi
# A module of the kernel, and its compression, must be readable by this series' kmod.
modinfo -k "$kver" nvme >/dev/null 2>&1 && ok "kmod reads the kernel's modules (nvme)" || bad "modinfo -k $kver nvme"

# Out-of-tree modules: built for exactly this kernel, and found before the kernel's own.
for dir in /lib/modules/"$kver"/updates/*/; do
    [ -d "$dir" ] || continue
    name="$(basename "$dir")"; n=0; wrong=0
    for ko in "$dir"*.ko; do
        n=$((n + 1))
        [ "$(modinfo -F vermagic "$ko" | cut -d' ' -f1)" = "$kver" ] || wrong=$((wrong + 1))
    done
    [ "$wrong" -eq 0 ] && ok "$name: $n modules built for $kver" || bad "$name: $wrong of $n modules built for another kernel"
done
if [ -d "/lib/modules/$kver/updates/doca" ]; then
    case "$(modinfo -k "$kver" -F filename mlx5_core 2>/dev/null)" in
        */updates/doca/*) ok "mlx5_core resolves to the DOCA module, not the kernel's" ;;
        *) bad "mlx5_core does not resolve to the DOCA module" ;;
    esac
fi

echo "── tools"
cd /tmp                       # some tools write a file where they stand
tools="$(ls -d /usr/lib/linux-"$VENDOR"-tools-* 2>/dev/null | head -n1)"
while IFS=$'\t' read -r pkg cmd; do
    [[ -z "$pkg" || "$pkg" == \#* ]] && continue
    [ "$pkg" = "@TOOLSPKG@" ] && pkg="$(dpkg-query -W -f '${Package}
' "linux-$VENDOR-tools-[0-9]*" 2>/dev/null | head -n1)"
    if [ -z "$pkg" ] || ! dpkg -s "$pkg" >/dev/null 2>&1; then continue; fi          # not built for this series
    cmd="${cmd//@TOOLS@/$tools}"
    if out="$(timeout 60 bash -c "$cmd" 2>&1)"; then ok "$pkg: $cmd"; else bad "$pkg: $cmd"; echo "      ${out:0:300}"; fi
done < "$MONO/tests/install/smoke.tsv"

# The same system, for stage 2 to boot.
if [ "${KEEP_ROOT:-1}" != 0 ]; then
    tar -cf /out/rootfs.tar --one-file-system --exclude=./out --exclude=./repo --exclude=./proc --exclude=./sys \
        --exclude=./dev --exclude="./${MONO#/}" -C / . 2>/dev/null
fi
echo "── container checks done"
exit "$fail"
