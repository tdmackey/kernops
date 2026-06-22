# Maintainer workflows

This repo is the artifact of record for a small Ubuntu-kernel delta. The
kernel git worktrees are the editing surface; this monorepo records the result.

Important invariants:

- `kernel/upstream-base.txt` pins the exact Ubuntu tag each base applies to.
- `kernel/patches/gb200/<base>/` is generated with `git format-patch`.
- The branch `gb200/<base>` in the kernel clone is the human workbench.
- CI and release builds reconstruct from the monorepo, not from your local
  branch state.
- Every carried patch needs a drop condition. Upstream picks should use
  `cherry-pick -x`; adapted picks should say `backported from commit ...`.

Useful checks:

```sh
make check
python3 tools/dashboard/generate.py --offline
PUBLISH=0 ./scripts/build-all.sh <base> "" noble arm64
PUBLISH=0 ./scripts/build-all.sh <base> "" noble amd64
```

`build-all.sh` defaults to `arm64`. Passing an empty flavour lets the script
choose the target default (`generic-64k`/`nvidia-64k` on arm64,
`generic`/`nvidia` on amd64).

## Add An Upstream Patch

Use this for upstream mainline/stable fixes that we carry until Ubuntu SRU
pulls them in.

1. Audit the candidate.

   ```sh
   cd /Volumes/Linux/gb200
   ./scripts/backport.sh --dry-run <sha> [<prereq-sha>...]
   ```

   If the dry run reports missing prerequisites, add the minimal hard
   prerequisite chain in upstream chronological order. See
   [backporting.md](/Volumes/Linux/gb200/docs/backporting.md) for the deeper
   audit flow.

2. Apply across all bases.

   ```sh
   ./scripts/backport.sh <sha> [<prereq-sha>...]
   ```

   The script:

   - creates or reuses `/Volumes/Linux/build/<base>` worktrees
   - cherry-picks with `-x`
   - skips bases where the patch is already present
   - exports changed stacks back into `kernel/patches/gb200/<base>/`
   - regenerates the dashboard

   If a base conflicts, the script aborts that base and lists it under
   "need a human". Resolve that base manually using the process in
   [backporting.md](/Volumes/Linux/gb200/docs/backporting.md), then export.

3. Review the exported record.

   ```sh
   git diff -- kernel/upstream-base.txt kernel/patches/gb200 tools/dashboard
   ./scripts/apply-series.sh --check <changed-base>
   make check
   ```

4. Build-test affected targets.

   For a narrow compile-only check:

   ```sh
   ./scripts/build-kernel.sh /Volumes/Linux/build/<base> generic noble
   ```

   For the release-shaped path without publishing:

   ```sh
   PUBLISH=0 ./scripts/build-all.sh <base> "" noble arm64
   PUBLISH=0 ./scripts/build-all.sh <base> "" noble amd64
   ```

5. Commit the monorepo change.

   The commit message should include why we carry the patch, any prerequisites,
   and the drop condition, for example:

   ```text
   backport: carry <subsystem> fix for <bug/workload>

   Carries <sha> until it reaches <base> via Ubuntu SRU/stable.
   Prerequisites: <sha1>, <sha2>.
   ```

## Add A Local Packaging Or Config Patch

Use this for GB200-specific packaging/config deltas that are not upstream
kernel fixes.

1. Create or enter the base worktree.

   ```sh
   cd /Volumes/Linux/gb200
   base=noble-6.8
   tag=$(awk -F'\t' -v b="$base" '$1==b{print $2}' kernel/upstream-base.txt)
   git -C /Volumes/Linux/noble worktree add -b "gb200/$base" \
     "/Volumes/Linux/build/$base" "$tag" 2>/dev/null || true
   cd "/Volumes/Linux/build/$base"
   git switch "gb200/$base"
   ```

2. Make the kernel-tree change and commit it.

   Keep the subject explicit. Packaging patches should normally start with the
   Ubuntu packaging convention already used in the stack:

   ```sh
   git commit -s -m "UBUNTU: [Packaging] <short reason>"
   ```

   For GB200-only config or local SAUCE, include a drop condition in the commit
   body. If the patch has no realistic upstream destination, say that plainly.

3. Export the base.

   ```sh
   cd /Volumes/Linux/gb200
   tag=$(awk -F'\t' -v b="$base" '$1==b{print $2}' kernel/upstream-base.txt)
   rm -f "kernel/patches/gb200/$base"/*.patch
   git -C "/Volumes/Linux/build/$base" format-patch --zero-commit -N \
     "$tag..gb200/$base" -o "kernel/patches/gb200/$base"
   ./scripts/apply-series.sh --check "$base"
   python3 tools/dashboard/generate.py --offline
   make check
   ```

4. Build-test the base/arch targets that can consume the patch.

## Remove A Patch

Prefer removing patches from the kernel workbench branch, then exporting. Do
not just delete a patch file unless you have already proven later patches do
not depend on it.

### Automatic Removal During Treadmill Rebase

When Ubuntu publishes a new tag, the treadmill rebases with `--empty=drop`.
If a carried upstream patch has landed in the new base, git usually drops it.

Local equivalent:

```sh
cd /Volumes/Linux/gb200
./scripts/rebase-base.sh <base> <new-ubuntu-tag>
```

Review:

```sh
git diff -- kernel/upstream-base.txt kernel/patches/gb200 dashboard
cat dashboard/range-diff-<base>.txt
./scripts/apply-series.sh --check <base>
make check
```

If the rebase conflicts, resolve in `/Volumes/Linux/build/<base>`, continue the
git rebase, then finish the export:

```sh
git -C /Volumes/Linux/build/<base> rebase --continue
./scripts/rebase-base.sh --finish <base> <new-ubuntu-tag>
```

### Manual Removal Without Moving The Base Tag

Use this when a local patch is no longer desired, or when dashboard/drop
readiness shows a patch is already present but you are not changing the base
tag yet.

1. Enter the worktree and identify the commit.

   ```sh
   base=hwe-7.0
   cd "/Volumes/Linux/build/$base"
   git switch "gb200/$base"
   git log --oneline --reverse "$(awk -F'\t' -v b="$base" '$1==b{print $2}' \
     /Volumes/Linux/gb200/kernel/upstream-base.txt)..gb200/$base"
   ```

2. Drop it with an interactive rebase or a targeted reset/replay.

   ```sh
   tag=$(awk -F'\t' -v b="$base" '$1==b{print $2}' \
     /Volumes/Linux/gb200/kernel/upstream-base.txt)
   git rebase -i "$tag"
   ```

   Delete the commit line for the patch being removed. If later commits
   conflict, that is a signal the patch was not independent; stop and inspect
   the dependency chain before continuing.

3. Export and validate.

   ```sh
   cd /Volumes/Linux/gb200
   rm -f "kernel/patches/gb200/$base"/*.patch
   git -C "/Volumes/Linux/build/$base" format-patch --zero-commit -N \
     "$tag..gb200/$base" -o "kernel/patches/gb200/$base"
   ./scripts/apply-series.sh --check "$base"
   python3 tools/dashboard/generate.py --offline
   make check
   ```

4. Build-test at least the affected base/arch target.

## Create A New Ubuntu Kernel Base

Example: add `hwe-7.1` so the treadmill can track `linux-hwe-7.1`.

1. Confirm the Ubuntu package, tag prefix, and first tag.

   In `/Volumes/Linux/noble`:

   ```sh
   git fetch --tags origin
   git tag -l 'Ubuntu-hwe-7.1-*' --sort=-creatordate | head
   ```

   Decide:

   - base name: `hwe-7.1`
   - source package: usually `linux-hwe-7.1`
   - tag glob: `Ubuntu-hwe-7.1-*`
   - tag prefix: `Ubuntu-hwe-7.1-`
   - initial pin: newest published archive tag, not a `-next` branch

2. Add the base pin.

   Edit `kernel/upstream-base.txt`:

   ```text
   hwe-7.1	Ubuntu-hwe-7.1-7.1.0-1.1_24.04.1
   ```

3. Add dashboard/treadmill metadata.

   Edit `tools/dashboard/config.json`:

   ```json
   {
     "name": "hwe-7.1",
     "clone": "/Volumes/Linux/noble",
     "tag_glob": "Ubuntu-hwe-7.1-*",
     "patch_branch": "gb200/hwe-7.1",
     "osv_ecosystem": "Ubuntu:24.04:LTS",
     "package": "linux-hwe-7.1",
     "tag_prefix": "Ubuntu-hwe-7.1-"
   }
   ```

   `tools/treadmill/detect.py` reads this config. Once the new base is in both
   files, the treadmill can map Launchpad publications to Ubuntu tags.

4. Add module matrix rows for every release target.

   Edit `modules/matrix.tsv`:

   ```text
   hwe-7.1	arm64	doca	<version>	<arm64-doca-repo-or-mrc-source>
   hwe-7.1	arm64	nvidia-open	<version>	https://github.com/NVIDIA/open-gpu-kernel-modules
   hwe-7.1	amd64	doca	<version>	<amd64-doca-repo-or-mrc-source>
   hwe-7.1	amd64	nvidia-open	<version>	https://github.com/NVIDIA/open-gpu-kernel-modules
   ```

   Then run:

   ```sh
   ./scripts/check-module-sources.sh hwe-7.1 arm64 noble
   ./scripts/check-module-sources.sh hwe-7.1 amd64 noble
   ```

5. Seed the patch branch.

   If the new base should start stock Ubuntu:

   ```sh
   tag=Ubuntu-hwe-7.1-7.1.0-1.1_24.04.1
   git -C /Volumes/Linux/noble worktree add -b gb200/hwe-7.1 \
     /Volumes/Linux/build/hwe-7.1 "$tag"
   mkdir -p kernel/patches/gb200/hwe-7.1
   ```

   If it should inherit the previous HWE delta, rebase the old branch onto the
   new tag:

   ```sh
   old_base=hwe-7.0
   new_base=hwe-7.1
   old_tag=$(awk -F'\t' -v b="$old_base" '$1==b{print $2}' kernel/upstream-base.txt)
   new_tag=$(awk -F'\t' -v b="$new_base" '$1==b{print $2}' kernel/upstream-base.txt)

   git -C /Volumes/Linux/noble worktree add -b "gb200/$new_base" \
     "/Volumes/Linux/build/$new_base" "gb200/$old_base"
   git -C "/Volumes/Linux/build/$new_base" rebase --empty=drop \
     --onto "$new_tag" "$old_tag" "gb200/$new_base"
   ```

6. Export the new stack.

   ```sh
   rm -f kernel/patches/gb200/hwe-7.1/*.patch
   git -C /Volumes/Linux/build/hwe-7.1 format-patch --zero-commit -N \
     "$new_tag..gb200/hwe-7.1" -o kernel/patches/gb200/hwe-7.1
   ```

   If the stack is empty, `apply-series.sh` still works without patch files.
   Git will not track an empty directory, which is fine.

7. Validate the new base.

   ```sh
   ./scripts/apply-series.sh --check hwe-7.1
   python3 tools/dashboard/generate.py --offline
   python3 tools/treadmill/detect.py --json
   make check
   PUBLISH=0 ./scripts/build-all.sh hwe-7.1 "" noble arm64
   PUBLISH=0 ./scripts/build-all.sh hwe-7.1 "" noble amd64
   ```

8. Commit all record changes.

   Include:

   - `kernel/upstream-base.txt`
   - `kernel/patches/gb200/hwe-7.1/*.patch` if any
   - `tools/dashboard/config.json`
   - `modules/matrix.tsv`
   - dashboard output/range-diff if intentionally regenerated

## Move A Base To A New Ubuntu Publication

This is the SRU treadmill path.

Automatic CI path:

1. `tools/treadmill/detect.py` sees a newer Launchpad publication.
2. `.github/workflows/update-and-rebase.yml` rebases the stack.
3. A PR opens with the updated pin, exported patches, and range-diff.
4. `kernel-ci.yml` gates the PR.
5. Merge triggers `publish.yml`.

Manual path:

```sh
cd /Volumes/Linux/gb200
python3 tools/treadmill/detect.py
./scripts/rebase-base.sh <base> [target-tag]
make check
PUBLISH=0 ./scripts/build-all.sh <base> "" noble arm64
PUBLISH=0 ./scripts/build-all.sh <base> "" noble amd64
```

Review `dashboard/range-diff-<base>.txt` before committing. Dropped patches
are expected when Ubuntu has absorbed our carry; unexpected drops need review.

## Update A Module Pin

Use this for DOCA, NVIDIA open modules, or internal module sources.

1. Edit `modules/matrix.tsv` for the specific `base/arch/module` rows.
2. Run source preflight.

   ```sh
   ./scripts/check-module-sources.sh <base> arm64 noble
   ./scripts/check-module-sources.sh <base> amd64 noble
   ```

3. Build the module path through the release-shaped script.

   ```sh
   PUBLISH=0 ./scripts/build-all.sh <base> "" noble arm64
   PUBLISH=0 ./scripts/build-all.sh <base> "" noble amd64
   ```

4. Regenerate the dashboard and run checks.

   ```sh
   python3 tools/dashboard/generate.py --offline
   make check
   ```

Module packages are versioned as:

```text
<module-version>+kernel.<exact-kernel-package-version>
```

and depend on the exact `linux-image-<kver>` package version. Do not relax
that dependency unless the module ABI policy changes deliberately.

## Build And Publish A Release Manually

For a local or emergency publish:

```sh
RELEASE=1 ./scripts/build-all.sh <base> "" noble arm64
RELEASE=1 ./scripts/build-all.sh <base> "" noble amd64
./scripts/validate-apt-repo.sh /Volumes/Linux/repo noble
python3 -m http.server -d /Volumes/Linux/repo
```

For a dry release build without touching the apt repo:

```sh
RELEASE=1 PUBLISH=0 ./scripts/build-all.sh <base> "" noble arm64
RELEASE=1 PUBLISH=0 ./scripts/build-all.sh <base> "" noble amd64
```

Each successful build writes:

```text
build/out/<base>/<arch>/<run-id>/.publish-manifest
build/out/<base>/<arch>/<run-id>/gb200-provenance.json
```

`publish-repo.sh` copies provenance to:

```text
repo/provenance/<base>/<arch>/<run-id>.json
```

To create a signed release tag with notes generated from the release
provenance:

```sh
./scripts/tag-release.sh gb200-<date-or-version> build/out/<base>
```

The script previews the generated notes, then creates `git tag -s`. Pass
`--push` as the third argument to push the tag after creation.

## Dashboard Workflow

Fast local refresh:

```sh
python3 tools/dashboard/generate.py --offline
```

Network refresh:

```sh
python3 tools/dashboard/generate.py --refresh
```

Deep patch-drop checks:

```sh
python3 tools/dashboard/generate.py --offline --deep-git-checks
```

The dashboard is a triage surface, not the source of truth. Source of truth is
still `upstream-base.txt`, exported patch files, module pins, and release
provenance.

## Before Opening Or Merging A PR

Run:

```sh
make check
git diff --check
python3 tools/dashboard/generate.py --offline
```

`make check` includes workflow hardening checks: external GitHub actions must
be pinned by SHA, workflows need explicit permissions and concurrency, and
checkout credentials must be disabled unless a later step deliberately sets an
authenticated remote.

For release-affecting changes also run, or ensure CI runs:

```sh
./scripts/check-module-sources.sh <base> arm64 noble
./scripts/check-module-sources.sh <base> amd64 noble
PUBLISH=0 ./scripts/build-all.sh <base> "" noble arm64
PUBLISH=0 ./scripts/build-all.sh <base> "" noble amd64
```

Do not merge a patch-stack change just because it applies. At minimum, review
the range-diff, provenance trailers, module matrix health, and dashboard action
queue.
