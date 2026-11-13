# 7.3-LTS unified kernel: design and implementation plan

This is the design rationale. For how to operate what exists, see
[workflows.md](workflows.md).

## Status (2026-11-12)

| Area | State |
|---|---|
| `kpm`: fetch, harvest, resolve, decide, import, export, verify, apply, rebase, add, config, packaging | implemented; unit tests plus an end-to-end test on synthetic repos |
| Ubuntu, NVIDIA 7.3, GCP, AWS, Azure bands | imported: 548 patches on `v7.3.4`, no content conflicts |
| Packaging for amd64 + arm64, vendor config union, local overrides | implemented; config settles on resolute |
| amd64 build on resolute | base + Ubuntu band built and booted; full series with tools in progress |
| hwe-6.17 and hwe-7.0 | migrated to kpm lines; old per-base scripts removed |
| NVIDIA BOS forward-port (section 10, phase 4) | audited, none carried: of 871 commits, 139 remain deferred (66 absent from 7.3, 37 need review, 36 reverts). The absent set is CXL reset + vfio-cxl, which is written against a Type-2 API that upstream replaced |
| arm64 builds, noble builds, out-of-tree modules on 7.3 | not started |
| Companion userspace packages, metapackage, signing | not started |
| CI build and publish of 7.3-lts | not started; CI only verifies its ledger and series |

Where the implementation differs from the text below:

- Per-line settings live in `kernel/lines/<line>/line.toml`; there is no
  separate `base` file, and `kernel/patches/` is gone.
- Patch headers are `Ledger-Change:` / `Ledger-Origin:` trailers in the commit
  message. `Upstream:` and `Conflicts:` headers are not generated.
- Config layering is `config/union.json` plus `config/local.toml`, not an
  `annotations.d/` directory per band.
- The "missing `Fixes:`" check is `kpm verify --fixes <ref>`.
- Fuzzy (per-file similarity) matching is not implemented; the weakest
  evidence used is an identical subject, and only as a proposal.

## 1. Goal

One kernel source, built as Ubuntu debs for 24.04 (noble) and 26.04
(resolute), on amd64, arm64-4k and arm64-64k, that runs on GCP, AWS, Azure and
our own bare metal (GB200 and x86 fleet).

- **Base**: upstream stable `linux-7.3.y` (currently `v7.3.4`).
- **Overlay**: a patch-file series grouped in numbered bands by source:
  Ubuntu, NVIDIA (incl. BOS), GCP, AWS, Azure, ours.
- **Ledger**: every commit in every source tree is accounted for by content,
  not SHA: carried, already in base, duplicate, deferred, or dropped with a
  reason.
- **Agent-operable**: rebasing to `v7.3.N+1`, resyncing a vendor, or adding a
  new source (e.g. Google COS) is a runbook an AI agent can execute with
  deterministic tools and hard verification gates.

This inverts the current repo, which is "Ubuntu tag + 1–6 patches". The new
shape is "stable tag + roughly 700–1100 patches", so hand management stops
working and the ledger becomes the core of the project.

## 2. What the trees look like today

Measured in `O:\trees` on 2026-11-12.

| Source | Ref | Base | Delta | Notes |
|---|---|---|---|---|
| linux-stable | `v7.3.4` | `v7.3` | 1846 commits | Our base. |
| Ubuntu (stonking) | `Ubuntu-7.3.0-11.12` | `v7.3` | 431 non-merge | 249 SAUCE, ~60 `[Config]`/`[Packaging]`, ~100 release bookkeeping, ~20 upstream cherry-picks. Not yet merged with 7.3.y. |
| NVIDIA 7.3 | `26.04_linux-nvidia-7.3-next` | `v7.3` | 740 non-merge | Contains the Ubuntu delta; NVIDIA's own part is ~310 (239 `NVIDIA: SAUCE:`, 35 `NVIDIA: VR: SAUCE:`, rest cherry-picks). Has `debian.nvidia-7.3`. |
| NVIDIA BOS | `26.04_linux-nvidia-bos` | **7.0** | ~270 subjects beyond linux-nvidia 7.0 | No 7.3 BOS branch exists. By subject match, at most ~366 BOS changes are in neither `v7.3` nor NVIDIA's 7.3 branch; realistic forward-port set is 200–350 (apparmor 5, cxl, vfio, sched/steal-governor, PCI, MPAM, tegra). |
| linux-azure | `import/7.3.0-1002.2` | — | unusable | **Wrong repo**: this is the git-ubuntu importer (one squashed commit per upload, 54 commits). Need `~canonical-kernel/ubuntu/+source/linux-azure/+git/stonking`. |
| linux-gcp | `Ubuntu-gcp-7.3.0-1005.5` | `Ubuntu-7.3.0-11.12` | 41 non-merge | **No kernel patches**: 14 packaging, 5 config, rest bookkeeping. The GCP band is config-only today. |
| linux-aws | `Ubuntu-aws-7.3.0-1005.5` | `Ubuntu-7.3.0-11.12` | 59 non-merge | 23 SAUCE (Xen/hibernation), 7 packaging, 5 config. |

The GCP and AWS trees do not carry the parent `Ubuntu-7.3.0-*` tags; the
parent is found from the `UBUNTU: Ubuntu-7.3.0-N.M` release commit in their
history. The unified repo (section 5) removes the need for that.

Better trees, verified to exist on Launchpad and being cloned bare into
`O:\trees`:

| Clone | Repo | Why |
|---|---|---|
| `linux-azure-stonking.git` | `~canonical-kernel/ubuntu/+source/linux-azure/+git/stonking` | Per-commit Azure 7.3 history, `Ubuntu-azure-7.3.0-*` tags. Replaces the importer clone. |
| `linux-nvidia-stonking.git` | `~canonical-kernel/ubuntu/+source/linux-nvidia/+git/stonking` | Native 7.3 linux-nvidia with `Ubuntu-nvidia-7.3.0-*` release tags; a cleaner NVIDIA source than the force-updated GitHub `-next` branch. |
| `linux-nvidia-resolute.git` | `~canonical-kernel/ubuntu/+source/linux-nvidia/+git/resolute` | `nvidia-bos-next` and `nvidia-7.3-next` with release tags: the tagged form of BOS. |

Canonical's `azure-7.3-next` (in linux-azure `+git/resolute`) is a 26.04
backport of the stonking tree; it is not cloned, on the unchecked assumption
that it carries no kernel patches beyond stonking's.

**Trial result (2026-11-12, WSL resolute).** Rebasing Ubuntu's 431-commit
delta from `v7.3` onto `v7.3.4` left 388 commits. All 43 that fell out are
upstream fixes Ubuntu had cherry-picked that are already in 7.3.1–7.3.4: 39
dropped automatically by patch-id, and 4 stopped as conflicts only because
stable carries a slightly different version (3 io_uring fixes and the
`Linux 7.3.1` version bump). No SAUCE, config or packaging commit conflicted.

Three facts shape the design:

1. **Canonical already did the 7.3 port for everything except BOS.** Ubuntu
   26.10 ships 7.3, so the Ubuntu, GCP, AWS, Azure and NVIDIA deltas are
   relative to `v7.3` and should apply to `v7.3.4` with few conflicts.
2. **Derivatives are layered.** GCP/AWS/Azure/NVIDIA trees are rebased on the
   Ubuntu master tag, so each vendor's own delta is
   `Ubuntu-7.3.0-N..Ubuntu-<vendor>-7.3.0-M`. The Ubuntu band is a real
   prerequisite of every other band.
3. **BOS is a forward-port, not a harvest.** The same machinery (a source whose
   base differs from ours) is what a Google COS source would need, so it is
   built once as a general mode.

Caveat on the LTS status: kernel.org does not list 7.3 as longterm yet. The
public record is Greg KH saying he will count on 7.3.y as the next LTS. No EOL
is published; December 2029 is a guess by analogy with 6.18.

## 3. Prior art and what we take from it

| Project | Model | What we copy |
|---|---|---|
| SUSE kernel-source | `series.conf` + `patches.suse/`, per-patch `Git-commit:` / `Patch-mainline:` / `References:` headers, `blacklist.conf`, generated expanded branch | Mandatory per-patch headers; a reasons file for commits deliberately not taken; an expanded git branch generated from the series. Closest overall match. |
| ChromiumOS | Subject prefixes plus a rebase database (sha, patch-id, subject, topic, disposition, upstream sha) that drives every rebase; `findmissing` robot chases `Fixes:` | **Disposition as data.** This is the ledger. |
| OpenWrt | `backport-X.Y/`, `pending-X.Y/`, `hack-X.Y/` with numeric ranges; landed-in version in backport filenames | Numbered bands per source; recording the upstream version a patch lands in so drops are mechanical. |
| Ubuntu | `UBUNTU: SAUCE:`, `(cherry picked from commit X)`, `(backported from commit X)`, `BugLink:`, annotations for config, derivatives rebased on master | The trailers are our primary harvest key. Annotations with `include` carry the config overlay. |
| CentOS Stream | `commit <sha>` plus a free-text `Conflicts:` section on backports | `Conflicts:` notes on every adapted patch: the context an agent needs next time. |
| linux-stable | `stable-queue.git` is a quilt series; `commit X upstream.` identity line | Confirms the patch-file approach scales to thousands of patches. |
| Fedora ARK, Android ACK | Long-lived branch that merges upstream instead of rebasing | Rejected: merge history hides per-patch state, which is exactly what we need to reconcile across six sources. |
| Debian | DEP-3 headers, `gbp pq` branch/series round trip | The branch-is-workbench, series-is-record round trip we already use. |
| Yocto kernel-cache | `.scc` features bind patches and config fragments | A patch and its config change travel together (section 7). |

Generic tools we build on rather than replace: `git patch-id --stable`,
`git range-diff`, `git rerere`, `git merge-tree --write-tree` and `git replay`
(worktree-less cherry-pick and rebase).

Git-native `change-id` is not merged upstream, so we use our own trailer.

## 4. Repository layout

```
kernel/
  sources.toml                 registry of source trees (section 5)
  lines/7.3-lts/
    base                       one line: v7.3.4
    series                     ordered list of patch paths (authoritative order)
    patches/
      0000-ubuntu/             0xxx  Ubuntu SAUCE and tree-touching packaging
      2000-nvidia/             20xx-24xx  linux-nvidia 7.3 delta
      2500-nvidia-bos/         25xx-29xx  BOS forward-ports
      3000-gcp/
      4000-aws/
      5000-azure/
      6000-local/            ours: hardware fixes, backports outside LTS scope
      9000-temp/               CVE/hotfix carries awaiting a stable release
    ledger/
      ubuntu.jsonl             one row per source commit (section 6)
      nvidia.jsonl  nvidia-bos.jsonl  gcp.jsonl  aws.jsonl  azure.jsonl
      local.jsonl
      decisions.md             human/agent rationale for non-obvious calls
    packaging/
      debian.kernops/          our flavour dir (section 7)
      annotations.d/           config overlay per band
tools/kpm/                     the patch manager (section 8)
docs/runbooks/                 agent runbooks (section 9)
AGENTS.md                      entry point for Claude/Codex
```

The older Ubuntu-based kernels, `hwe-6.17` and `hwe-7.0`, are lines of the same
kind (base = an Ubuntu release tag, patches = our own backports) and keep
building alongside 7.3-lts. The `noble-6.8` base is retired.

### Numbering

Your band scheme, with three adjustments:

- **Order lives in `series`, not in filenames.** Files are named
  `NNNN-slug.patch` and numbered by the tool within their band, but identity is
  the ledger ID in the patch header. Renumbering after an insert or drop is a
  pure rename.
- **NVIDIA is split at 2500** so BOS forward-ports stay separable and can be
  swapped wholesale when NVIDIA publishes a 7.3 BOS branch.
- **1xxx is reserved** (Ubuntu overflow), and **9xxx is temporary carries**,
  applied last so they never become a prerequisite of anything.

Bands apply in numeric order. An upstream cherry-pick that several vendors
carry lives once, in the lowest band that claims it; the ledger records every
claimant. If Ubuntu later drops it while NVIDIA still carries it, reconcile
moves it to 2xxx instead of losing it.

Capacity today: Ubuntu ~330, NVIDIA ~310, BOS up to ~350. If a band outgrows
1000 we widen to five digits; nothing depends on the width.

## 5. Source registry

`sources.toml` is the only place a tree is described. Sketch:

```toml
[stable]
url = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
role = "base"
track = "tags:v7.3.*"

[ubuntu]
band = 0
url = "https://git.launchpad.net/~ubuntu-kernel/ubuntu/+source/linux/+git/stonking"
track = "tags:Ubuntu-7.3.0-*"
delta = "v7.3..{ref}"
noise = ["UBUNTU: Ubuntu-", "UBUNTU: Start new release", "UBUNTU: link-to-tracker"]
route = { "UBUNTU: [Config]" = "config", "UBUNTU: [Packaging]" = "packaging" }

[azure]
band = 5000
parent = "ubuntu"
url = "https://git.launchpad.net/~canonical-kernel/ubuntu/+source/linux-azure/+git/stonking"
track = "tags:Ubuntu-azure-7.3.0-*"
delta = "{parent_tag}..{ref}"

[nvidia-bos]
band = 2500
parent = "nvidia"
url = "https://github.com/NVIDIA/NV-Kernels"
track = "branch:26.04_linux-nvidia-bos"     # force-updated: pin SHAs
delta = "v7.0..{ref}"
mode = "forward-port"                        # source base != our base
```

Adding Google COS later is one stanza (`band = 3500`, `mode = "forward-port"`,
its `cos-X.Y` branch) plus `kpm harvest cos`. Identity matching resolves
everything already present; only the remainder needs triage.

All trees are fetched as remotes into **one bare repository** with namespaced
tags (`refs/tags/<source>/*`), because NV-Kernels and the Launchpad trees
publish colliding tag names. One object store makes cross-tree comparison
cheap, and a bare repo sidesteps the case-insensitive filesystem: harvest,
fingerprinting, `git apply --cached`, `merge-tree` and `replay` never check
files out. Anything that needs a worktree (conflict resolution, builds) runs
on Linux: WSL2 on ext4 locally, or CI.

## 6. Patch identity and the ledger

### Fingerprints

Every commit seen in any tree gets all of these, strongest first:

1. **Upstream reference** parsed from the message: `(cherry picked from commit
   X)`, `(backported from commit X)`, `commit X upstream.`, `[ Upstream commit
   X ]`, including the tree name when it is not mainline.
2. **Lore identity**: `Link:` / Message-Id, and `BugLink:` as a grouping hint.
3. **`patch-id`**: `git patch-id --stable`. Exact diff modulo line numbers.
4. **`delta-id`**: hash of the added and removed lines only, per file,
   whitespace-normalised, context dropped. Survives context drift, which is
   what breaks patch-id on most backports. Home-grown; no standard tool.
5. **Per-file delta hashes**, for a similarity score that separates
   "identical", "adapted backport" and "partial".
6. **Normalised subject + author + author date**, with vendor prefixes
   stripped. A tiebreak, never sufficient alone.

Commits that match at levels 1–4 are merged automatically into one **change**
(a union-find cluster across all trees). Matches at levels 5–6 are only
*proposed*; a human or agent confirms, and the confirmation is stored so the
question is never asked twice.

One limit to be honest about: a SAUCE patch that later lands upstream
rewritten shares no content with its predecessor. Only a `Link:` or a judgment
call connects them. Those are recorded in `decisions.md`.

### Ledger rows

One JSON line per source commit, in the source's topological order, so
diffs in PRs read like the vendor's own log:

```json
{"change":"C-00412","sha":"3fa9c1…","subject":"iommu/tegra241-cmdqv: Fix CMD_SYNC use-after-free on teardown",
 "patch_id":"…","delta_id":"…","upstream":"9d1e…","first_seen":"26.04_linux-nvidia-bos@f1979a96",
 "disposition":"carry","patch":"2500-nvidia-bos/2514-iommu-tegra241-cmdqv-fix-cmd-sync-uaf.patch",
 "adapted":true}
```

Dispositions:

| Disposition | Meaning | Required field |
|---|---|---|
| `carry` | In the series | `patch` |
| `in-base` | Content already in `v7.3.N` | `base_sha`, `landed` (e.g. `v7.3.3`) |
| `duplicate` | Same change carried via another source | `change` shared with a `carry` row |
| `superseded` | Replaced by a different change | `by` |
| `deferred` | Wanted, not yet ported | `reason` |
| `dropped` | Deliberately not taken | `reason` |
| `noise` / `config` / `packaging` | Release bookkeeping, or routed to section 7 | — |
| `undecided` | New, untriaged | — |

Carried patch files repeat the essentials in their header so each file is
self-describing: `Ledger-Change:`, `Ledger-Origin: <source>@<sha>`,
`Upstream:` (sha and landed-in version, or `never` with a reason), and a
`Conflicts:` note whenever `adapted` is true.

### Invariants (`kpm verify`, required in CI)

- The series applies cleanly to `base`.
- Every commit in every source delta at its pinned ref has exactly one row,
  and no row is `undecided` on `main`.
- Every patch file has a `carry` row and vice versa; the file's recomputed
  fingerprints match the row unless `adapted` is set.
- No `carry` patch has a fingerprint present in `base` (it should have been
  dropped).
- No carried upstream commit has an unapplied upstream `Fixes:` follow-up
  (`kpm verify --fixes`).

Fingerprint indexes of `v7.3..v7.3.N` and of mainline since `v7.3` are a
rebuildable local cache (SQLite, gitignored), not part of the record.

## 7. Packaging and config

Vanilla stable has no `debian/`. Ubuntu's packaging dirs are not tracked as
patches; they are churn, and they are ours to own.

- `packaging/debian.kernops/` is derived from `debian.master` at the pinned
  Ubuntu tag and overlaid at build time with Ubuntu's `debian/`. Resyncing it
  is a three-way merge against the new Ubuntu tag (vendor-branch pattern).
- **Flavours** mirror `debian.nvidia-bos` exactly: `amd64-kernops`,
  `arm64-kernops` (4k), `arm64-kernops-64k`.
- **Config** is an annotations file that includes `debian.master`'s, then
  `annotations.d/2000-nvidia`, `3000-gcp`, `4000-aws`, `5000-azure`,
  `6000-local` in band order. `kpm config reconcile` generates the vendor
  fragments from each derivative's annotations and reports conflicts. Default
  rule for a unified kernel is union (`y` beats `m` beats `n`), because the
  cloud flavours disable options for size that another cloud or bare metal
  needs; every conflict is listed for review and can be pinned in
  `6000-local`.
- A `[Config]` commit that enables code added by a patch is linked to that
  change in the ledger, so dropping the patch drops the option.
- **Two series from one source.** Same tree, same series; only the changelog
  suffix (`~24.04.1` / `~26.04.1`) and the builder image differ. 26.04 has
  precedent: Canonical backports the stonking kernel there
  (`debian.nvidia-7.3`, `azure-7.3-next`). 24.04 has no 7.3 build, but it
  has a 7.0 one (`debian.nvidia-bos-7.0` on `24.04_linux-nvidia-bos-7.0-next`),
  and its delta from the resolute 7.0 packaging is the template for our noble
  overlay: `gcc-13`, `clang-18`, `rustc-1.80` / `bindgen-0.65` pins,
  `libstdc++-13-dev`, unversioned `pahole` (1.25), `pkg-config`,
  `do_lib_rust = false` on arm64, and `CONFIG_RUST` off in annotations.
  Between v7.0 and v7.3.4 the kernel's minimums moved only for LLVM (noble's
  clang 18 is at the floor), rustc (1.78 → 1.85) and bindgen (0.65 → 0.71);
  gcc (12) and pahole are unchanged. Since Rust is already off on noble, the
  expected 7.3 delta is small. One thing to check: that noble 7.0 packaging
  builds arm64 as `Image.gz`, not `vmlinuz.efi`, which is the LP #2098111
  PE-boot issue this repo already gates on.
- Cloud NIC drivers (ENA, gVNIC, EFA, MANA, idpf) are in-tree upstream and
  Canonical's 7.3 cloud kernels use them as-is; the only vendor change is one
  AWS ENA SAUCE patch (NUMA-aware interrupt allocation). If we ever need
  features that exist only in Amazon's or Google's out-of-tree driver
  releases, they go in `modules/matrix.tsv` as DKMS-style modules, not in the
  patch series.
- **Per-series packaging differences** live in a small overlay,
  `packaging/series/{noble,resolute}/`, applied over the shared
  `debian.kernops/` when the source package is generated. Canonical does the
  same thing with a whole second directory (`debian.nvidia-7.3` selected by
  `debian/debian.env`); an overlay avoids two copies drifting. It holds only:
  changelog suffix and distribution, `Build-Depends` substitutions
  (the 7.3 packaging asks for `clang-22`, `llvm-22-tools`, `rustc >= 1.82`,
  `bindgen`, `pahole >= 1.29`), `rules.d` toggles (`do_tools_*`, Rust), and an
  annotations fragment for compiler-dependent config. The kernel tree and
  patch series are never series-specific.
- Out-of-tree modules (`modules/matrix.tsv`) gain a `7.3-lts` base. DOCA and
  nvidia-open compatibility with 7.3 must be confirmed; not checked yet.

### Identity: name and version are configuration

Nothing branded is hardcoded. One file, `kernel/identity.toml`, is the only
place the name appears; `kernops` throughout this document is just its default
value.

```toml
vendor         = "kernops"            # e.g. "mycompany"
arches         = ["amd64", "arm64"]   # armhf, ppc64el, riscv64, s390x are dropped
source_package = "linux-{vendor}"     # also names linux-meta-{vendor}
flavour        = "{vendor}"           # gives {vendor} and {vendor}-64k
packaging_dir  = "debian.{vendor}"
version        = "{kver}-{abi}.{upload}~{series}.{respin}"
kver           = "ubuntu"             # "ubuntu" = 7.3.0, "stable" = 7.3.4
maintainer     = "Kernel Team <kernel@example.com>"
```

- The packaging directory in the repo is a template (`packaging/debian.tmpl/`
  with `@VENDOR@`-style placeholders). The concrete `debian.<vendor>/`,
  control files, flavour names, metapackage names and changelog entries are
  rendered at source-package generation time.
- **Patch files and the ledger never contain the brand.** Headers are
  `Ledger-Change:` / `Ledger-Origin:`, IDs are `C-NNNNN`, and our own band is
  `6000-local/`. Renaming the vendor therefore touches one file and no
  patches.
- Module packages, the userspace matrix, provenance and the apt repo layout
  read the same file.
- A rename after release is a new package lineage: the meta package needs
  `Replaces:`/`Provides:` for the old names. The tool can generate those from
  a `previous_vendors` list.

### Tools packages built from the kernel source

The cloud flavours' `linux-tools-*` and `linux-cloud-tools-*` packages are not
separate projects: they are built from `tools/` in the kernel tree by the same
`debian/rules`, switched per flavour and architecture by `do_tools_*` flags in
`rules.d/<arch>.mk`. What each 7.3 tree enables:

| Flag | Produces | master | aws | gcp | azure |
|---|---|---|---|---|---|
| `do_tools_perf`, `cpupower`, `usbip` | `linux-tools-*`, `linux-perf` | both | both | both | both |
| `do_tools_x86` | turbostat, x86_energy_perf_policy | amd64 | amd64 | amd64 | amd64 |
| `do_tools_bpftool` | `bpftool` | both | off | off | amd64 |
| `do_tools_hyperv` | `linux-cloud-tools-*`: `hv_kvp_daemon`, `hv_vss_daemon`, `hv_fcopy_uio_daemon`, `lsvmbus` | amd64 | arm64 only | off | both |
| `do_tools_rtla`, `acpidbg`, perf jvmti/python | extra tools | mostly amd64 | partial | off | amd64 |
| `do_zfs` | ZFS modules built with the kernel | off | both | both | arm64 |
| `do_dkms_nvidia*` | prebuilt NVIDIA modules | off | off | amd64 | off |

For the unified kernel, `debian.kernops/rules.d` takes the **union**: every
tool on, on every architecture where it builds, including the Hyper-V daemons
on both amd64 and arm64. One `linux-tools` and one `linux-cloud-tools` set then
serves all three clouds and bare metal.

Three follow-ons:

- **Metapackages.** Cloud images and agents depend on names like
  `linux-tools-azure` or `linux-cloud-tools-aws` from the separate
  `linux-meta-*` sources. We ship our own `linux-meta-kernops` whose packages
  `Provides:` those names, so an image built for a given cloud installs
  cleanly. Which names are actually depended on needs checking per cloud
  image.
- **ZFS is not supported.** `do_zfs = false` everywhere.
- **NVIDIA modules** stay in `modules/matrix.tsv` as today, not `do_dkms_*`.

### Prebuilt out-of-tree modules

`modules/matrix.tsv` grows from two modules on three bases to a larger set on
one base, built for every kernel we publish: DOCA (mlnx-ofed), nvidia-open,
nvidia-fs, and others as needed.

- **Matrix**: module × series (noble, resolute) × flavour build
  (amd64, arm64-4k, arm64-64k). Six kernel builds, each with its own
  version-locked, signed module debs (the existing LRM-style pattern).
- **Build order is a dependency graph, not a list.** DOCA first; nvidia-open
  after it (`nvidia_peermem` links against OFED's `Module.symvers`); nvidia-fs
  after both. The matrix gains a `needs` column and the build script sorts on
  it.
- **Gate**: a kernel release is not published unless every module in the
  matrix builds and loads against it on all six builds. A stable rebase that
  breaks a module is caught in the rebase PR, not after publish.
- **Version pins move independently of the kernel**, through the same
  detector-and-PR flow as kernel sources.

### Config reduction (after the first release)

The first release uses the union config so nothing is missing on any platform.
Once builds are routine, the config shrinks to what we run:

- Inputs: loaded-module and device inventories from each cloud's instance
  types and from our bare metal, plus the module matrix.
- Each removal is a `6000-local` annotation with a note saying why; the
  boot and module gates on every platform are the safety net.
- Shrinking the config also shrinks the series. A patch that only touches code
  we no longer build (laptop device trees, camera drivers, Xen hibernation if
  we drop Xen instance types) moves to `dropped` with reason `config-off`, and
  stops costing rebase effort.

Guest agents (walinuxagent, google-guest-agent, amazon-ssm-agent and similar)
are not part of the kernel source packages and are out of scope here.

### Companion userspace packages

Some userspace is coupled to the kernel version and the series' stock version
will not understand 7.3. We build and publish these ourselves, for both noble
and resolute, in the same apt suites as the kernel.

| Package | Why it is kernel-coupled | Needed at |
|---|---|---|
| pahole (dwarves) | Generates BTF during the kernel build | build time, before the kernel |
| makedumpfile, kdump-tools, crash | Must know the running kernel's structures and layout | runtime (crash capture and analysis) |
| kexec-tools | Must load our arm64 image: an EFI zboot `vmlinuz.efi` with a **zstd** payload (`CONFIG_KERNEL_ZSTD`). noble's 2.0.28 zboot loader only handles gzip and lzma, so noble needs a newer build | runtime (kexec, kdump) |
| bpftrace, bcc, libbpf | Depend on kernel BTF, helpers and program types | runtime |
| linux-firmware | New drivers ask for firmware the older series does not ship | runtime; mainly a noble concern |
| bpftool, perf, cpupower | Built from the kernel source as `linux-tools` | already produced by the kernel build |

Mechanism, mirroring `modules/matrix.tsv`:

- `userspace/matrix.tsv` pins, per target series: package, the Ubuntu series
  whose source package we take (normally the newest, e.g. stonking), version,
  and an optional patch directory. The default is a no-change backport rebuild
  with a `~24.04.1+ko1` style suffix; original packaging work is the exception.
- Built in the existing per-series builder containers, published with the
  kernel, recorded in provenance.
- **Build order**: pahole is stage 0. It is built first and installed into the
  builder image, so both series generate BTF with the same pahole and
  `CONFIG_PAHOLE_VERSION` stops being a per-series config difference.
- **Version choice is constrained on noble.** A newer bpftrace or bcc may want
  a newer LLVM than noble's clang 18; in that case we pin the newest release
  that builds against noble's LLVM instead of backporting a toolchain.
- **Tests** in CI: kernel BTF is present and loadable; a bpftrace kprobe
  one-liner runs; a forced panic under QEMU produces a vmcore that
  makedumpfile filters and crash opens, at 4k and 64k.
- The treadmill detector watches the source series for new uploads of pinned
  packages, the same way it watches kernel publications today.

Not verified yet: which versions each series ships, and which of them already
work with 7.3 unchanged. Anything that does is left alone.

## 8. Tooling: `kpm`

A single Python package in `tools/kpm`, replacing the hardcoded
`$WORK_ROOT` paths with `KERNOPS_TREES` / `KERNOPS_BUILD`. Every command
has `--json` output and a non-zero exit on anything needing judgment. Existing
per-base scripts and the vendored patchscan tool are replaced by it.

| Command | Does |
|---|---|
| `kpm fetch` | Update the unified bare repo for the line's sources. |
| `kpm harvest` | Enumerate each vendor delta at its newest ref, fingerprint, update the ledger. Reports new, vanished and re-identified commits. |
| `kpm resolve` | Decide rows by identity (`in-base`, `duplicate`), exact revert pairs and policy rules; leave the rest `undecided`. |
| `kpm decide` | Set a disposition by change ID, SHA or selector. |
| `kpm add <sha>` | Carry a commit of our own choosing in the local band. |
| `kpm import` / `kpm export` | Ledger → workbench branch, and branch → patch files + series. |
| `kpm apply <dir>` | Series → plain source tree for a build. |
| `kpm rebase <tag>` | Move the line to a new base tag: mark what landed as `in-base`, rebuild, re-export, write a range-diff; stop on conflict. |
| `kpm verify` | The invariants above; `--fixes` adds the missing-follow-up check. |
| `kpm why` / `kpm status` / `kpm lines` | Reporting. |
| `kpm config` / `kpm packaging` | Section 7. |

The workbench stays a git branch (`kernops/7.3-lts`), pushed and tagged per
release as the expanded tree. The series plus ledger remain the record: CI
builds only from them.

## 9. The agent flow

The point of sections 5–8 is that an agent's job reduces to judgment calls
with a checkable answer.

- `AGENTS.md` at the repo root states the invariants, the trailers, and the
  rule that the agent never adds `Signed-off-by` (matching the kernel's own
  `Documentation/process/coding-assistants.rst`).
- Runbooks in `docs/runbooks/`: `rebase-stable.md`, `resync-source.md`,
  `add-source.md`, `forward-port.md`, `triage.md`, `cve-carry.md`.
- **Conflict resolution** follows the LLMinus pattern: retrieve similar past
  resolutions (our `rerere` cache, `Conflicts:` notes, and how the same
  upstream commit was backported in 7.3.y or a vendor tree), resolve to the
  upstream end state, then prove it with a build of the touched objects on all
  three flavours. Every adapted patch gets a `Conflicts:` note and
  `adapted: true`.
- **Guardrails**: `dropped` and `deferred` need a reason; fuzzy identity
  matches need a recorded decision; a rebase PR always carries the
  `range-diff` and the list of auto-dropped patches with the base SHA that
  replaced each one.
- **Gates** before merge: `kpm verify`, three-flavour build, virtme-ng boot
  plus the existing mm/dma/iommu/locking kselftests at 4k and 64k.

Example prompts this is designed to make routine: "rebase 7.3-lts to v7.3.6",
"Ubuntu published 7.3.0-14, resync", "add COS `cos-6.12` as a GCP source in
3500 and tell me what it has that we lack", "is the fix for CVE-2026-NNNNN in
our kernel?".

## 10. Implementation plan

Each phase ends in something verifiable.

| Phase | Work | Exit check |
|---|---|---|
| **0. Foundations** | Retire the 6.8 base; `kernel/identity.toml`; Unified bare repo; re-clone linux-azure from the per-commit repo; rename the AWS clone; path config; `kpm` skeleton, `sources.toml`, fingerprint library with tests (use Ubuntu↔NVIDIA-7.3 overlap as ground truth: ~431 commits must cluster). | `kpm harvest ubuntu nvidia` produces ledgers; overlap is found automatically. |
| **1. Ubuntu band + packaging** | Resolve Ubuntu's delta against `v7.3.4`; import SAUCE into 0xxx; build `debian.kernops`; resolute builder first, then **noble toolchain spike**. | Three flavours build on resolute and boot under virtme-ng; noble result known. |
| **1b. Userspace** | `userspace/matrix.tsv`; pahole as stage 0 in both builder images; then makedumpfile/kdump/crash, bpftrace/bcc/libbpf, linux-firmware for both series. | BTF, bpftrace and kdump-to-crash tests pass on both series, 4k and 64k. |
| **2. NVIDIA 7.3 band** | Import ~310 into 20xx; module matrix for 7.3 with dependency ordering (DOCA, nvidia-open, nvidia-fs). | GB200 boot on 64k; every matrix module builds, signs and loads on all six builds. |
| **3. Cloud bands** | Harvest GCP, AWS, Azure; import; `kpm config reconcile`. | One image boots on each cloud, on both architectures where offered. |
| **4. BOS forward-port** | Triage the 200–350 candidates by topic (sched/steal-governor, cxl, vfio, MPAM, tegra241, PCI, apparmor); port topic by topic into 25xx. | Each topic builds and passes its kselftests; ledger has no `undecided` BOS rows. |
| **5. Treadmill** | Stable-tag detector (`git ls-remote`), per-source resync, rebase PR automation, dashboard on the ledger, publish to the apt repo for both series. | A real `v7.3.N → N+1` rebase lands as an agent-authored PR with no manual ledger edits. |
| **5b. Config reduction** | Inventory what each platform loads; cut the union config down; drop patches for code no longer built. | Config and series are measurably smaller; every platform still boots and loads its modules. |
| **6. Prove extensibility** | Add Google COS as a source. | Report of what COS carries that we lack, produced by the runbook alone. |

Phases 2, 3 and 4 are independent once phase 1 lands. Phase 4 is the largest
and least predictable piece of work.

## 11. Decisions needed

Decided:

- **Ubuntu SAUCE**: take all of it first. Config reduction (section 7) decides
  what leaves later.
- **Names and versions** are configuration, not code (section 7, Identity).
- **Existing bases**: the 6.8 base is retired; keep `hwe-6.17` and `hwe-7.0`
  building.
- **ZFS**: not supported.

Still open:

1. **Cherry-pick placement.** Section 4 keeps a shared upstream pick in the
   lowest band that claims it. The alternative is a dedicated upstream-backport
   band, which is tidier but breaks "bands apply in numeric order" for Ubuntu's
   own picks.
2. **Where Linux work runs.** WSL2 on ext4 for local conflict resolution and
   builds, or CI only.
3. **Default version scheme**: Ubuntu's `7.3.0-ABI.UPLOAD` convention or the
   real stable sublevel. Either is a one-line config choice, but changing it
   after the first release changes `uname -r` and every module package name.

## 12. Risks

- **Noble toolchain vs 7.3** (section 7). Low: the noble 7.0 packaging is a
  working template and the 7.0 → 7.3 toolchain minimums barely moved. Confirmed
  by building in phase 1.
- **BOS forward-port size.** The 200–350 estimate is from subject matching, not
  content; it could move either way once fingerprinted.
- **Unified config conflicts.** Cloud flavours tune for their hypervisor;
  the union may cost boot time or image size on one of them.
- **Force-updated sources.** NV-Kernels branches are rebased; the ledger pins
  SHAs and treats a vanished commit as a signal to review, not to drop.
- **LTS status** is not yet on kernel.org.
