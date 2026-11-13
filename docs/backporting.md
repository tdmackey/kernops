# Backporting an upstream commit

The recurring task: an upstream fix has not reached a base we ship, and we want
to carry it until it does. The commands are in
[workflows.md](workflows.md#carry-one-of-our-own-patches); this document is the
judgment that goes with them.

## Principles

- **Minimal carry.** The fix and its hard prerequisites, nothing speculative.
  Every patch should have a reason to exist and a way to disappear: `kpm
  rebase` drops it once its content reaches the base.
- **Provenance is sacred.** `kpm add` records the upstream SHA. If you adapt
  the code, say so in the commit message (`backported from`, plus a
  `Conflicts:` note describing what you changed). Automatic drop detection and
  the missing-`Fixes:` check both depend on it.
- **The branch is the workbench; the patch files are the record.**

## 1. Is it, or part of it, already there?

`kpm add` checks this for the commit you name: ancestry, a commit that names
it, the same subject on the same files, and whether its diff reverse-applies.
It refuses to add what is already present.

That is not enough on its own. Stable and Ubuntu routinely take **part** of a
series, so look at the whole history of the files, not just your commit:

```sh
git -C $WORK_ROOT/kernel.git log --oneline mainline/master -- <files>
git -C $WORK_ROOT/kernel.git log --oneline <base-source>/<base-tag> -- <files>
```

Audit against the base tag in `line.toml`, never a branch head.

## 2. Identify hard prerequisites

A pick that needs missing infrastructure shows up three ways:

- the commit message says so (`Fixes:` tags, "prepare for", a series cover
  letter);
- the diff calls functions or symbols the base does not have;
- the cherry-pick conflicts on context that turns out to be a sibling patch.

Take the minimal chain in upstream chronological order and pass all of it to
`kpm add` in that order.

## 3. Resolving a conflict

`kpm import` stops with exit code 3 and lists the files.

- Resolve to the **upstream end state**. When unsure what that looks like on
  older code, read the same region on a line that already has the series.
- A conflict where the tree already contains one side's effect usually means a
  partial backport is present. Go back to step 1 for that file before merging
  anything by hand.
- Add the `Conflicts:` note, then `git add` and `kpm import --continue`.

## 4. Prove it

```sh
kpm export
kpm verify --strict --fixes mainline/master
./scripts/build-line.sh <line> <series>      # or build-all.sh for the hwe lines
```

Anything that touches memory management, IOMMU or DMA must not ship on a
4k-only test pass; it needs the 64k build and boot as well.

## Gotchas log

- A branch head is not the newest tag. Audit against the tag you build.
- Stable backports change SHAs and sometimes subjects and diffs. When in
  doubt, check by content.
- Partial series are the norm, not the exception.
- git.kernel.org is bot-walled for automated clients; `sources.toml` uses the
  googlesource mirrors.
- On WSL, never work on a kernel tree under `/mnt/<drive>`: the filesystem is
  case-insensitive and slow. Use the distro's own ext4 disk.
