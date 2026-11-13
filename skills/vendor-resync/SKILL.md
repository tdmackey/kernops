---
name: vendor-resync
description: Take in new commits from a source (the Ubuntu, NVIDIA, GCP, AWS or Azure kernel tree) that a line uses, decide each commit, and write the patch series again. Use when `kpm updates` reports a source as MOVED, when asked to get vendor fixes, or when adding a new source such as Google COS.
---

# Take in new commits from a source

In the commands, `$L` is the line and `$S` is the name of the source in
`kernel/sources.toml`.

## Procedure

1. Read the new commits into the ledger.
   ```sh
   ./scripts/kpm --line $L fetch $S
   ./scripts/kpm --line $L harvest $S
   ./scripts/kpm --line $L resolve $S
   ./scripts/kpm --line $L status
   ```
   - `harvest` adds a row for each new commit. If the vendor rebased its tree,
     a commit with the same content keeps its decision.
   - `resolve` decides the rows that it can prove: the change is in the base,
     or it is a duplicate of a row in another source.

2. Read the `vanished` list that `harvest` prints. These commits are no longer
   in the source. If the line carries one of them, find out why the vendor
   removed it before you keep it.

3. Decide each row that is `undecided`, and each row that has a proposal.
   ```sh
   ./scripts/kpm --line $L decide <decision> <change id> --reason "..."
   ```
   - A proposal is a match that `kpm` could not prove. Read the two diffs
     before you accept it. Two commits with the same title can have different
     content.
   - Use `dropped` for a patch that we do not want, for example a patch for
     hardware that we do not build for. Give the reason.
   - Use `deferred` for a patch that we want later. Say what it waits for.
   - Use `superseded` when upstream solved the problem in a different way.
     Give the upstream commit.

4. Write the patch series again and check it.
   ```sh
   ./scripts/kpm --line $L import
   ./scripts/kpm --line $L export
   ./scripts/kpm --line $L config --write
   ./scripts/kpm --line $L verify --strict
   ```
   - If `import` exits with code 3, resolve the conflict
     (`docs/backporting.md`), then run `import --continue`.
   - `config --write` shows the config options that the source enables and the
     line does not.

5. Build and boot the line, then commit `kernel/lines/$L/`.

## A source on an older kernel than the line

Example: a source on 7.0 for the 7.3 line. Use the `forward-port` skill. In
`policy.toml`, make `deferred` the default decision for this source. Examine
each commit before you carry it.

## Add a source

1. Add the source to `kernel/sources.toml`: URL, tag pattern or branch,
   `delta_base`, and a band that is not in use.
2. If the source is on a different mainline release than the line, set
   `upstream`. This also keeps the fetch small.
3. Add the name to `sources` in the `line.toml` of the line.
4. Add a rule for the source to `policy.toml`.
5. Do the procedure above.
