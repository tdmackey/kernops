# bpftrace

## noble

`noble/patches/0001-no-libsframe-dev.patch` removes `libsframe-dev` from
`Build-Depends`. noble has no package of that name; its `binutils-dev` contains
libsframe.

The build needs `bpftool` and the kernel headers of our own kernel build for
noble. `userspace/bpfcc/notes.md` explains why. Build the kernel for noble
before this package.

Remove the patch when the row takes a bpftrace version that does not depend on
`libsframe-dev`, or when noble is no longer a target.
