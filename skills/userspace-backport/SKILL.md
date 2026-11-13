---
name: userspace-backport
description: Add, update or repair a userspace package that depends on the kernel version (pahole, bpftrace, makedumpfile, iproute2, firmware and others), built for noble or resolute from a newer Ubuntu series. Use when a package is too old for the kernel, when a package build fails, or when asked which userspace packages need an update.
---

# Userspace packages

## How it works

- `userspace/matrix.tsv` has one row for each package and series:
  series, source package, the Ubuntu series to take it from, version, reason.
- `userspace/sources.lock` has the checksum of each source.
- `scripts/build-userspace.sh` builds the rows in the container of the series.
  A package can build against the packages that earlier rows made.
- `userspace/<package>/` contains our changes to a package, if there are any.

`docs/workflows.md`, section "Userspace packages", gives the commands.

## Select a version

1. Ask Launchpad which version each series has. Do not use memory.
   ```
   https://api.launchpad.net/1.0/ubuntu/+archive/primary?ws.op=getPublishedSources&source_name=<package>&exact_match=true&status=Published
   ```
2. Use the version from the series that has our kernel version.
3. Use a newer development series only if it has a newer upstream release.
4. Examine the upstream project also. Ubuntu can be behind the version that
   the kernel needs.
   ```sh
   git ls-remote --tags <upstream repository>
   ```
5. If no Ubuntu series has a necessary fix, add the upstream commits as patches
   in `userspace/<package>/patches/`. `userspace/makedumpfile/` is an example.
6. Add a row for a series only if the version in that series is older.

Most old tools continue to work on a new kernel. These tools read kernel data
structures and must agree with the kernel: `pahole`, `crash`, `makedumpfile`,
and the BPF tools.

## If a build fails

Find the cause first. The script prints the reason that `apt` gives.

| What you see | Cause | Correction |
|---|---|---|
| The build takes seconds and the `.deb` is very small. | The package is a metapackage. Other source packages have the content. | Add those source packages as rows. |
| `debhelper-compat (= N)` cannot be installed. | The package needs a newer debhelper. | Use the `debhelper` row. Do not change each package. |
| A `-dev` package "is not installable". | The series has a different name for it. | Add a small patch for `debian/control` in `userspace/<package>/<series>/patches/`. |
| A kernel constant is not defined. | The build needs the headers of our kernel. | Build the kernel for that series first. |
| `bpftool` is missing. | The same. | The same. |
| The package links to the old library of the series. | The library row is after this row. | Move the library row before this row. |

Change `debian/control` with a patch. Do not replace the file. A patch fails
clearly when the next version changes the file.

## Records to keep

- A new or changed source adds a line to `userspace/sources.lock`. Commit it.
- When you change our patches for the same source version, increase the number
  in `userspace/<package>/revision`.
- Write `userspace/<package>/notes.md` for each change: what, why, and when we
  can remove it.
- A library package replaces the library on the target system. Make sure that
  the package name did not change (for example `libfoo1` to `libfoo2`). If it
  changed, report it.
- Do not edit `matrix.tsv` or the script while a build runs.
