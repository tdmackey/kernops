---
name: forward-port
description: Decide which patches from a source on an OLDER kernel (for example NVIDIA BOS on 7.0) a newer line (7.3) must carry, and make sure that no patch is carried when upstream solved the problem in a different way. Use when forward-porting BOS or any vendor patches to a newer kernel version.
---

# Forward-port patches from an older kernel

## The risk

A patch from an older kernel can apply without a conflict and still be wrong.
This occurs when upstream solved the same problem in a different way. The old
patch then works against the upstream code.

Examine every candidate before you import one.

## Procedure

1. Run `harvest` and `resolve` for the source (see the `vendor-resync` skill).
   This removes the commits that are in the base and the duplicates.

2. Remove the reverts. If the source applies a patch and then reverts it, the
   two commits cancel.

3. Put each remaining commit into one group of this table.

   | Group | How to identify it | Decision |
   |---|---|---|
   | Present | The patch applies in reverse to the base of the line. | `in-base` |
   | Same title | The base has a commit with the same title and different content. Read the two diffs. | Usually `superseded` |
   | Reworked | The base already has each function, structure and symbol that the patch adds, in the upstream design. | `superseded`. Record the upstream commits. |
   | Old interface | The patch calls an interface that upstream replaced. | `deferred` until the vendor changes the patch. Say this in the reason. |
   | Absent | None of the above. Upstream did not solve the problem. | Candidate for `carry` |

4. Examine each "Absent" commit again. A commit can look absent when it is not:
   - Upstream merged a later version of the patch set with different titles.
     Search for the functions, not for the title.
   - The patch was a workaround, and upstream corrected the cause.
   - The hardware support came into upstream through a different subsystem.

5. If upstream has a different form of a vendor patch, use the upstream form.
   Do this also when the upstream form needs a boot option. Record the boot
   option in `docs/design-7.3-lts.md`.

6. Record a reason for each row. For `superseded`, give the upstream commit.

7. Import only the rows with the decision `carry`.
   - Resolve each conflict to agree with the structure of the newer kernel.
   - Add a `Conflicts:` note.

8. Compile for amd64 and arm64.

9. Report the result:
   - what you compiled, what you booted, and what you tested on hardware
   - the deferred rows, in groups, with the condition that each group waits for

## The same risk in other work

This risk is not only in a forward-port. It is the same for a CVE fix and for a
fix from a different stable branch. For CVEs, use the `cve-triage` skill.

## If you are not sure

If you cannot tell whether the upstream code makes a patch unnecessary, use
`deferred`. Write the question in the reason. Do not carry the patch.
