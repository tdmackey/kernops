---
name: userspace-backport
description: Add, update or fix a kernel-coupled userspace package (pahole, bpftrace, makedumpfile, iproute2, firmware, ...) rebuilt for noble or resolute from a newer Ubuntu series. Use when a package is too old for the kernel, a backport fails to build, or when asked what userspace needs updating.
---

# Userspace backports

`userspace/matrix.tsv` lists `series, source package, from-series, version, why`.
`scripts/build-userspace.sh` rebuilds each row in the series' container and
serves what it has built to later rows. `docs/workflows.md` ("Userspace
packages") has the commands.

## Choosing a version

1. Ask Launchpad what each series ships; do not go from memory:
   `https://api.launchpad.net/1.0/ubuntu/+archive/primary?ws.op=getPublishedSources&source_name=<pkg>&exact_match=true&status=Published`
2. Take it from the series that ships our kernel version. Name a newer
   development series only where it already packages a newer upstream release.
3. Then check upstream too (`git ls-remote --tags`): Ubuntu can lag what the
   kernel needs. If no Ubuntu series has the fix, carry the upstream commits as
   patches (see `userspace/makedumpfile/`), and say in `notes.md` when the
   directory can be deleted.
4. List a package for a series only if that series' own version is older.

Old tools usually keep working on a new kernel. Tools that read kernel
internals (pahole, crash, makedumpfile, BPF) are the ones that must match.

## When a build fails

Find the actual reason before changing anything; the script prints apt's.

| Symptom | Cause | Fix |
|---|---|---|
| built in seconds, tiny .deb | it is a metapackage; the content moved to other source packages | add those sources as rows |
| `debhelper-compat (= N)` unsatisfied | packaged for a newer debhelper | the `debhelper` row, not a per-package edit |
| a `-dev` package "not installable" | the series splits or names it differently | a one-line patch to `debian/control` under `userspace/<pkg>/<series>/patches/` |
| undefined kernel constant | needs our kernel's API headers | build the kernel for that series first |
| needs `bpftool` | same | same |
| links against the series' old library | should not happen: the builder upgrades to our packages first | check the library row comes earlier in the matrix |

Prefer a patch to `debian/control` over replacing the whole file: it survives
the next version bump or fails loudly.

## Bookkeeping

- A new or changed source records its checksum in `userspace/sources.lock`;
  commit it. Changing a version: edit the row, delete the old lock line, build.
- Changing our layers for the same source version: bump
  `userspace/<pkg>/revision`.
- Every deviation gets a `userspace/<pkg>/notes.md`: what, why, and when it
  can be removed.
- Libraries replace the system copy on the target. Check the library package
  name did not change (`libfoo1` → `libfoo2`); if it did, say so.
- Do not edit `matrix.tsv` or the script while a build is running.
