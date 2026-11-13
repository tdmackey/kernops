---
name: vendor-resync
description: Take in new commits from a vendor kernel tree (Ubuntu, NVIDIA, GCP, AWS, Azure) that a line tracks, decide each one, and regenerate the patch series. Use when `kpm updates` reports a source as MOVED, when asked to pull in vendor fixes, or when adding a new source tree such as Google COS.
---

# A vendor published new commits

`L` is the line, `S` the source name from `kernel/sources.toml`.

```sh
./scripts/kpm --line $L fetch $S
./scripts/kpm --line $L harvest $S      # new rows; a vendor rebase keeps old decisions by content
./scripts/kpm --line $L resolve $S      # proves what is in the base or a duplicate of another source
./scripts/kpm --line $L status
```

Then every row left `undecided`, and every row with a `proposed` match, needs a
decision (`./scripts/kpm --line $L decide …`):

- **Proposals are subject-only matches.** Read both diffs before accepting.
  Same title with different content is common (a vendor's backport vs upstream's
  final version).
- `harvest` reports rows that **vanished** from the vendor tree. If we carry
  one, find out why the vendor dropped it before keeping it.
- Revert pairs inside the vendor tree cancel out; check that what is left is
  what the vendor ships.
- A patch that only makes sense for hardware or a product we do not build for
  is `dropped` with that reason, not `deferred`. `deferred` means "we want it,
  not yet", and says what it is waiting for.

Then rebuild the series and check it:

```sh
./scripts/kpm --line $L import          # exit 3 = conflict; see docs/backporting.md
./scripts/kpm --line $L export
./scripts/kpm --line $L config --write  # did the vendor enable options we should too?
./scripts/kpm --line $L verify --strict
```

## A source on an older base than the line

For example a 7.0 tree feeding the 7.3 line. Use the `forward-port` skill: the
default disposition is `deferred` in `policy.toml`, and each candidate is
audited before it is carried.

## Adding a source

Add it to `kernel/sources.toml` (URL, tag glob or branch, `delta_base`, a free
band), add its name to the line's `sources`, add a policy entry, then run the
steps above. A source built on another mainline release sets `upstream`, which
also keeps the fetch small.
