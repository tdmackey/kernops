# NV-Kernels automation inventory

Source: github.com/NVIDIA/NV-Kernels, orphan `github-actions` branch,
inventoried at commit `2c7c305c4360` (June 2026). Per the master plan this
repo is **reference/patch-source only** — force-updated, pin SHAs, never a
base. Verdict per component below.

## update-and-rebase.yml (142 lines) — ADAPT

Their daily rebase treadmill. Shape:

1. `prepare` job builds a matrix of (branch, newest upstream tag) — notably
   it queries tags with `git ls-remote` (no clone needed).
2. Per branch: skip if a `<branch>-<tag>` tag already exists (idempotency
   marker — simple and effective), else shallow-init, fetch, find merge-base,
   `git rebase --onto <new-tag> <merge-base>`, push a **tag** (not a branch;
   PR #449 stopped pushing to `-next` branches) and chain into kernel-build.
3. No conflict handling: a conflicting rebase just fails the job.

Differences for us:
- Their upstream is **Greg KH stable tags**; ours is **Ubuntu archive
  publications** — our detector must poll the Launchpad publishing API / USN
  OSV feed (master plan caveat: not cgit/Forgejo scraping), then map
  publication → `Ubuntu-*` tag → rebase `gb200/<base>`.
- We want the conflict path to *open an issue/PR with the state preserved*,
  not just fail.
- Their idempotency-by-tag trick is worth keeping verbatim.
- We attach `git range-diff` output to the result for review; they don't.

## kernel-build.yml (98 lines) — ADAPT (heavily)

Build+test matrix: x86 defconfig, arm64 4k, arm64 64k — on **GitHub-hosted
runners** including `ubuntu-24.04-arm`. Uses **virtme-ng** (`vng -vb` build,
`vng -- uname -a` boot test) and runs kselftests `mm dma iommu locking`
in-VM. Their config_4k/config_64k fragments live next to the workflows.

Takeaways:
- **virtme-ng is the right boot-test harness** for CI (boots the built tree
  directly, no image dance) — adopt for the kselftest gate on our runners.
  Note: hosted arm runners are KVM-less for 64k?? They run vng anyway —
  acceptable speed for smoke tests; our self-hosted Graviton has bare-metal
  KVM at 64k (and unlike laptop HVF, 64k guests work).
- They build with kconfig fragments, NOT Ubuntu packaging — fine for "does
  the kernel work", useless for "does the deb build". We need both: their
  vng+kselftest stage, plus our `fakeroot debian/rules` packaging stage.
- The kselftest target set (mm dma iommu locking) matches the master plan's
  gate; steal as-is. So does the `--summary` invocation.

## patchscan (174 lines, by a Canonical engineer) — ADOPT (vendored)

Vendored at `tools/patchscan/` (provenance: NV-Kernels `2c7c305c4360`,
`.github/scripts/patchscan`). Two checks over a commit range:

1. every `(cherry picked|backported) from commit X` / `Upstream commit X`
   trailer resolves to a real upstream commit with **matching subject and
   author**;
2. for each referenced upstream commit, greps upstream for later
   `Fixes: <sha8>` commits **not present in the range** — i.e. automatic
   "you carried the patch but missed its fix" detection. This is exactly the
   writeback-UAF class of omission we hit by hand; run it on every series
   change and every rebase.

Usage (in the noble clone, which has the `upstream` remote):

```sh
python3 tools/patchscan/patchscan 'Ubuntu-6.8.0-130.130..gb200/noble-6.8' upstream master
# --no-update to skip the fetch; needs: pip install gitpython colorama
```

Wire into CI as a required check on patch-branch PRs (their patchscan.yml,
433 lines, is mostly PR-comment plumbing around this script — reuse the
comment-posting idea, rewrite the plumbing for our repo).

## validate-pr (694 lines) — REFERENCE (trim later)

Full PR hygiene: trailer formats, `NVIDIA: SAUCE:` subject policing,
Signed-off-by chain verification against upstream, patch-id comparison,
BugLink requirements per target branch. Heavily NVIDIA/Canonical-convention
specific. When our flow becomes PR-driven, lift: trailer/provenance checks,
patch-id drift detection (catches silently-mangled backports). Skip: their
S-o-b chain policy (we're not submitting to Canonical), BugLink rules.

## What they DON'T have (we build ourselves)

- Archive-publication detection (Launchpad API / USN OSV) — theirs is
  upstream-tag-driven.
- Anything debian-packaging aware (deb build, ABI, changelog, versioning).
- range-diff review artifacts; conflict-state preservation.
- Signing, artifact publishing, image pipeline — all ours (master plan
  phases 1–3).

## The stable-LTS escape hatch track

Decision (June 2026): alongside the Ubuntu bases we maintain a
**`stable-6.18`** base (vanilla Greg KH LTS, the line NV-Kernels also
tracks) so that moving off Ubuntu kernels to a normal LTS stays a
live option, continuously proven rather than theoretical.

- **Delta is tiny** (verified against release tags): v6.18 GA needs only the
  tegra trio + possibly the writeback UAF fix (CC: stable, likely already in
  6.18.y). The writeback contention rework is already in v6.18.
- **Their update-and-rebase.yml applies near-verbatim** for this track —
  its trigger IS Greg KH stable tags (`git ls-remote` on gregkh/linux.git,
  idempotency tag, rebase --onto merge-base). Only branch names change.
- **No Ubuntu packaging on this track.** It builds with config fragments +
  virtme-ng (their kernel-build.yml model) — its job is "patches apply and
  the kernel boots/passes kselftests on every stable release", i.e. keeping
  the door open, not producing production debs. If we ever walk through the
  door, packaging becomes its own project (config provenance: start from
  Ubuntu's config, `make olddefconfig`).
- backport.sh treats it as just another base in upstream-base.txt once the
  stable remote is fetched (commands in that file, ~moderate one-time fetch).

## Adoption order

1. **patchscan locally** (done — vendored) and on every backport/rebase.
2. **Rebase workflow** for our bases: detector → rebase → patchscan +
   range-diff → build → PR. The skeleton of update-and-rebase.yml + our
   backport.sh/audit logic.
3. **kselftest gate** via virtme-ng on the Graviton runners (4k + 64k).
4. validate-pr trimmings when PR flow exists.
