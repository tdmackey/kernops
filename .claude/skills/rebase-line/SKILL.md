---
name: rebase-line
description: Move a kernel line to a newer base (a new linux-7.3.y stable release, or a new Canonical hwe publication) and bring its patch series along. Use when `kpm updates` reports a base as STALE, or when asked to update, rebase or bump a line.
---

# Move a line to a new base

Work on Linux with `WORK_ROOT` set. `L` is the line, `T` the new tag.

1. **Find the target.** `./scripts/kpm --json updates` → the row with
   `what == "base"` for the line gives `latest`. For Ubuntu lines this is the
   newest *published* kernel; do not pick a newer git tag, it may be in
   -proposed.
2. **Fetch and rebase.**
   ```sh
   ./scripts/kpm --line $L fetch
   ./scripts/kpm --line $L import          # current stack, so a range-diff can be written
   ./scripts/kpm --line $L rebase $T
   ```
   Exit 0: done, the series is re-exported. Exit 3: a patch conflicts.
3. **On a conflict**, resolve in the worktree the message names, following
   `docs/backporting.md`: first check whether the content is already in the new
   base (then drop it, do not force it), otherwise resolve toward what upstream
   ended up with and add a `Conflicts:` note. Then
   `./scripts/kpm --line $L import --continue` and `./scripts/kpm --line $L export`.
4. **Review what changed**, not just that it applies: the list of patches that
   became `in-base`, the range-diff in `~/.cache/kpm/range-diff-$L.txt`, and
   anything exported as `adapted`.
5. **Check for fixes we now lack**: `./scripts/kpm --line $L verify --strict --fixes mainline/master`
   lists upstream commits whose `Fixes:` tag names something we carry.
6. **Build and boot** at least amd64:
   `./scripts/in-builder.sh resolute ./scripts/build-line.sh $L resolute`, then
   `./scripts/vng-boot.sh $L`.
7. Commit `kernel/lines/$L/`. The message says old base → new base, how many
   patches dropped out as upstream, and which conflicts were resolved by hand.

Do not rebase more than one line in one commit.
