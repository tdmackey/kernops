---
name: forward-port
description: Decide which patches from a vendor tree built on an OLDER kernel (for example NVIDIA BOS on 7.0) should be carried on a newer line (7.3), without re-adding anything upstream has since solved differently. Use when forward-porting BOS or any vendor delta across kernel versions.
---

# Forward-port from an older base

The risk is not conflicts. It is carrying a patch whose problem upstream has
already solved another way: it may apply cleanly and then fight the upstream
code. Audit every candidate before importing any.

## Sort every candidate into one of five

After `harvest` and `resolve` have removed what is provably in the base or a
duplicate, for each remaining commit:

| Finding | How to tell | Disposition |
|---|---|---|
| **Present** | the patch reverse-applies to the line's base | `in-base` |
| **Same-title twin** | a commit with the same subject is in the base range, content differs | read both; almost always `superseded` |
| **Reworked** | every function, struct or symbol the patch adds already exists in the base, under upstream's design | `superseded`; note the upstream commits |
| **Depends on a superseded API** | it calls interfaces upstream replaced (e.g. the old CXL Type-2 calls) | `deferred` until the vendor reworks it; say so |
| **Absent** | none of the above; the problem is still unsolved upstream | candidate to `carry` |

Also net out the vendor's own reverts first, and drop SAUCE in favour of the
upstream form when one exists, even if it needs a boot option to enable (record
the option in `docs/design-7.3-lts.md`).

Things that look absent but are not:
- upstream merged a later revision of the series under different subjects;
  search by the functions touched, not the title;
- the patch was a workaround for a bug upstream fixed at the root;
- the hardware enablement landed through a different subsystem tree.

## Then

1. Record a reason on every row, including the upstream commit for each
   `superseded`.
2. Import only the `carry` set. Resolve conflicts toward the newer kernel's
   structure (`docs/backporting.md`), with a `Conflicts:` note.
3. Compile-check both architectures. Say plainly what was compile-checked,
   booted, or tested on hardware; a forward-port is usually only the first.
4. Group what was deferred by what it is waiting for, so the next pass knows
   when to look again.

When unsure whether upstream's approach covers a patch, it is `deferred` with
the question written down, not carried.
