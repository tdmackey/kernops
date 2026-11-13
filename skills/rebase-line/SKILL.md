---
name: rebase-line
description: Move a kernel line to a newer base (a new linux-7.3.y stable release, or a new Canonical release of the Ubuntu kernel under `7.0` or `hwe-6.17`) and update its patch series. Use when `kpm updates` reports a base as STALE, or when asked to update, rebase or bump a line.
---

# Move a line to a new base

Do this work on Linux with `WORK_ROOT` set. In the commands, `$L` is the line
and `$T` is the new tag.

## Procedure

1. Find the new tag.
   ```sh
   ./scripts/kpm --json updates
   ```
   Use the `latest` value of the row that has `"what": "base"` for the line.
   For an Ubuntu line, this is the newest kernel that the archive published.
   Do not use a newer git tag. That kernel can be in `-proposed` only.

2. Get the new base and rebuild the patch series on it.
   ```sh
   ./scripts/kpm --line $L fetch
   ./scripts/kpm --line $L import
   ./scripts/kpm --line $L rebase $T
   ```
   - Exit code 0: the patch series is written. Go to step 4.
   - Exit code 3: a patch has a conflict. Go to step 3.
   - Other exit codes: `kpm` put the line back to its old base. Correct the
     cause and do step 2 again.

3. Resolve the conflict in the directory that the message gives.
   1. Find out if the new base already contains the change. If it does, drop
      the patch. Do not force it.
   2. If it does not, resolve the conflict to agree with the upstream code.
      `docs/backporting.md` gives the method.
   3. Add a `Conflicts:` note to the commit message.
   4. Run `git add`, then:
      ```sh
      ./scripts/kpm --line $L import --continue
      ./scripts/kpm --line $L export
      ```
   If the message says that a patch was "adapted by hand", compare the staged
   change with the old patch file and make the same adaptation again.

4. Examine the result. Do not stop at "it applies".
   - Read the list of patches that became `in-base`.
   - Read the range-diff in `~/.cache/kpm/range-diff-$L.txt`.
   - Read each patch that `export` reports as `adapted`.

5. Find upstream fixes that the line does not have.
   ```sh
   ./scripts/kpm --line $L fetch mainline
   ./scripts/kpm --line $L verify --strict --fixes mainline/master
   ```

6. Build and boot.
   - `7.3-lts`:
     ```sh
     ./scripts/in-builder.sh resolute ./scripts/build-line.sh $L resolute
     ./scripts/vng-boot.sh $L
     ```
   - `7.0`: as `7.3-lts`, for noble and for resolute.
   - `hwe-6.17`: `./scripts/build-all.sh $L "" noble <arch>`

7. Commit `kernel/lines/$L/`. In the message, give:
   - the old base and the new base
   - the number of patches that dropped out because upstream has them
   - each conflict that you resolved

Move one line in each commit.
