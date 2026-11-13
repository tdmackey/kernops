# kernops

This repository builds Linux kernel packages for Ubuntu.

- The main line is `7.3-lts`: the upstream stable kernel `linux-7.3.y` plus
  patches from Ubuntu, NVIDIA, GCP, AWS, Azure, and our own.
- Two more lines, `hwe-7.0` and `hwe-6.17`, use Canonical's kernels as the base.
- Packages are built for Ubuntu noble (24.04) and resolute (26.04), on amd64
  and arm64.

## Words used in this repository

| Word | Meaning |
|---|---|
| line | One kernel we maintain. A directory under `kernel/lines/`. |
| base | The upstream tag a line starts from, for example `v7.3.4`. |
| source | A kernel tree we take patches from. Listed in `kernel/sources.toml`. |
| ledger | The record of one decision for each commit of a source. |
| patch series | The ordered list of patches that a line applies to its base. |
| series | An Ubuntu release: `noble` or `resolute`. |
| workbench | The git branch `work/<line>` that `kpm` builds the patch series from. |

## Where things are

| Path | Contents |
|---|---|
| `kernel/sources.toml` | All sources and their patch-number bands. |
| `kernel/identity.toml` | Product name, architectures, version scheme, build switches for each series. |
| `kernel/lines/<line>/` | `line.toml` (the base), `ledger/`, `series`, `patches/`, `config/`. |
| `tools/kpm/` | The patch manager. Run it with `./scripts/kpm`. |
| `scripts/` | Build, boot-test and release scripts. |
| `userspace/` | Userspace packages that depend on the kernel version. |
| `modules/` | Versions of the out-of-tree modules. |
| `docs/` | `workflows.md` has the procedures. `design-7.3-lts.md` gives the reasons. |

## Commands you need most

```sh
./scripts/kpm updates                          # Show what changed upstream. Exit 10: something changed.
./scripts/kpm --line 7.3-lts status            # Show the decisions for each source.
./scripts/kpm --line 7.3-lts verify --strict   # Check the line. This must pass.
./scripts/kpm --line 7.3-lts cves              # Show the CVEs that the line does not fix.
python3 tools/dashboard/generate.py            # Write the status page for all lines.
make check                                     # Run the static checks and the tests.
./scripts/in-builder.sh noble ./scripts/build-line.sh 7.3-lts noble   # Build for noble.
```

Put `--json` before the command name. Exit code 3 from `import` or `rebase`
means a conflict that a person or agent must resolve.

## Procedures

Each directory in `skills/` has a `SKILL.md` for one job:

| Skill | Use it to |
|---|---|
| `skills/rebase-line` | Move a line to a newer base. |
| `skills/vendor-resync` | Take in new commits from a source, or add a source. |
| `skills/forward-port` | Decide which patches from a source on an older kernel to carry. |
| `skills/userspace-backport` | Add, update or repair a userspace package. |

`docs/workflows.md` has the other procedures.

## Rules

1. Do not edit `series` or the files in `patches/`. `kpm export` writes them.
   To change a patch, change the workbench or the ledger.
2. Give every ledger decision a reason.
3. Do not accept a proposal before you read both diffs. A proposal is a match
   that `kpm` could not prove.
4. Before you carry a patch from a source on an older kernel, make sure that
   upstream did not solve the same problem in a different way. If it did, the
   decision is `superseded`. Use the `forward-port` skill.
5. Keep the kernel tree and the patch series the same for all series. Only
   packaging switches and userspace packages can be different.
6. Do not add `Signed-off-by`. Write a `Conflicts:` note when you change a patch.
7. Work is complete when `kpm verify --strict` passes and the build passes.
   Report which of these you did: compile, boot, test on hardware.

## Build environment

- Kernel work needs Linux and a case-sensitive filesystem.
- `WORK_ROOT` (default `~/kernops`) contains the git repository of all sources,
  the build trees, and the packages.
- Build each series in its container with `scripts/in-builder.sh`.
- For a new series, build in this order:
  1. `dwarves` (the kernel build needs `pahole`)
  2. the kernel
  3. the other userspace packages (`bpfcc` and `bpftrace` use `bpftool` and the
     headers from the kernel build)

## Known problems

- **Do not edit a script or `userspace/matrix.tsv` while a build uses it.** The
  shell reads a script during the run. The build then does the wrong steps.
- **Under WSL, do not run `git` in a checkout on a Windows drive.** Use
  `git -C <path>`, as `kpm` does.
- **Do not put shell variables in `wsl.exe -- bash -c '...'`.** The calling
  shell replaces them. Write a script file and run the file.
- **Do not build under `fakeroot`.** The build is five times slower, and the
  packaging does not need it.
- **Do not fetch the large kernel trees manually.** `kpm fetch` limits what it
  offers to the server. A manual fetch from Launchpad can stop for an hour.
- **If a package builds in a few seconds, examine the `.deb`.** It can be an
  empty metapackage.
