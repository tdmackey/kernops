# gb200 — Ubuntu kernel fork & build pipeline

Thin patches-on-top-of-Ubuntu kernel fork tracking Canonical `linux-nvidia` /
`linux-nvidia-64k` (noble 6.8 -> resolute 7.0) for GB200 (Grace arm64,
64k pages), with release-parity `amd64` builds for x86_64 fleet images.
See `/Volumes/Linux/linux-image-buidler.md` (master plan) for the full design;
this repo is its implementation.

## Layout

```
kernel/
  patches/gb200/      exported patch series (artifact of record; edited as a git
                      branch in the kernel tree, exported here by CI)
  patches/cve/        temporary CVE patches, dropped when Canonical SRUs land
  configs/            config fragments (module signing keys, etc.)
  upstream-base.txt   pinned Ubuntu tag the series applies to
modules/              out-of-tree module packaging (nvidia-open, DOCA/OFED, internal)
signing/              PUBLIC certs only — private keys live in KMS/PKCS#11
image/                mkosi configs (UKI/PXE image pipeline, later phase)
workflows/            CI definitions
env/                  builder container definitions (noble, resolute)
scripts/              host-side helpers (env setup, build, rebase)
docs/                 workflows.md is the operator runbook; build-pipeline.md
                      covers the local build environment
```

## Quickstart

```sh
./scripts/host-setup.sh             # one-time: podman VM + builder images
./scripts/build-all.sh noble-6.8 generic-64k noble arm64  # GB200 target
./scripts/build-all.sh noble-6.8 generic noble amd64      # x86_64 parity target
                                    # END-TO-END for one base/arch:
                                    #   record -> kernel debs -> image gate ->
                                    #   DOCA + nvidia-open modules -> apt suite
python3 -m http.server -d /Volumes/Linux/repo  # serve the apt repo
#   node side: deb [trusted=yes] http://<host>:8000 noble-6.8 main
```

Daily life:

```sh
python3 tools/treadmill/detect.py   # any base stale vs the archive?
./scripts/rebase-base.sh <base>     # move a stack to the published tag
./scripts/backport.sh <sha>...      # carry an upstream fix on all bases
./scripts/build-kernel.sh /Volumes/Linux/build/noble-6.8 generic  # iterate
./scripts/boot-test.sh build/out/<base>/<arch>/<run-id>/linux-image-*.deb # 4k smoke
python3 tools/dashboard/generate.py # refresh dashboard/index.html
./scripts/validate-apt-repo.sh /Volumes/Linux/repo noble # apt repo smoke
```

See [docs/workflows.md](/Volumes/Linux/gb200/docs/workflows.md) for the
step-by-step runbooks: adding/removing patches, adding a new tracked kernel
base, SRU treadmill updates, module pin updates, and manual releases.

Dashboard notes: the default view is fast and uses exact upstream refs plus
the latest Ubuntu range for drop-readiness. Add `--deep-git-checks` when you
want slower reverse-apply content checks, and `--offline` when Launchpad/OSV
network access is unavailable.

`make check` runs syntax/YAML checks, workflow hardening checks, dashboard
config/render checks, dashboard regression tests, fake-deb script fixtures,
and patch-series apply validation. Release builds also write
`gb200-provenance.json` beside the debs, including per-module ABI metadata
captured from the built `.ko` files. `publish-repo.sh` copies provenance into
`provenance/<base>/<arch>/<run-id>.json` in the apt repo tree, and
`publish.yml` validates the temporary repo before `rsync` makes it live.
`arm64` is the default arch for legacy commands; `x86_64` is accepted as an
alias for Debian `amd64`.

In CI the same loop is: update-and-rebase.yml (daily detect->rebase->PR) →
kernel-ci.yml (PR gate: packaging+PE, module matrix, virtme-ng+kselftests
at 4k/64k, patchscan) → publish.yml (merge to main: release rebuild,
temporary apt repo validation, release summary, live repo sync). Use
`./scripts/tag-release.sh <tag> <artifact-root>` to create signed release tags
with notes generated from provenance.

Source trees (separate clones, not in this repo):

- `/Volumes/Linux/noble` — lp:~ubuntu-kernel/ubuntu/+source/linux/+git/noble
- `/Volumes/Linux/NV-Kernels` — github.com/NVIDIA/NV-Kernels (pinned patch
  source / reference only; force-updated upstream, never a base)
