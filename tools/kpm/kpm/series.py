"""import and export: ledger decisions <-> workbench branch <-> patch files.

  import   ledger `carry` rows  ->  commits on branch work/<line>   (needs a worktree)
  export   commits on the branch ->  patches/<band>-<source>/NNNN-slug.patch + series

Every imported commit carries `Ledger-Change:` and `Ledger-Origin:` trailers, so a
commit maps back to its ledger row exactly. Commits that only touch packaging are
never imported; packaging paths are stripped from mixed commits.
"""
import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

from . import fingerprint as fp
from . import ledger
from .config import Config
from .core import _PACKAGING
from .git import Git, GitError, git_env

_IDENT = {"GIT_COMMITTER_NAME": "kpm", "GIT_COMMITTER_EMAIL": "kpm@localhost"}


def slug(subject: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fp.subject_key(subject)).strip("-")
    return s[:52].rstrip("-") or "patch"


def branch(cfg: Config) -> str:
    return f"work/{cfg.line}"


def workdir(cfg: Config) -> Path:
    return Path(os.environ.get("KPM_WORK", cfg.repo.parent / "work" / cfg.line))


def plan(cfg: Config) -> list[tuple[str, int, dict]]:
    """Carry rows in apply order: bands ascending, then each source's own order."""
    out = []
    banded = sorted((cfg.sources[n].band, n) for n in cfg.line_sources if cfg.sources[n].band is not None)
    for band, name in banded:
        out += [(name, band, r) for r in ledger.read(cfg.ledger_path(name)) if r["disposition"] == "carry"]
    return out


def _run(wt: Path, *args: str, env: dict | None = None, input: str | None = None):
    return subprocess.run(["git", "-C", str(wt), *args], env=git_env({**_IDENT, **(env or {})}),
                          input=input.encode() if input is not None else None, capture_output=True)


def _unmerged(wt: Path) -> list[str]:
    return _run(wt, "diff", "--name-only", "--diff-filter=U").stdout.decode().split()


def _restore(wt: Path, paths) -> None:
    """Put paths back to their HEAD state (or absent), clearing any conflict on them."""
    for path in paths:
        _run(wt, "reset", "-q", "HEAD", "--", path)
        if _run(wt, "cat-file", "-e", f"HEAD:{path}").returncode == 0:
            _run(wt, "checkout", "-q", "--", path)
        else:
            (wt / path).unlink(missing_ok=True)


def _strip_packaging(wt: Path) -> None:
    staged = _run(wt, "diff", "--cached", "--name-only").stdout.decode().split("\n")
    _restore(wt, filter(_PACKAGING.match, staged))


def _real_conflicts(wt: Path) -> list[str]:
    """Unmerged paths that matter. Conflicts in packaging paths are discarded:
    those paths are never part of the series."""
    left = _unmerged(wt)
    _restore(wt, [p for p in left if _PACKAGING.match(p)])
    return [p for p in left if not _PACKAGING.match(p)]


def _commit(wt: Path, source: str, row: dict) -> None:
    sha = row["sha"]
    meta = _run(wt, "log", "-1", "--format=%an%x00%ae%x00%aI", sha).stdout.decode().strip().split("\x00")
    body = _run(wt, "log", "-1", "--format=%B", sha).stdout.decode()
    msg = _run(wt, "interpret-trailers", "--trailer", f"Ledger-Change: {row['change']}",
               "--trailer", f"Ledger-Origin: {source}@{sha}", input=body).stdout.decode()
    p = _run(wt, "commit", "-q", "--no-verify", "-F", "-", input=msg,
             env={"GIT_AUTHOR_NAME": meta[0], "GIT_AUTHOR_EMAIL": meta[1], "GIT_AUTHOR_DATE": meta[2]})
    if p.returncode:
        raise GitError(f"commit of {sha[:12]} failed: {p.stderr.decode().strip()}")


_PACKAGING_NOTE = ("the changes to debian*/ in the original commit are left out: "
                   "packaging and config are generated, not part of the patch series")


def conflicts_note(row: dict) -> str:
    """What we changed in this patch, for its message. "" if it is the vendor's commit unchanged.

    A row's `conflicts` field holds what a person changed to resolve a conflict.
    A commit that also touched packaging paths gets a standard note."""
    parts = [str(row["conflicts"]).strip()] if row.get("conflicts") else []
    if row.get("touches_packaging"):
        parts.append(_PACKAGING_NOTE)
    return "\n".join(parts)


def with_conflicts(text: str, note: str) -> str:
    """Put a `Conflicts:` paragraph in a patch's message, before our trailers. Does nothing if there is one."""
    head, sep, diff = text.partition("\n---\n")
    if not note or not sep or "\nConflicts:\n" in head:
        return text
    marker = "\nLedger-Change: "
    i = head.rfind(marker)
    if i < 0:
        return text
    lines = []
    for para in note.split("\n"):
        lines += textwrap.wrap(para, 68, initial_indent="\t", subsequent_indent="\t  ") or [""]
    return head[:i].rstrip("\n") + "\n\nConflicts:\n" + "\n".join(lines) + "\n" + head[i:] + sep + diff


_PLACE_NOTE = ("the changed lines are the vendor's; the code next to them is not the same as in the "
               "vendor's tree (the base or an earlier patch of the series changed it)")


def _decide_empty(cfg: Config, source: str, row: dict, landed: str = "", base_sha: str = "") -> None:
    """A carry row that has nothing left to apply.

    With `landed`, its content is already in the base (found while importing a
    patch file). Otherwise it emptied out once packaging paths were stripped."""
    rows = ledger.read(cfg.ledger_path(source))
    for r in rows:
        if r["sha"] == row["sha"]:
            r.pop("decided_by", None)
            if landed:
                # What landed is our patch file. If we had changed it by hand, the
                # vendor's commit is not the same change: keep the file, so a later
                # base move can check the change that was really there.
                pfile = cfg.line_dir / "patches" / r["patch"] if r.get("patch") else None
                if pfile and pfile.exists() and (r.get("adapted") or r.get("conflicts")) and r.get("change"):
                    keep = cfg.line_dir / "landed" / f"{r['change']}.patch"
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    ledger.atomic_write(keep, pfile.read_text(encoding="utf-8", errors="surrogateescape"))
                    r["landed_patch"] = f"landed/{r['change']}.patch"
                r.pop("patch", None)
                r.update(disposition="in-base", base_sha=base_sha, landed=landed, match="content-present")
            elif r.get("touches_packaging"):
                r["disposition"] = "packaging"
            else:
                r.update(disposition="dropped", reason="empty on import: content already in the tree")
    ledger.write(cfg.ledger_path(source), rows)


def _state_file(cfg: Config) -> Path:
    return cfg.cache / f"import-{cfg.line}.json"


def _on_branch(cfg: Config, wt: Path) -> set[str]:
    out = _run(wt, "log", "--format=%(trailers:key=Ledger-Change,valueonly)", f"{cfg.base_ref}..HEAD")
    return set(out.stdout.decode().split())


def import_(cfg: Config, git: Git, cont: bool = False, conflicts: str = "") -> dict:
    wt, br, state = workdir(cfg), branch(cfg), _state_file(cfg)
    res = {"branch": br, "worktree": str(wt), "applied": 0, "from_patch": 0, "picked": 0,
           "empty": [], "landed": [], "conflict": None}

    if cont:
        pending = json.loads(state.read_text()) if state.exists() else None
        if pending:
            left = _unmerged(wt)
            if left:
                res["conflict"] = {**pending, "files": left, "hint": "resolve, git add, then kpm import --continue"}
                return res
            _run(wt, "add", "-u")
            if pending["row"].get("touches_packaging"):
                _strip_packaging(wt)
            if conflicts.strip():
                # What the person changed. It goes in the ledger row, and from
                # there into the patch message at export.
                rows = ledger.read(cfg.ledger_path(pending["source"]))
                for r in rows:
                    if r["sha"] == pending["row"]["sha"]:
                        r["conflicts"] = conflicts.strip()
                ledger.write(cfg.ledger_path(pending["source"]), rows)
            if _run(wt, "diff", "--cached", "--quiet").returncode == 0:
                # Resolved to "nothing left to apply".
                res["empty"].append({"source": pending["source"], "change": pending["row"]["change"],
                                     "subject": pending["row"]["subject"]})
                _decide_empty(cfg, pending["source"], pending["row"])
            else:
                _commit(wt, pending["source"], pending["row"])
                res["picked"] += 1
            state.unlink()
    else:
        git.run("worktree", "remove", "--force", str(wt), check=False)
        git.run("worktree", "prune")
        git.run("worktree", "add", "-q", "-B", br, str(wt), cfg.base_ref)
        _run(wt, "config", "rerere.enabled", "true")
        _run(wt, "config", "rerere.autoupdate", "true")
        state.unlink(missing_ok=True)

    done = _on_branch(cfg, wt)
    patches = cfg.line_dir / "patches"
    base_sha = git.rev(cfg.base_ref)
    # The base alone, to tell "the base has this" from "an earlier patch has this".
    cfg.cache.mkdir(parents=True, exist_ok=True)
    base_env = {"GIT_INDEX_FILE": str(cfg.cache / f"import-base-{cfg.line}.index")}
    if git.call("read-tree", cfg.base_ref, env=base_env).returncode:
        raise GitError(f"cannot read base {cfg.base_ref}")
    for source, _band, row in plan(cfg):
        if not row.get("change"):
            raise GitError(f"{source}:{row['sha'][:12]} is carried but has no change id; run kpm resolve {source}")
        if row["change"] in done:
            continue
        have_commit = git.ok("cat-file", "-e", f"{row['sha']}^{{commit}}")
        pfile = patches / row["patch"] if row.get("patch") else None
        if pfile is None and row.get("landed_patch"):
            # An adapted patch that had landed in an earlier base and is carried
            # again: what we carried is the diff that was kept then, not the
            # vendor's commit.
            pfile = cfg.line_dir / row["landed_patch"]
        pname = row.get("patch") or row.get("landed_patch") or ""
        if pfile and pfile.exists():
            # Is the change already here? "Here" is the worktree: the base plus the
            # patches before this one. The test must be made there. Against the base
            # alone, a patch that removes what an earlier patch added would look
            # present (its reverse, adding the files, applies to a tree without them).
            if _run(wt, "apply", "--check", "--reverse", str(pfile)).returncode == 0:
                if git.call("apply", "--cached", "--check", "--reverse", str(pfile.resolve()),
                            env=base_env).returncode == 0:
                    # The base has it.
                    res["landed"].append({"source": source, "change": row["change"], "subject": row["subject"]})
                    _decide_empty(cfg, source, row, landed=cfg.base_tag, base_sha=base_sha)
                else:
                    # An earlier patch of the series has it, not the base.
                    res["empty"].append({"source": source, "change": row["change"], "subject": row["subject"]})
                    _decide_empty(cfg, source, row)
                continue
            if _run(wt, "am", "-q", "--3way", "--keep-cr", str(pfile)).returncode == 0:
                res["from_patch"] += 1
                res["applied"] += 1
                continue
            _run(wt, "am", "--abort")
        if not have_commit:
            what = f"{pname} does not apply and" if pfile and pfile.exists() else "no patch file and"
            res["conflict"] = {"source": source, "change": row["change"], "sha": row["sha"],
                               "subject": row["subject"], "files": [],
                               "hint": f"{what} commit {row['sha'][:12]} is not in the unified repo; "
                                       "fetch its source (kpm fetch), then run kpm import again"}
            return res
        p = _run(wt, "cherry-pick", "-n", row["sha"])
        if p.returncode not in (0, 1):          # 1 = conflicts; anything else is git failing
            raise GitError(f"cherry-pick of {row['sha'][:12]} failed: {p.stderr.decode(errors='replace').strip()}")
        left = _real_conflicts(wt) if p.returncode else []
        # A patch we changed by hand no longer applies. The vendor's original is
        # now in the worktree, but it is not what we carried: a person compares.
        lost_adaptation = bool(row.get("adapted") and pfile and pfile.exists())
        if left or lost_adaptation:
            ledger.atomic_write(state, json.dumps({"source": source, "row": row}))
            hint = f"resolve in {wt}, git add, then kpm import --continue"
            if lost_adaptation:
                hint = (f"{pname} was adapted by hand and no longer applies; the vendor's original is "
                        f"staged in {wt}. Redo the adaptation there, git add, then kpm import --continue")
            res["conflict"] = {"source": source, "change": row["change"], "sha": row["sha"],
                               "subject": row["subject"], "files": left, "hint": hint}
            return res
        if row.get("touches_packaging"):
            _strip_packaging(wt)
        if _run(wt, "diff", "--cached", "--quiet").returncode == 0:
            _run(wt, "reset", "-q", "--hard")
            res["empty"].append({"source": source, "change": row["change"], "subject": row["subject"]})
            _decide_empty(cfg, source, row)
            continue
        _commit(wt, source, row)
        res["picked"] += 1
        res["applied"] += 1
    return res


def band_scale(bands: dict[str, int], counts: dict[str, int]) -> int:
    """The factor (1, 10, 100, ...) that gives every band room for its patches.

    A patch number says which source the patch is from: 2000 to 2499 is the
    source with band 2000 when the next band is 2500. A source with more
    patches than its band has room for would run into the next band. The
    numbers get one more digit then, for the whole line: band 2500 becomes
    25000, with ten times the room. Nothing depends on the width."""
    order = sorted((band, name) for name, band in bands.items())
    for (band, name), (after, other) in zip(order, order[1:]):
        if band == after:
            raise GitError(f"sources {name} and {other} have the same band {band}")
    scale = 1
    while any(band * scale + counts.get(name, 0) >= after * scale
              for (band, name), (after, _other) in zip(order, order[1:])):
        scale *= 10
    return scale


def export(cfg: Config, git: Git) -> dict:
    wt = workdir(cfg)
    patches = cfg.line_dir / "patches"
    ledgers = {n: ledger.read(cfg.ledger_path(n)) for n in cfg.line_sources if cfg.sources[n].band is not None}
    by_change = {r["change"]: (n, r) for n, rows in ledgers.items() for r in rows
                 if r["disposition"] == "carry" and r.get("change")}

    if _state_file(cfg).exists():
        raise GitError("an import is stopped on a conflict; finish it with kpm import --continue before exporting")
    p = _run(wt, "log", "--reverse", "--format=%H %(trailers:key=Ledger-Change,valueonly,separator=%x20)",
             f"{cfg.base_ref}..HEAD")
    if p.returncode:
        raise GitError(f"cannot read the workbench branch in {wt} (run kpm import first): "
                       f"{p.stderr.decode(errors='replace').strip()}")
    log = p.stdout.decode().splitlines()
    on_branch = {l.partition(" ")[2].strip() for l in log}
    lost = [r["patch"] for _n, r in by_change.values() if r.get("patch") and r["change"] not in on_branch]
    if lost:
        # Exporting now would delete these patch files.
        raise GitError(f"{len(lost)} carried patches are not on the workbench branch (first: {lost[0]}); "
                       "run kpm import first")
    written: set[Path] = set()
    series, counters, unknown, adapted = [], {}, [], 0
    unexplained: list[str] = []
    planned: list[tuple] = []           # (path, text, row, changed): written only when all of them are valid
    # A patch number says which source the patch is from. That holds only while
    # no band runs into the next one: check before anything is written.
    per_source: dict[str, int] = {}
    for line in log:
        change = line.partition(" ")[2].strip()
        if change in by_change:
            per_source[by_change[change][0]] = per_source.get(by_change[change][0], 0) + 1
    scale = band_scale({n: cfg.sources[n].band for n in per_source}, per_source)
    width = 4 + len(str(scale)) - 1
    for line in log:
        sha, _, change = line.partition(" ")
        change = change.strip()
        if change not in by_change:
            unknown.append(sha[:12])
            continue
        name, row = by_change[change]
        band = cfg.sources[name].band
        n = counters[name] = counters.get(name, 0) + 1
        band *= scale
        rel = f"{band:0{width}d}-{name}/{band + n:0{width}d}-{slug(row['subject'])}.patch"
        text = _run(wt, "format-patch", "-1", "--stdout", "--zero-commit", "--no-signature",
                    "-N", "--no-renames", sha).stdout.decode("utf-8", errors="surrogateescape")
        note = conflicts_note(row)
        if git.ok("cat-file", "-e", f"{row['sha']}^{{commit}}"):
            # Compare with the commit itself, not with the id that the row holds:
            # the same diff, made the same way as the patch.
            original = git.run("show", "--format=", "--no-renames", row["sha"])
            # The text of the change, and the mode of each file: a script that lost
            # its executable bit is a changed patch with the same text.
            changed = fp.delta(text)[0] != fp.delta(original)[0] or fp.modes(text) != fp.modes(original)
            # A binary file has no lines to compare: compare the file itself, as
            # the workbench commit and the original commit leave it.
            for path in ([] if changed else fp.binary_files(text)):
                ours = git.run("rev-parse", "--verify", "-q", f"{sha}:{path}", check=False).strip()
                theirs = git.run("rev-parse", "--verify", "-q", f"{row['sha']}:{path}", check=False).strip()
                changed = changed or ours != theirs
            if not changed and fp.places(text) != fp.places(original):
                # The same lines at another place: in another function of the file,
                # or next to code that the vendor's tree does not have. delta_id
                # does not see that; the lines around each change do.
                changed = True
                note = note or _PLACE_NOTE
        else:
            changed = fp.delta(text)[0] != row.get("delta_id")
        if changed and not note:
            # We changed what the patch does and nothing says how. The message of a
            # changed patch must say it (AGENTS.md, rule 6).
            unexplained.append(f"{row['change']} {row['subject']}")
        if changed:
            text = with_conflicts(text, note)
        planned.append((rel, text, row, changed))

    if unexplained:
        # Nothing was written, removed or changed up to here: the patch files, the
        # series file and the ledgers still are the last complete export.
        raise GitError(f"{len(unexplained)} patches differ from their original commit and have no Conflicts note "
                       f"(first: {unexplained[0]}). Say what was changed: "
                       "kpm export --conflicts C-NNNNN='<file>: <what and why>'")
    for rel, text, row, changed in planned:
        out = patches / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
        written.add(out)
        series.append(rel)
        row["patch"] = rel
        if row.get("landed_patch"):
            # Carried again: the patch file is the record now.
            (cfg.line_dir / row.pop("landed_patch")).unlink(missing_ok=True)
        if changed:
            row["adapted"] = True
            adapted += 1
        else:
            row.pop("adapted", None)
    stale = [p for p in patches.glob("*/*.patch") if p not in written] if patches.exists() else []
    for p in stale:
        p.unlink()
    for d in (patches.iterdir() if patches.exists() else []):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()                       # a band that changed width, or lost its last patch
    ledger.atomic_write(cfg.line_dir / "series", "".join(s + "\n" for s in series))
    for n, rows in ledgers.items():
        if rows:
            ledger.write(cfg.ledger_path(n), rows)
    return {"patches": len(series), "adapted": adapted, "removed_stale": len(stale),
            "unknown_commits": unknown, "per_source": counters}


def check_applies(cfg: Config, git: Git) -> list[str]:
    """Apply the series to the base in a throwaway index. No worktree needed."""
    series = cfg.line_dir / "series"
    if not series.exists():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(git.env, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        base = ["git", "-C", str(git.repo)]
        if subprocess.run([*base, "read-tree", cfg.base_ref], env=env, capture_output=True).returncode:
            return [f"cannot read base {cfg.base_ref}"]
        for rel in series.read_text().split():
            p = subprocess.run([*base, "apply", "--cached", str(cfg.line_dir / "patches" / rel)],
                               env=env, capture_output=True)
            if p.returncode:
                return [f"series stops applying at {rel}: {p.stderr.decode().strip().splitlines()[0]}"]
    return []


def apply(cfg: Config, git: Git, dest: Path) -> dict:
    """Materialise base + series as a plain source tree in `dest`. No git history,
    no worktree: this is what a build consumes."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"kpm: {dest} exists and is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    ar = subprocess.Popen(["git", "-C", str(git.repo), "archive", cfg.base_ref], env=git.env, stdout=subprocess.PIPE)
    rc = subprocess.run(["tar", "-x"], cwd=dest, stdin=ar.stdout).returncode
    ar.stdout.close()
    if ar.wait() or rc:
        raise SystemExit(f"kpm: could not extract {cfg.base_ref}")
    series_file = cfg.line_dir / "series"
    rels = series_file.read_text().split() if series_file.exists() else []
    # Keep git from treating `dest` as part of an enclosing repository.
    env = git_env({"GIT_CEILING_DIRECTORIES": str(dest.resolve().parent)})
    for rel in rels:
        p = subprocess.run(["git", "apply", "--whitespace=nowarn", str((cfg.line_dir / "patches" / rel).resolve())],
                           cwd=dest, env=env, capture_output=True)
        if p.returncode:
            raise SystemExit(f"kpm: {rel} does not apply: {p.stderr.decode(errors='replace').strip().splitlines()[0]}")
    return {"tree": str(dest), "base": cfg.base_tag, "patches": len(rels)}


def rebase(cfg: Config, git: Git, new_tag: str) -> dict:
    """Move the line to a new base tag.

    1. Carried patches whose content landed between the old and new base are
       marked in-base (by upstream reference, patch-id or delta-id).
    2. The workbench is rebuilt on the new base from the remaining patches; a
       patch whose content turns out to be present is marked in-base too.
    3. On success the series is re-exported. On a conflict the new base stays
       recorded and `kpm import --continue` then `kpm export` finish the job.

    Before that, every `in-base` row is checked against the new base. A row
    that no longer holds (the new base reverts the commit, or does not contain
    it) becomes undecided, and `kpm verify --strict` fails until it is decided.
    """
    from . import core
    old = cfg.base_tag
    if new_tag == old:
        return {"old": old, "new": new_tag, "unchanged": True}
    if not git.ok("rev-parse", "--verify", "-q", f"{cfg.ref(new_tag)}^{{commit}}"):
        raise GitError(f"{cfg.ref(new_tag)} is not in the unified repo; run kpm fetch {cfg.base_source_name}")
    wt = workdir(cfg)
    old_tip = git.run("rev-parse", "--verify", "-q", branch(cfg), check=False).strip()

    idx = core.index_range(cfg, git, old, new_tag)
    # If anything below fails outright (not a conflict), the line goes back as it was.
    keep = [cfg.line_dir / "line.toml", *(cfg.ledger_path(n) for n in cfg.line_sources)]
    before = {p: p.read_text(encoding="utf-8") for p in keep if p.exists()}

    def undo():
        for p, text in before.items():
            ledger.atomic_write(p, text)
        cfg.base_tag = old

    # A row that says "the base has this" was decided for the old base. The new
    # base can revert the commit, or not contain it at all. Such a row goes back
    # to undecided with a proposal, so a person reads it: the change is then in
    # no patch and in no base, and nothing else would say so.
    # Read the reverts from the start of the base's series, not from the old base:
    # the new base can revert a revert that is older than the old base, and that
    # brings back the first link of the chain.
    start = cfg.ref(cfg.series_root) if cfg.kind == "stable" and cfg.series_root else \
        (f"mainline/{cfg.upstream}" if cfg.upstream else "")
    if not start or not git.ok("rev-parse", "--verify", "-q", f"{start}^{{commit}}"):
        start = cfg.ref(old)
    reverted = core.reverts_in(git, start, cfg.ref(new_tag))
    landed, invalidated = [], []
    for name in cfg.line_sources:
        rows = ledger.read(cfg.ledger_path(name))
        for r in rows:
            if r["disposition"] == "in-base":
                why = core.still_in_base(cfg, git, r, cfg.ref(old), cfg.ref(new_tag), reverted)
                if why:
                    invalidated.append({"source": name, "change": r.get("change"), "subject": r["subject"], "why": why})
                    r["proposed"] = {"disposition": "in-base", "match": r.get("match", ""),
                                     "base_sha": r.get("base_sha", ""), "landed": r.get("landed", ""),
                                     "note": f"was in {old}; in {new_tag} {why}. Carry it, or decide again."}
                    for k in ("base_sha", "landed", "match", "decided_by"):
                        r.pop(k, None)
                    r["disposition"] = "undecided"
                continue
            if r["disposition"] != "carry":
                continue
            hit = core.match_base(r, idx)
            # Same changed lines alone is not proof. If the content really landed,
            # the import below finds it: the patch file applies in reverse to the base.
            # A patch that we changed is not the commit that its fingerprints
            # describe: the new base can have the vendor's original and not what
            # we added. It is left to the import too, which tests the patch file
            # itself and stops for a person if it does not fit.
            if hit and hit[2] != "delta-id" and not (r.get("adapted") or r.get("conflicts")):
                landed.append({"source": name, "change": r.get("change"), "subject": r["subject"],
                               "base_sha": hit[0], "landed": hit[1], "match": hit[2]})
                r.pop("patch", None)
                r.pop("adapted", None)
                r.update(disposition="in-base", base_sha=hit[0], landed=hit[1], match=hit[2])
        if rows:
            ledger.write(cfg.ledger_path(name), rows)

    try:
        cfg.set_base(new_tag)
        res = import_(cfg, git)
    except BaseException:
        undo()
        raise
    if res["conflict"] and not res["conflict"]["files"] and "adapted by hand" not in res["conflict"]["hint"]:
        undo()                                  # import could not start that patch at all
        raise GitError(res["conflict"]["hint"])
    landed += [dict(x, landed=new_tag, match="content-present") for x in res["landed"]]
    out = {"old": old, "new": new_tag, "landed": landed, "invalidated": invalidated, "empty": res["empty"],
           "conflict": res["conflict"], "applied": res["applied"]}
    if res["conflict"]:
        return out
    out["export"] = export(cfg, git)
    if old_tip:
        rd = _run(wt, "range-diff", f"{cfg.ref(old)}..{old_tip}", f"{cfg.ref(new_tag)}..HEAD")
        report = cfg.cache / f"range-diff-{cfg.line}.txt"
        cfg.cache.mkdir(parents=True, exist_ok=True)
        report.write_bytes(rd.stdout)
        out["range_diff"] = str(report)
    return out
