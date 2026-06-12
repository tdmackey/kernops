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
./scripts/host-setup.sh          # one-time: podman VM + builder images
./scripts/build-kernel.sh /Volumes/Linux/noble generic        # build a kernel
./scripts/builder-shell.sh noble                              # poke around
```

Source trees (separate clones, not in this repo):

- `/Volumes/Linux/noble` — lp:~ubuntu-kernel/ubuntu/+source/linux/+git/noble
- `/Volumes/Linux/NV-Kernels` — github.com/NVIDIA/NV-Kernels (pinned patch
  source / reference only; force-updated upstream, never a base)
