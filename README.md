# gb200 — Ubuntu kernel fork & build pipeline

Thin patches-on-top-of-Ubuntu kernel fork tracking Canonical `linux-nvidia` /
`linux-nvidia-64k` (noble 6.8 → resolute 7.0) for GB200 (Grace arm64, 64k pages).
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
docs/                 build-pipeline.md is the operative doc
```

## Quickstart

```sh
./scripts/host-setup.sh             # one-time: podman VM + builder images
./scripts/build-all.sh noble-6.8 generic-64k   # END-TO-END for one base:
                                    #   record -> kernel debs -> PE gate ->
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
./scripts/boot-test.sh build/out/<base>/linux-image-*.deb          # 4k smoke
python3 tools/dashboard/generate.py # refresh dashboard/index.html
```

In CI the same loop is: update-and-rebase.yml (daily detect->rebase->PR) →
kernel-ci.yml (PR gate: packaging+PE, module matrix, virtme-ng+kselftests
at 4k/64k, patchscan) → publish.yml (merge to main: release rebuild + apt
repo sync).

Source trees (separate clones, not in this repo):

- `/Volumes/Linux/noble` — lp:~ubuntu-kernel/ubuntu/+source/linux/+git/noble
- `/Volumes/Linux/NV-Kernels` — github.com/NVIDIA/NV-Kernels (pinned patch
  source / reference only; force-updated upstream, never a base)
