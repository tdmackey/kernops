# Build pipeline — local development environment

Status: Phase 0 (foundations). This doc covers the local macOS build env and
the manual kernel build loop. CI (GitHub Actions on Graviton4) replicates
these exact steps later — keep this doc and the scripts in lockstep.

## Architecture

```
macOS host (Apple Silicon, arm64)
└── podman machine "gb200-builder"          applehv VM, 8 cpu / 20 GiB / 150 GB
    │                                        native arm64 Linux — same arch as
    │                                        GB200 Grace, no cross/emulation
    ├── /Volumes/Linux                       virtiofs bind mount (source trees,
    │                                        this repo, build outputs)
    ├── container gb200-builder:noble        ubuntu:24.04 + linux build-deps
    ├── container gb200-builder:resolute     ubuntu:26.04 + linux build-deps
    └── volumes gb200-ccache-{noble,resolute} ccache on VM-local disk
```

Why this shape:

- **Builds must happen on Linux**; the kernel tree and packaging assume it.
- **Apple Silicon is arm64**, so the VM builds GB200 kernels natively — the
  same property we get from Graviton4 in CI. No qemu-user, no cross toolchain
  for the packaging path.
- **One builder image per Ubuntu series**, because `apt-get build-dep linux`
  must resolve against the target series (noble toolchain for 6.8/6.17,
  resolute for 7.0). The images are interchangeable cattle — rebuild freely.
- **The volume is Case-sensitive APFS.** Required: kernel trees contain
  case-colliding paths. Never relocate sources to a case-insensitive volume.

Known caveat: virtiofs is slow for the many-small-files I/O of a kernel build.
ccache (VM-local volume) absorbs recompiles. If full builds prove too slow,
the fallback is rsync the tree to VM-local disk, build there, copy .debs back
— measure before adding that complexity.

## One-time setup

```sh
cd /Volumes/Linux/gb200
./scripts/host-setup.sh
```

Idempotent: creates the podman machine, starts it, builds both builder
images, creates ccache volumes. Re-run any time; it converges.

## Source trees

Single clone per Launchpad repo, all branches; build specific tags via
worktrees so the main checkout stays untouched:

```sh
cd /Volumes/Linux/noble
git fetch origin
git tag -l 'Ubuntu-*' --sort=-creatordate | head        # what's buildable
git worktree add /Volumes/Linux/build/noble-6.8 Ubuntu-6.8.0-60.63   # example
```

Branch vs tag discipline (from the master plan):

- **Build production from `Ubuntu-*` release tags** that correspond to archive
  publications. Detection of new publications is via the Launchpad publishing
  API / USN OSV feed, not git scraping.
- **`*-next` branches are unreleased staging** for the next SRU cycle — use
  only for early conflict warning (dry-run rebases), never as a build base.
- `linux-nvidia` trees use `debian.nvidia/`; plain `linux` uses
  `debian.master/`. The flavour name (e.g. `nvidia-64k`) selects within that.

## Building a kernel

```sh
./scripts/build-kernel.sh <source-tree> [flavour] [series]
# e.g.
./scripts/build-kernel.sh /Volumes/Linux/build/noble-6.8 generic noble
./scripts/build-kernel.sh /Volumes/Linux/build/nvidia-6.8 nvidia-64k noble
```

What it runs inside the container:

```sh
export DEB_BUILD_OPTIONS="parallel=$(nproc)"
fakeroot debian/rules clean          # regenerates debian/ from debian.<branch>/
fakeroot debian/rules binary-headers binary-<flavour> \
    do_tools=false skipdbg=true skipabi=true skipmodule=true
```

- `clean` is mandatory first — it generates `debian/control` and friends from
  the per-branch packaging directory.
- The skip flags are the **fast-iteration profile**: no tools packages, no
  debug symbols (the dbgsym deb is gigabytes), no ABI/module-list enforcement
  against the previous upload. **Release builds drop `skipdbg`** and decide
  the ABI question deliberately (we are an internal fork: keep
  `skipabi=true skipmodule=true` until/unless we maintain our own ABI files
  for out-of-tree module compatibility).
- .debs land in the parent directory of the source tree.

## Versioning convention (release builds)

Append a local suffix to the Ubuntu version in the changelog; never touch the
ABI field:

```
6.8.0-1046.49          Canonical
6.8.0-1046.49+gb200.1  ours (first respin on that base)
```

Mechanically: `dch` against `debian.<branch>/changelog` (noting `clean`
regenerates `debian/changelog` from it), distribution stays the series name.
Sorts above Canonical's binary and below their next upload. In the immutable
image model apt ordering only matters at image build time.

## Phase 0 exit check — the zboot/PE landmine (LP #2098111)

After any arm64 build, verify what the kernel binary actually is:

```sh
dpkg-deb --fsys-tarfile linux-image-*_arm64.deb \
  | tar -xO --wildcards './boot/vmlinuz-*' > /tmp/vmlinuz
file /tmp/vmlinuz
```

- `PE32+ executable ... Aarch64` → bootable by systemd-stub / UKI path. ✅
- `gzip compressed data` (raw `Image.gz`) → the LP #2098111 landmine; the
  zinstall/PE packaging fix must be carried in `kernel/patches/gb200/` for
  noble. Expected state today: **noble 6.8 fails this check** (fix needed),
  resolute 7.0 expected native PE (must be empirically confirmed on
  `linux-nvidia-64k`).

Phase 0 exit: a manually built `+gb200.0` `nvidia-64k` kernel that boots under
QEMU arm64 (64k) **as a PE `vmlinuz.efi`**.

## Patch stack workflow (summary; tooling lands in Phase 1)

- Working medium: branch `gb200-patches` rebased onto the pinned Ubuntu tag
  (`git rebase --onto <new-tag> <old-tag>`), conflicts resolved in git with
  `rerere`, reviewed with `git range-diff`.
- Artifact of record: `git format-patch` export into `kernel/patches/gb200/`
  plus the pin in `kernel/upstream-base.txt`, committed together in this repo.
  The monorepo alone must be able to reconstruct the tree.
- Sections, in apply order: `packaging/` (zboot fix — noble-only),
  `config/` (signing wiring), `backports/` (upstream cherry-picks, `-x`
  trailers mandatory), `cve/` (temporary until Canonical SRUs land).
- Drop rules: backports drop when their upstream SHA reaches the base;
  packaging/config drop on explicit conditions (LP Fix-Released; resolute).

## Not yet wired (later phases)

Signing (KMS/PKCS#11), module matrix (nvidia-open/DOCA), QEMU boot + kselftest
gate, Launchpad publication detector, OCI release bundle, mkosi/UKI image
pipeline. Each gets its own doc as it lands.
