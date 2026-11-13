# bpfcc

## noble

`noble/patches/0001-lua-no-deterministic-bytecode-flag.patch`: bcc 0.35 builds
its Lua bindings with `luajit -bgd`. The `-d` option only exists in LuaJIT
releases from 2024 on; noble ships 2.1.0+git20231223. The patch drops `-d`,
which only gives up byte-for-byte reproducible bytecode. Remove the patch if
noble ever gets a newer LuaJIT.

The `libbpf-tools` stage needs `/usr/sbin/bpftool` to generate BPF skeletons.
noble has no standalone `bpftool` package (its `/usr/sbin/bpftool` is a wrapper
from `linux-tools-common` that only works for the running kernel), so
`scripts/build-userspace.sh` installs the bpftool from our own kernel tools
package for the series into the builder. That means the kernel has to be built
for a series (`scripts/build-line.sh <line> <series>`) before bpfcc or bpftrace
can be.

`bpftrace` has the same need, plus our kernel's userspace API headers (it uses
`BPF_TRACE_KPROBE_SESSION`, which noble's 6.8 headers lack); the script installs
those into the builder from the built kernel tree as well.
