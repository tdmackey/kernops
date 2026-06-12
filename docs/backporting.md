# Backporting an upstream commit

The recurring task: an upstream fix hasn't reached the Ubuntu bases we ship
(6.8 / 6.17 / …) and we want to carry it until it arrives via SRU/stable.
This documents the process end to end, with the traps we've already hit.

Principles (from the master plan):

- **Minimal carry.** Cherry-pick the fix and its hard prerequisites, nothing
  speculative. Every patch must have a drop condition.
- **Provenance is sacred.** `cherry-pick -x` always; if you adapt the code,
  mark it `backported from` and say what you changed. This is what makes
  automatic drop detection possible later.
- **The git branch is the workbench; the exported patch dir is the record.**

## Fast path: `scripts/backport.sh`

For most fixes this one command does steps 0–3 and 5 across **all** bases:

```sh
./scripts/backport.sh [--dry-run] <sha> [<sha>...]
```

It fetches mainline if a sha is unknown, audits every base in
`kernel/upstream-base.txt` (against the patch branch if it exists, else the
base tag), cherry-picks `-x` only what's missing in oldest-first order,
creates worktrees/branches on demand, exports the patch dirs, and
regenerates the dashboard. On conflict it aborts that base and lists it
under "need a human" — then you do the manual flow below for that base
(usually it means a missing prerequisite or partial stable backport).

It never resolves conflicts and never commits the monorepo: build-test the
changed bases, review the diff, commit yourself. Always start with
`--dry-run` to sanity-check the audit (prerequisites missing on a base show
up as MISSING there too — feed them as additional shas).

The manual process it automates:

## 0. Get the commit

The noble clone has two remotes:

- `origin` — Launchpad Ubuntu kernel git (tags `Ubuntu-*`)
- `upstream` — kernel.googlesource.com mirror of torvalds/linux (fast, and
  unlike git.kernel.org not behind an anti-bot wall)

```sh
cd /Volumes/Linux/noble
git fetch --no-tags upstream master      # incremental; gets recent mainline
git log -1 <sha>                         # confirm it's what you think it is
```

## 1. Audit: is it (partially) there already?

**Audit against the build tag from `kernel/upstream-base.txt`, never a
branch head.** `origin/master` was 40 releases behind the tag we build when
this burned us. Two checks per base, because stable backports get new SHAs
(ancestry check alone lies) and sometimes new subjects (subject grep alone
lies too):

```sh
./scripts/check-present.sh <sha> Ubuntu-6.8.0-130.130 Ubuntu-hwe-6.17-6.17.0-38.38_24.04.1
```

Then look at the **whole series**, not just your commit — Ubuntu/stable
routinely takes part of a series (we found three separate cases on day one:
tegra timeout set, curr_xfer protection set, writeback lockup set):

```sh
# everything upstream did to the file(s), newest first
git log --oneline upstream/master -- <files>
# what the base actually has
git log --oneline <base-tag> -- <files>
```

## 2. Identify hard prerequisites

A pick that "needs" missing infrastructure shows up three ways:

- the commit message says so (`Fixes:` tags, "prepare for", series cover
  letters — check `b4`/lore if unsure);
- the diff calls functions/symbols the base doesn't have
  (`git grep <symbol> <base-tag> -- <file>`);
- the cherry-pick conflicts on context that turns out to be a sibling patch.

Take the minimal prerequisite chain in **upstream chronological order**
(`git log --reverse`). Example from the tegra fix: `5b94c94caafc` needs
`tegra_qspi_handle_timeout()`, introduced by `380fd29d57ab`, which needs the
helpers from `6022eacdda8b` — so the carry is those three, in that order.

## 3. Apply on each base branch

Each base has a worktree + branch (`git worktree add -b gb200/<base>
/Volumes/Linux/build/<base> <base-tag>` if it doesn't exist yet):

```sh
cd /Volumes/Linux/build/noble-6.8        # branch gb200/noble-6.8
git cherry-pick -x <prereq1> <prereq2> <fix>
```

On conflict:

- Resolve to the **upstream end state** — when unsure what that should look
  like on older code, read the same region on a base that already has the
  series (hwe-7.0 usually) or `git show <sha>:<file>`.
- A conflict where HEAD already contains one side's effect usually means a
  partial stable backport — re-run the step-1 audit on that file before
  hand-merging anything.
- After resolving, edit the trailer: `(cherry picked from …)` →
  `(backported from …)` plus a one-line bracket note, e.g.
  `[gb200: adapted timeout-path conflicts — 6.8 lacks the internal-DMA
  null-check variant]`. Keep `git rerere` happy by finishing with
  `git cherry-pick --continue`, not a manual commit.

Sanity checks that are cheap and have caught real problems:

```sh
git cherry-pick --abort                          # if it all smells wrong
git show <sha> | git apply --check --reverse -   # "already applied?" test
grep -c <key-symbol> <file>                      # compare against upstream count
```

## 4. Compile-test

```sh
/Volumes/Linux/gb200/scripts/build-kernel.sh /Volumes/Linux/build/<base> generic-64k <series>
```

Warm ccache makes iteration on a single-file fix cheap. A full deb build per
affected base before export is the bar; QEMU boot for anything that touches
early boot or core subsystems.

## 5. Export to the monorepo (the record)

```sh
cd /Volumes/Linux/gb200
rm -f kernel/patches/gb200/<base>/*.patch
git -C /Volumes/Linux/build/<base> format-patch --zero-commit -N \
    <base-tag>..gb200/<base> -o kernel/patches/gb200/<base>/
# update kernel/upstream-base.txt if the base tag moved
python3 tools/dashboard/generate.py --offline
git add -A && git commit
```

Commit message: say **why** the patch is carried, what it depends on, and the
**drop condition** (usually "drop when <sha> reaches the base via stable —
detected automatically from the -x trailer"). If Ubuntu's omission looks like
an oversight (incomplete series), consider also filing/poking the Launchpad
bug so the carry becomes Canonical's problem.

## Gotchas log

- `origin/master` ≠ newest tag. Audit against the tag you build.
- Stable backports change SHAs and sometimes subjects/diffs; check by
  content (`git apply --check --reverse`) when in doubt.
- Partial series are the norm, not the exception. Look at file history, not
  just your one commit.
- git.kernel.org cgit is bot-walled; use the googlesource mirror or the
  GitHub mirror (`api.github.com/repos/torvalds/linux/commits/<sha>`).
- This shell is zsh: `echo ===` word-expands (`=cmd` lookup) and unquoted
  `$VAR` doesn't word-split — prefer explicit lists in loops.
