# Build pipeline — local development environment

There are two build paths:

- **Native Linux** (`scripts/build-line.sh`): for the upstream-stable lines
  such as 7.3-lts. Runs directly on the target Ubuntu series (a container, VM
  or WSL distro). See [workflows.md](workflows.md#build).
- **macOS + podman** (`scripts/build-all.sh`): for the Ubuntu-based hwe lines,
  including out-of-tree modules and publishing. The rest of this document
  describes that environment. Set `WORK_ROOT=/Volumes/Linux` (or wherever the
  case-sensitive volume is) before using it.

## Architecture

```
macOS host (Apple Silicon, arm64)
└── podman machine "kernel-builder"          applehv VM, 8 cpu / 20 GiB / 150 GB
    │                                        native arm64 Linux — same arch as
    │                                        GB200 Grace, no cross/emulation
    ├── $WORK_ROOT                       virtiofs bind mount (source trees,
    │                                        this repo, build outputs)
    ├── container kernel-builder:noble        ubuntu:24.04 + linux build-deps
    ├── container kernel-builder:resolute     ubuntu:26.04 + linux build-deps
    └── volumes kernel-ccache-{noble,resolute} ccache on VM-local disk
```

The GB200 target remains native `arm64` with 64k-page flavours. Release
parity for x86_64 uses Debian `amd64` package naming, native amd64 CI runners,
and the normal `generic`/`nvidia` flavours. End-to-end artifacts live under
`build/out/<base>/<arch>/<run-id>/` so both architectures can publish into the
same apt suite without overwriting each other.

Why this shape:

- **Builds must happen on Linux**; the kernel tree and packaging assume it.
- **Apple Silicon is arm64**, so the VM builds GB200 kernels natively — the
  same property we get from Graviton4 in CI. No qemu-user, no cross toolchain
  for the packaging path.
- **One builder image per Ubuntu series**, because `apt-get build-dep linux`
  must resolve against the target series. The images are interchangeable cattle — rebuild freely.
- **The volume is Case-sensitive APFS.** Required: kernel trees contain
  case-colliding paths. Never relocate sources to a case-insensitive volume.

virtiofs is slow for the many small files of a kernel build. `build-all.sh`
therefore copies the tree to a disk inside the VM, builds there, and copies the
packages back. ccache is on a VM-local volume.

## One-time setup

```sh
cd <this repo>
./scripts/host-setup.sh
```

Idempotent: creates the podman machine, starts it, builds both builder
images. You can run it again at any time.

## Source trees

`kpm fetch` keeps one bare repository, `$WORK_ROOT/kernel.git`, holding every
source tree with namespaced tags (`ubuntu-noble/Ubuntu-hwe-7.0-…`,
`stable/v7.3.4`). Nothing builds from a checkout of it: `kpm apply` writes a
plain source tree from a line's base tag and patch series.

- **Build production from release tags** that correspond to archive
  publications. New publications are detected through the Launchpad publishing
  API (`kpm updates`), not by scraping git.
- **`*-next` branches are unreleased staging**; never a build base.

## Flavour strategy: 4k locally, 64k on CI/hardware

Apple Silicon's hypervisor (HVF) cannot run 64k-page arm64 guests (M1/M2
support 4k/16k granules); 64k under QEMU falls back to TCG emulation, which
is unusably slow. So:

- **Local loop**: build and boot-test the `generic` (4k) flavour. Patches are
  page-size-independent in almost all cases, so 4k compile+boot validates the
  series.
- **64k flavours** (`generic-64k`, `nvidia-64k` — what GB200 actually runs):
  built locally when needed (compile check is fine, just no fast boot), but
  boot/kselftest-gated on Graviton CI runners and real hardware, where 64k is
  native. Anything that smells page-size-sensitive (mm, iommu, dma) must not
  ship on a 4k-only test pass.

`./scripts/boot-test.sh <linux-image-*.deb>` runs the local smoke test:
extracts the PE vmlinuz, boots it under qemu/hvf behind edk2 UEFI (the same
PE/zboot path PXE/UKI uses, so it regression-tests the zboot fix too) with a
minimal initramfs, and passes on a BOOT-SMOKE-OK marker. Needs
`brew install qemu` on the host.

## Building a kernel

```sh
./scripts/build-all.sh <line> [flavour] [series] [arch]     # hwe lines, in podman
./scripts/build-line.sh <line> <series>                     # stable lines, native
```

Both reconstruct the tree from the record first (`kpm verify --strict`, then
`kpm apply`), so a build can never depend on a developer's branch.

- `debian/rules clean` is mandatory first: it generates `debian/control` and
  friends from the packaging directory.
- The default is the **fast-iteration profile** (`skipdbg`, `skipabi`,
  `skipmodule`): no debug symbols, no ABI or module-list enforcement.
  `RELEASE=1` drops it.
- **`build-line.sh` does not use `fakeroot`.** (`build-all.sh`, for the hwe
  lines, still does.) The packaging declares
  `Rules-Requires-Root: no`. Under fakeroot every file operation of every
  compiler goes through one daemon; measured on a 24-core machine it left the
  CPU 82% idle and made the build about five times slower.
- .debs land in the parent directory of the source tree.

## Versioning convention (release builds)

Not implemented for the hwe lines: their packages have Canonical's version.
(`7.3-lts` takes its version from `kernel/identity.toml`.) The intended scheme
is a local suffix on the Ubuntu version, with the ABI field unchanged:

```
6.8.0-1046.49          Canonical
6.8.0-1046.49+<vendor>.1  ours (first respin on that base)
```

Mechanically: `dch` against `debian.<branch>/changelog` (noting `clean`
regenerates `debian/changelog` from it), distribution stays the series name.
Sorts above Canonical's binary and below their next upload. In the immutable
image model apt ordering only matters at image build time.

## Release publication validation

`publish.yml` does not publish into a candidate suite. Instead it assembles the
entire apt repository under `$RUNNER_TEMP/repo`, runs
`./scripts/validate-apt-repo.sh "$RUNNER_TEMP/repo" noble`, writes a release
summary from provenance, and only then `rsync`s the temporary tree to the live
repo host.

The validation checks the reprepro database, verifies provenance exists, and
uses apt against the local file repo for each published architecture so broken
indices, missing packages, and dependency-resolution mistakes are caught before
nodes can see the repo.

## Phase 0 exit check — the zboot/PE landmine (LP #2098111)

After any arm64 build, verify what the kernel binary actually is:

```sh
dpkg-deb --fsys-tarfile linux-image-*_arm64.deb \
  | tar -xO --wildcards './boot/vmlinuz-*' > /tmp/vmlinuz
file /tmp/vmlinuz
```

- `PE32+ executable ... Aarch64` → bootable by systemd-stub / UKI path. ✅
- `gzip compressed data` (raw `Image.gz`) → the LP #2098111 landmine; the
  zinstall/PE packaging fix would have to be carried in the line's series.
  The retired noble 6.8 base needed it; hwe-7.0 builds a native PE image. Keep
  this check for every arm64 build: Canonical's noble 7.0 NVIDIA packaging
  still builds `Image.gz`.

Phase 0 exit: a manually built `+<vendor>.0` `nvidia-64k` kernel that boots under
QEMU arm64 (64k) **as a PE `vmlinuz.efi`**.

## Patch stack workflow

See [workflows.md](workflows.md). In short: the workbench is a git branch that
`kpm import` builds; the record is the exported series plus the ledger, and
only the record is built.

## CI guardrails

The GitHub workflows intentionally use explicit token permissions,
concurrency groups, SHA-pinned actions, and `persist-credentials: false` on
checkout. `scripts/check-workflows.py` enforces those invariants in
`make check` so release hardening does not depend on review memory.

Still later: OCI release bundle and mkosi/UKI image pipeline.
