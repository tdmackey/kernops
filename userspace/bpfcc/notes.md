# bpfcc

## noble

`noble/patches/0001-lua-no-deterministic-bytecode-flag.patch`: bcc 0.35 builds
its Lua bindings with `luajit -bgd`. The `-d` option only exists in LuaJIT
releases from 2024 on; noble ships 2.1.0+git20231223. The patch drops `-d`,
which only gives up byte-for-byte reproducible bytecode. Remove the patch if
noble ever gets a newer LuaJIT.

**Still does not build on noble (2026-11-13).** With the patch the main build
passes, but the `libbpf-tools` stage needs `/usr/sbin/bpftool` to generate BPF
skeletons, and noble has no standalone `bpftool` package: its
`/usr/sbin/bpftool` is a wrapper from `linux-tools-common` that only works for
the running kernel. The fix is to build against the bpftool from our own 7.3
kernel tools package for noble, which does not exist yet.

`bpftrace` on noble is blocked the same way (`Build-Depends: bpftool`), and
additionally needs this package's `libbpfcc-dev`.
