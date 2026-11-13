# kernops: kernel patch series and build pipeline

Maintains Linux kernels as **a base tag plus a patch series**, and builds them
as Ubuntu packages for amd64 and arm64 (4k and 64k pages).

Each kernel is a *line* under `kernel/lines/`:

| Line | Base | Carries |
|---|---|---|
| `7.3-lts` | upstream stable `v7.3.N` | every kernel patch from Ubuntu, NVIDIA, GCP, AWS and Azure's 7.3 trees, plus our own |
| `hwe-7.0`, `hwe-6.17` | Canonical's `Ubuntu-hwe-*` release tags | our own backports only |

The series is the record: CI builds from the files in this repo, never from a
developer's branch. A *ledger* accounts for every commit in every vendor tree
by content, so a patch is recognised even when its SHA changed in a backport.

Design and rationale: [docs/design-7.3-lts.md](docs/design-7.3-lts.md).
Day-to-day procedures: [docs/workflows.md](docs/workflows.md).

## Layout

```
kernel/
  identity.toml          product name, architectures, version scheme
  sources.toml           every source tree kpm knows about
  lines/<line>/
    line.toml            base tag and which sources this line uses
    series               patch order (authoritative)
    patches/<band>-<source>/NNNN-*.patch
    ledger/<source>.jsonl   one row per source commit, with a decision
    pins.json            exact vendor refs the ledgers describe
    policy.toml          default decisions
    config/              vendor config union and our own overrides
modules/                 out-of-tree module pins and build scripts
scripts/                 kpm launcher, build, publish and release helpers
tools/kpm/               the kernel patch manager
tools/dashboard/         status page
env/                     builder container definitions
tests/                   unit tests, an end-to-end kpm test, script fixtures
```

## Quickstart

Everything that touches a kernel tree needs Linux on a **case-sensitive
filesystem** (kernel sources contain names that differ only by case). On
Windows use WSL on its own ext4 disk, not a `/mnt/<drive>` path.

```sh
export WORK_ROOT=~/kernops                 # clones, build output, apt repo
export KPM_LINE=7.3-lts                    # or pass --line to each command

./scripts/kpm fetch                        # base + vendor trees into $WORK_ROOT/kernel.git
./scripts/kpm status                       # what each source contributes
./scripts/kpm verify --strict              # ledger complete, series applies

./scripts/build-line.sh 7.3-lts resolute   # tree -> packaging -> config -> debs
./scripts/vng-boot.sh 7.3-lts              # boot the result under virtme-ng
```

`KPM_MIRRORS=/path/to/clones` makes `kpm fetch` use local clones instead of the
network.

## Common tasks

```sh
./scripts/kpm why "<sha, change id or subject text>"   # is it in our kernel, and why?
./scripts/kpm rebase v7.3.5                # move to a new base; drops what landed upstream
./scripts/kpm add <mainline-sha>           # carry one of our own backports
./scripts/kpm harvest && ./scripts/kpm resolve   # a vendor published a new tree
./scripts/kpm updates                      # newer base tags, vendor trees that moved
make check                                 # static checks and tests
```

## CI

- `kernel-ci.yml`: on every PR, checks each line's ledger and series on a
  hosted runner, then builds, boots and tests the Ubuntu-based lines on
  self-hosted runners.
- `update-and-rebase.yml`: daily; when Canonical publishes a new kernel,
  rebases the affected line and opens a PR (or an issue on conflict).
- `publish.yml`: on merge, release build, apt repository validation, publish.

The 7.3-lts line is checked in CI but not yet built or published there.
