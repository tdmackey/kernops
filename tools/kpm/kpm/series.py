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
from pathlib import Path

from . import fingerprint as fp
from . import ledger
from .config import Config
from .core import _PACKAGING
from .git import Git, GitError

_IDENT = {"GIT_COMMITTER_NAME": "kpm", "GIT_COMMITTER_EMAIL": "kpm@localhost"}


def slug(subject: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fp.subject_key(subject)).strip("-")
    return s[:52].rstrip("-") or "patch"


def branch(cfg: Config) -> str:
    return f"work/{cfg.line}"


def workdir(cfg: Config) -> Path:
    return Path(os.environ.get("KPM_WORK", Path.home() / "kernops" / "work" / cfg.line))


def plan(cfg: Config) -> list[tuple[str, int, dict]]:
    """Carry rows in apply order: bands ascending, then each source's own order."""
    out = []
    banded = sorted(((s.band, n) for n, s in cfg.sources.items() if s.band is not None))
    for band, name in banded:
        out += [(name, band, r) for r in ledger.read(cfg.ledger_path(name)) if r["disposition"] == "carry"]
    return out


def _run(wt: Path, *args: str, env: dict | None = None, input: str | None = None):
    return subprocess.run(["git", "-C", str(wt), *args], env=dict(os.environ, **_IDENT, **(env or {})),
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


def _decide_empty(cfg: Config, source: str, row: dict) -> None:
    """A carry row that changes nothing once packaging paths are stripped."""
    rows = ledger.read(cfg.ledger_path(source))
    for r in rows:
        if r["sha"] == row["sha"]:
            r.pop("decided_by", None)
            if r.get("touches_packaging"):
                r["disposition"] = "packaging"
            else:
                r.update(disposition="dropped", reason="empty on import: content already in the tree")
    ledger.write(cfg.ledger_path(source), rows)


def _state_file(cfg: Config) -> Path:
    return cfg.cache / f"import-{cfg.line}.json"


def _on_branch(cfg: Config, wt: Path) -> set[str]:
    out = _run(wt, "log", "--format=%(trailers:key=Ledger-Change,valueonly)", f"{cfg.base_ref}..HEAD")
    return set(out.stdout.decode().split())


def import_(cfg: Config, git: Git, cont: bool = False) -> dict:
    wt, br, state = workdir(cfg), branch(cfg), _state_file(cfg)
    res = {"branch": br, "worktree": str(wt), "applied": 0, "from_patch": 0, "picked": 0,
           "empty": [], "conflict": None}

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
            _commit(wt, pending["source"], pending["row"])
            state.unlink()
            res["picked"] += 1
    else:
        git.run("worktree", "remove", "--force", str(wt), check=False)
        git.run("worktree", "prune")
        git.run("worktree", "add", "-q", "-B", br, str(wt), cfg.base_ref)
        _run(wt, "config", "rerere.enabled", "true")
        _run(wt, "config", "rerere.autoupdate", "true")
        state.unlink(missing_ok=True)

    done = _on_branch(cfg, wt)
    patches = cfg.line_dir / "patches"
    for source, _band, row in plan(cfg):
        if row["change"] in done:
            continue
        pfile = patches / row["patch"] if row.get("patch") else None
        if pfile and pfile.exists():
            if _run(wt, "am", "-q", "--3way", "--keep-cr", str(pfile)).returncode == 0:
                res["from_patch"] += 1
                res["applied"] += 1
                continue
            _run(wt, "am", "--abort")
        p = _run(wt, "cherry-pick", "-n", row["sha"])
        if p.returncode and (left := _real_conflicts(wt)):
            cfg.cache.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"source": source, "row": row}))
            res["conflict"] = {"source": source, "change": row["change"], "sha": row["sha"],
                               "subject": row["subject"], "files": left,
                               "hint": f"resolve in {wt}, git add, then kpm import --continue"}
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


def export(cfg: Config, git: Git) -> dict:
    wt = workdir(cfg)
    patches = cfg.line_dir / "patches"
    ledgers = {n: ledger.read(cfg.ledger_path(n)) for n, s in cfg.sources.items() if s.band is not None}
    by_change = {r["change"]: (n, r) for n, rows in ledgers.items() for r in rows
                 if r["disposition"] == "carry" and r.get("change")}

    log = _run(wt, "log", "--reverse", "--format=%H %(trailers:key=Ledger-Change,valueonly,separator=%x20)",
               f"{cfg.base_ref}..HEAD").stdout.decode().splitlines()
    written: set[Path] = set()
    series, counters, unknown, adapted = [], {}, [], 0
    for line in log:
        sha, _, change = line.partition(" ")
        change = change.strip()
        if change not in by_change:
            unknown.append(sha[:12])
            continue
        name, row = by_change[change]
        band = cfg.sources[name].band
        n = counters[name] = counters.get(name, 0) + 1
        rel = f"{band:04d}-{name}/{band + n:04d}-{slug(row['subject'])}.patch"
        text = _run(wt, "format-patch", "-1", "--stdout", "--zero-commit", "--no-signature",
                    "-N", "--no-renames", sha).stdout.decode("utf-8", errors="surrogateescape")
        out = patches / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
        written.add(out)
        series.append(rel)
        row["patch"] = rel
        if fp.delta(text)[0] != row.get("delta_id"):
            row["adapted"] = True
            adapted += 1
        else:
            row.pop("adapted", None)

    stale = [p for p in patches.glob("*/*.patch") if p not in written] if patches.exists() else []
    for p in stale:
        p.unlink()
    with open(cfg.line_dir / "series", "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(s + "\n" for s in series))
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
