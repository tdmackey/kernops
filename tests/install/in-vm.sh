#!/bin/sh
# Stage 2 of scripts/install-test.sh. Runs as root in a VM that booted the
# installed kernel with the installed system as its root filesystem.
# Usage: in-vm.sh <kernel release that must be running>
want="$1"
fail=0
ok()   { echo "ok    $*"; }
bad()  { echo "FAIL  $*"; fail=1; }
skip() { echo "skip  $*"; }
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

echo "── running kernel $(uname -r)"
[ "$(uname -r)" = "$want" ] && ok "the installed kernel is the one running" || bad "running $(uname -r), expected $want"

# Modules come from the installed packages, through the series' own kmod.
for m in nvme virtio_net xfs btrfs nf_tables; do
    modprobe "$m" 2>/dev/null && ok "modprobe $m" || bad "modprobe $m"
done
if [ -d "/lib/modules/$want/updates/doca" ]; then
    if modprobe mlx5_core 2>/tmp/e; then
        case "$(cat /sys/module/mlx5_core/srcversion 2>/dev/null):$(modinfo -F filename mlx5_core)" in
            *:*/updates/doca/*) ok "mlx5_core from DOCA loads (no device needed)" ;;
            *) bad "mlx5_core loaded, but not from the DOCA package" ;;
        esac
        modprobe mlx5_ib 2>/dev/null && ok "mlx5_ib from DOCA loads" || bad "modprobe mlx5_ib"
    else
        bad "modprobe mlx5_core (DOCA): $(head -c 200 /tmp/e)"
    fi
fi
if [ -d "/lib/modules/$want/updates/nvidia-open" ]; then
    # The driver needs a GPU to stay loaded; without one modprobe reports "No such device".
    modprobe nvidia 2>/tmp/e && ok "nvidia loads" || {
        if grep -qi 'no such device' /tmp/e; then ok "nvidia: symbols resolve (it stops for lack of a GPU)"; else bad "modprobe nvidia: $(head -c 200 /tmp/e)"; fi
    }
fi
echo "      taint: $(cat /proc/sys/kernel/tainted)"

# Signature enforcement, when the test asked for it (SIG_ENFORCE=1).
if [ "$(cat /sys/module/module/parameters/sig_enforce 2>/dev/null)" = Y ]; then
    ok "the kernel enforces module signatures"
    # A module with its signature cut off must be refused.
    src="$(find "/lib/modules/$want/kernel/drivers/net" -name 'dummy.ko*' | head -n1)"
    if [ -n "$src" ]; then
        case "$src" in *.zst) zstd -dqc "$src" > /tmp/unsigned.ko ;; *) cp "$src" /tmp/unsigned.ko ;; esac
        size="$(stat -c %s /tmp/unsigned.ko)"
        head -c "$((size - 40))" /tmp/unsigned.ko > /tmp/cut.ko          # removes the signature marker
        if insmod /tmp/cut.ko 2>/tmp/e; then bad "a module without a signature was loaded"; rmmod dummy 2>/dev/null
        else ok "a module without a signature is refused ($(sed 's/.*: //' /tmp/e | head -c 60))"; fi
    else skip "no dummy module to test a refused load"; fi
    for d in /lib/modules/"$want"/updates/*/; do
        [ -d "$d" ] || continue
        n=0; signed=0
        for ko in "$d"*.ko; do
            n=$((n + 1)); modinfo -F signer "$ko" 2>/dev/null | grep -q . && signed=$((signed + 1))
        done
        [ "$n" -eq "$signed" ] && ok "$(basename "$d"): all $n modules are signed ($(modinfo -F signer "$d"$(ls "$d" | head -n1)))" \
            || bad "$(basename "$d"): $((n - signed)) of $n modules are not signed"
    done
fi

# BTF and BPF: pahole made the kernel's BTF; libbpf, bpftool and bpftrace read it.
[ -s /sys/kernel/btf/vmlinux ] && ok "the kernel has BTF" || bad "no /sys/kernel/btf/vmlinux"
tools="$(ls -d /usr/lib/linux-*-tools-* 2>/dev/null | head -n1)"
if [ -x "$tools/bpftool" ]; then
    "$tools/bpftool" btf dump file /sys/kernel/btf/vmlinux format c 2>/dev/null | grep -q 'struct task_struct' \
        && ok "bpftool reads the kernel's BTF" || bad "bpftool btf dump"
    "$tools/bpftool" prog show >/dev/null 2>&1 && ok "bpftool talks to the running kernel" || bad "bpftool prog show"
fi
if command -v bpftrace >/dev/null; then
    out="$(timeout 60 bpftrace -e 'BEGIN { printf("bt-ok\n"); exit(); }' 2>&1)"
    echo "$out" | grep -q bt-ok && ok "bpftrace compiles and runs a program" || bad "bpftrace: $(echo "$out" | head -c 240)"
else skip "bpftrace is not built for this series"; fi
if [ -x "$tools/perf" ]; then
    "$tools/perf" stat -e task-clock true >/dev/null 2>&1 && ok "perf stat runs" || bad "perf stat"
fi
command -v trace-cmd >/dev/null && { trace-cmd list -e sched 2>/dev/null | grep -q sched_switch && ok "trace-cmd lists kernel events" || bad "trace-cmd list"; }

# Networking and storage tools against the running kernel.
command -v ip >/dev/null && { ip -j -d link show lo >/dev/null 2>&1 && ok "ip link (netlink) works" || bad "ip link"; }
command -v devlink >/dev/null && { devlink dev show >/dev/null 2>&1 && ok "devlink talks to the kernel" || bad "devlink dev show"; }
command -v ethtool >/dev/null && { ethtool -i lo >/dev/null 2>&1 || ethtool lo >/dev/null 2>&1; ok "ethtool runs against a device"; }
command -v nvme >/dev/null && { nvme list >/dev/null 2>&1 && ok "nvme list" || bad "nvme list"; }
command -v cxl >/dev/null && { cxl list >/dev/null 2>&1 && ok "cxl list" || bad "cxl list"; }

# Crash dump path: kexec must accept this kernel image (zstd on arm64, bzImage here).
if command -v kexec >/dev/null; then
    initrd=""; [ -s "/boot/initrd.img-$want" ] && initrd="--initrd=/boot/initrd.img-$want"
    out="$(kexec -l "/boot/vmlinuz-$want" $initrd --reuse-cmdline 2>&1)" && { ok "kexec loads the installed kernel image"; kexec -u >/dev/null 2>&1; } \
        || bad "kexec -l: $(echo "$out" | head -c 240)"
fi
if command -v makedumpfile >/dev/null && [ -r /proc/kcore ]; then
    # Needs memory reserved for a crash kernel; the test boots with crashkernel=.
    out="$(makedumpfile --mem-usage /proc/kcore 2>&1)"; mrc=$?
    if [ "$mrc" -ne 0 ]; then
        bad "makedumpfile --mem-usage /proc/kcore"; echo "$out" | grep -v '^$' | head -n 12 | sed 's/^/      /'
    elif echo "$out" | grep -q 'kernel version is not supported'; then
        bad "makedumpfile does not declare support for this kernel version"
    else
        ok "makedumpfile reads this kernel's memory layout, and supports its version"
    fi
fi

# The kernel log. A warning listed in /known-warnings.tsv is reported, not failed.
dmesg | grep -E 'WARNING: |BUG: |Oops' > /tmp/warn || true
unknown=0
while IFS= read -r line; do
    why=""
    while IFS="$(printf '\t')" read -r pattern reason; do
        case "$pattern" in ''|'#'*) continue ;; esac
        if echo "$line" | grep -Eq -- "$pattern"; then why="$reason"; break; fi
    done < /known-warnings.tsv
    if [ -n "$why" ]; then
        skip "known kernel warning: $(echo "$line" | sed 's/^\[[^]]*\] *//' | cut -c1-90)"
    else
        unknown=$((unknown + 1))
        bad "kernel log: $(echo "$line" | cut -c1-150)"
    fi
done < /tmp/warn
if [ "$unknown" -gt 0 ]; then
    dmesg | grep -E -B14 -A16 'WARNING: |BUG: |Oops' | head -n 80 | cut -c1-180 | sed 's/^/      /'
else
    ok "kernel log has no unknown BUG, Oops or WARNING"
fi
echo "── vm checks done"
exit "$fail"
