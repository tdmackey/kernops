# Out-of-tree modules — nvidia-open, DOCA/OFED, internal

Decision (master plan + June 2026): **prebuilt, version-locked, signed debs
— never DKMS in production images.** This mirrors Canonical's LRM
(linux-restricted-modules) pattern: the module package is built once,
against exactly one kernel, and depends on exactly that kernel.

## Why not DKMS at install/boot time

- The immutable PXE image model has no compilers/headers in production.
- Per-base version skew (DOCA 3.0 on 6.8, 3.2 on 6.17) becomes an explicit,
  reviewed pin instead of whatever the node resolved at install time.
- Module signing requires the key at build time — a controlled signer in
  CI, not every node.
- `dkms mkbmdeb` was removed in dkms 2.8.8+ (Debian #1009179): the binary-
  module packaging glue is ours either way, so own it properly.

## The matrix

`modules/matrix.tsv`: `base <TAB> arch <TAB> module <TAB> version <TAB> source`.
One row = one (base x arch x module x version) pin. `arch` is the Debian
package architecture (`arm64` or `amd64`; `all` is accepted by the tooling for
future arch-independent rows). kernel-ci builds every row for a target whenever
that base's kernel or that row changes. Version bumps are one-line PRs gated by
the same CI.

## Build contract

`scripts/build-modules.sh <base> <kver-deb-dir> [series] [arch]` reads the matrix and runs
`modules/<name>/build.sh` for each row, **in matrix order** (DOCA before
nvidia-open — `nvidia_peermem` must resolve against OFED's
`Module.symvers`, not in-tree RDMA). Each `build.sh` runs inside the
builder container with:

- `KVER`        — the kernel version string (from the headers deb)
- `HEADERS_DIR` — directory containing linux-headers-<kver> .deb(s)
- `MODVER`      — the pinned module version
- `SRC`         — the pinned source (repo URL / apt repo)
- `SYMVERS_EXTRA` — Module.symvers from earlier rows (empty for the first)
- `OUT`         — where to drop `.ko`s + a `manifest.tsv`

and must produce out-of-tree `.ko`s built against those headers. The driver
script then:

1. **signs** every `.ko` (`scripts/sign-file sha512 <key>` — KMS/PKCS#11
   key in CI; unsigned in local dev, enforcement is phased per the master
   plan: sign from day 0, `module.sig_enforce=1` only after SB enrollment)
2. writes `modules/<name>-abi.json` with SHA256, `modinfo` fields
   (`vermagic`, `depends`, signer/hash data), signature marker state, and any
   undefined symbols reported by `nm -u`
3. packages `gb200-modules-<name>-<kver>_<modver>+kernel.<kernel-deb-version>_<arch>.deb`
   with `Depends: linux-image-<kver> (= <kernel-deb-version>)`,
   `/lib/modules/<kver>/updates/` layout, and a `depmod` trigger — our
   replacement for `dkms mkbmdeb`.

`scripts/write-provenance.py` embeds those ABI JSON files into
`gb200-provenance.json`; the dashboard and release summary use that data to
show module signer, `vermagic`, `.ko` count, and unresolved-symbol counts.

`scripts/check-module-sources.sh <base> [arch] [series]` is the cheap
preflight run before expensive builds. It verifies DOCA apt repos expose the
pinned DKMS package version and NVIDIA sources expose the pinned git tag.
`build-all.sh` runs it by default; set `MODULE_SOURCE_PREFLIGHT=0` only for a
deliberate offline/local experiment.

## Per-module notes

- **doca**: DOCA-Host ships DKMS *source* debs; `doca-kernel-support`
  rebuilds them (`WITH_MOD_SIGN=1 MODULE_SIGN_PUB_KEY=… MODULE_SIGN_PRIV_KEY=…`
  supported). We use its build, not its install. Canonical is repackaging
  the DKMS portion as `doca-ofed-25.10` for noble (LP #2139667) — if that
  lands as prebuilt for our kernels, rows can flip to consuming it.
- **nvidia-open**: github.com/NVIDIA/open-gpu-kernel-modules, supports
  4k/64k page sizes (not 16k), GB200-verified. Build:
  `make modules -j$(nproc) KERNEL_UNAME=$KVER` then `nvidia_peermem` with
  OFED symvers in `KBUILD_EXTRA_SYMBOLS`.
- **internal**: same contract, sources vendored under `modules/<name>/src`.

## Testing reality

Local/CI without a GPU or ConnectX can verify: modules build, are signed,
`depmod` resolves, packages install. Functional load (GPU + RDMA up,
`nvidia_peermem` symbol resolution at runtime) happens on GB200 staging —
the master plan's hardware checklist item 4. Don't mistake a green build
matrix for a tested driver.
