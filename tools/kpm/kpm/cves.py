"""Which kernel CVEs a line is affected by and does not fix (`kpm cves`).

The data is the kernel CVE authority's own repository
(git.kernel.org/pub/scm/linux/security/vulns.git). For each CVE it has a
`.dyad` file with one line per kernel branch:

    <version that introduced it>:<commit>:<version that fixes it>:<commit>

`0:0` on the left means "not known, assume always"; on the right, "not fixed
in that branch". The `.json` record has the title and the affected files.

A CVE is settled for a line when one of these holds:

  not-affected   the flaw was introduced after the line's base
  fixed-in-base  the fix is in the base (mainline before the series, or a
                 stable release at or below the base)
  fixed-by-patch the line carries the fix (a ledger row names the fix commit)

Otherwise it is open, with what would close it:

  rebase         a later release of the same stable series has the fix
  backport       only mainline (or another series) has the fix
  no-fix         no branch has a fix yet

For a line on an Ubuntu kernel the stable level is not a version number:
Canonical applies fixes one by one. There the base's own history is read for
the upstream commits it names, which needs the unified repo.
"""
import json
import re
import subprocess
import tomllib
from pathlib import Path

from . import fingerprint as fp
from . import ledger
from .config import Config, work_root
from .git import Git, git_env

URL = "https://git.kernel.org/pub/scm/linux/security/vulns.git"
OPEN = ("rebase", "backport", "no-fix")
# Severity is the CVSS rating in the record. The kernel CVE authority scores
# only part of its records; "unscored" means no rating, not a low one.
SEVERITIES = ("critical", "high", "medium", "low", "unscored")
_VER = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(-rc\d+)?$")


def checkout() -> Path:
    return work_root() / "vulns"


def update() -> Path:
    """Get or refresh the CVE repository. It is large; only the newest commit is kept."""
    path = checkout()
    if (path / ".git").exists():
        cmds = [["fetch", "-q", "--depth", "1", "origin"], ["reset", "-q", "--hard", "FETCH_HEAD"]]
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        cmds = [["clone", "-q", "--depth", "1", URL, str(path)]]
    for c in cmds:
        cwd = ["-C", str(path)] if c[0] != "clone" else []
        p = subprocess.run(["git", *cwd, *c], env=git_env(), capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(f"git {c[0]} of the CVE repository failed: {p.stderr.strip()}")
    return path


# ── versions ─────────────────────────────────────────────────────────────────

def parse(v: str):
    """('mainline', (major, minor)) or ('stable', (major, minor), patch), or None."""
    m = _VER.match(v.strip())
    if not m:
        return None
    a, b, c, d = (int(x) if x else None for x in m.group(1, 2, 3, 4))
    if a == 2 and c is not None:                 # 2.6.N was a mainline release
        return ("mainline", (a, b * 1000 + c)) if d is None else ("stable", (a, b * 1000 + c), d)
    return ("mainline", (a, b)) if c is None else ("stable", (a, b), c)


class Base:
    """Where a line stands: its series (major, minor) and, for a stable line, its release."""

    def __init__(self, series: tuple, patch: int | None):
        self.series, self.patch = series, patch

    @classmethod
    def of(cls, cfg: Config) -> "Base":
        tag = cfg.base_tag if cfg.kind == "stable" else (cfg.upstream or "")
        p = parse(tag)
        if not p:
            raise ValueError(f"line {cfg.line}: cannot tell the kernel version from '{tag}'")
        return cls(p[1], p[2] if p[0] == "stable" else (0 if cfg.kind == "stable" else None))

    def has(self, version: str) -> bool | None:
        """True if that release is at or below this base; None if it is another series' release."""
        p = parse(version)
        if not p:
            return None
        if p[0] == "mainline":
            return p[1] <= self.series             # an -rc of our series is before its release
        if p[1] != self.series:
            return None
        return self.patch is not None and p[2] <= self.patch


def read_dyad(path: Path) -> list[tuple[str, str, str, str]]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split(":")
        if len(parts) == 4 and not line.startswith("#"):
            out.append(tuple(parts))
    return out


def assess(pairs: list[tuple], base: Base, carried, in_base=lambda sha: False, undone=lambda sha: False,
           fix_undone=None, reverter=lambda sha: "") -> dict:
    """Status of one CVE for one base.

    carried(sha): a patch of the line is that commit. in_base(sha): the base's
    own history names that commit (an Ubuntu base). undone(sha): the base
    reverts that commit. A release number says that a fix went in; it does not
    say that the fix is still there.

    fix_undone(sha): the same question for a fix, with the reverts of all
    history and not only those of the series. It is asked for fixes only: the
    doubtful answer for a fix is "open", and for the commit that brought the
    flaw it would be "not affected".

    reverter(sha): the commit that reverts `sha`, or "". A record can count the
    removal of a driver as the fix, and name the commit that put the driver
    back as a new introducing commit, with its own fixes. That revert does not
    reopen the first flaw: the record judges the code that came back by the
    second one."""
    fix_undone = fix_undone or undone

    def reopened(fix: str) -> bool:
        by = reverter(fix)
        return fix_undone(fix) and not (by and any(by.startswith(i) for i in groups if i != "0"))
    groups: dict[str, list[tuple]] = {}
    for p in pairs:
        groups.setdefault(p[1], []).append(p)
    worst, detail = "not-affected", {}
    rank = ["not-affected", "fixed-in-base", "fixed-by-patch", "rebase", "backport", "no-fix"]
    for intro_sha, group in groups.items():
        # Was the flaw introduced at or below our base? An intro in another series' release
        # is that series' backport of the same commit; the mainline intro decides for us.
        intro = [base.has(p[0]) for p in group if p[0] != "0"]
        if intro and not any(intro):
            # By its release, the flaw came in after our base. A backport can still
            # bring it: a patch of ours, or a commit in the history of an Ubuntu
            # base, that is the introducing commit.
            if intro_sha == "0" or not (carried(intro_sha) or in_base(intro_sha)):
                continue
        # The commit that brought the flaw is reverted, in the base or by a patch of
        # ours: the tree does not have it, so there is nothing to fix. Unless a
        # patch of ours applies that commit again after the base reverted it.
        if intro_sha != "0" and undone(intro_sha) and not carried(intro_sha):
            continue
        # Only the fixes of this introducing commit: a CVE can have several flaws,
        # and the fix of one does not close another.
        fixes = sorted({p[3] for p in group if p[3] != "0"})
        ours = next((p for p in group if p[2] != "0" and (v := parse(p[2])) and v[0] == "stable"
                     and v[1] == base.series), None)
        main = next((p for p in group if p[2] != "0" and (v := parse(p[2])) and v[0] == "mainline"), None)
        released = [p for p in (main, ours) if p and base.has(p[2])]        # the fix is in a release the base has
        reverted = [p for p in released if reopened(p[3])]
        if len(released) > len(reverted) or any(in_base(f) for f in fixes):
            status, why = "fixed-in-base", {}
        elif any(carried(f) for f in fixes):
            status, why = "fixed-by-patch", {}
        elif reverted:
            # The base had the fix and reverts it. Its release is not above the base, so
            # a move to that release does not help: the fix must be carried.
            status, why = "backport", {"fixed_in": reverted[0][2], "fix": reverted[0][3], "reverted_in_base": True}
        elif ours:
            status, why = "rebase", {"fixed_in": ours[2], "fix": ours[3]}
        elif main:
            status, why = "backport", {"fixed_in": main[2], "fix": main[3]}
        elif fixes:
            other = next(p for p in group if p[3] != "0")
            status, why = "backport", {"fixed_in": other[2], "fix": other[3]}
        else:
            status, why = "no-fix", {}
        if rank.index(status) > rank.index(worst):
            worst, detail = status, why
    return {"status": worst, **detail}


# ── what the line has ────────────────────────────────────────────────────────

def _short_match(have: set[str]):
    # A patch can name its upstream commit with fewer than twelve digits (the
    # parser takes eight or more). Those are compared as prefixes; the rest by
    # their first twelve, which is fast for the thousands of commits of a base.
    by12 = {h[:12] for h in have if len(h) >= 12}
    short = sorted({h for h in have if 7 <= len(h) < 12})
    return lambda sha: sha[:12] in by12 or any(sha.startswith(s) for s in short)


def carried_commits(cfg: Config) -> set[str]:
    """Commits the line's patches say they are: the SHA and upstream references of every carried row."""
    out: set[str] = set()
    for name in cfg.line_sources:
        for r in ledger.read(cfg.ledger_path(name)):
            if r["disposition"] in ("carry", "in-base"):
                out.add(r["sha"])
                out.update(u.split()[0] for u in r.get("upstream", []))
                if r.get("base_sha"):
                    out.add(r["base_sha"])
    return out


def _origin(git: Git | None, name: str) -> str:
    """The commit that a workbench commit was made from, or `name` itself (core.origin_of)."""
    from . import core
    return core.origin_of(git, name)


def carried_revert_pairs(cfg: Config, git: Git | None = None) -> list[tuple[str, str]]:
    """(the patch's commit, the commit it reverts) for every carried revert, in the order the patches apply."""
    sources = getattr(cfg, "sources", {})
    names = sorted(cfg.line_sources, key=lambda n: (getattr(sources.get(n), "band", None) or 0))
    return [(r["sha"], _origin(git, r["reverts"])) for name in names for r in ledger.read(cfg.ledger_path(name))
            if r["disposition"] == "carry" and r.get("reverts")]


def _with_upstream(git: Git, commits) -> list[str]:
    """The commits, and the upstream commits that each one says it is."""
    out = list(commits)
    for c in list(out):
        body = git.run("show", "-s", "--format=%b", f"{c}^{{commit}}", check=False)
        out += [u.split()[0] for u in fp.upstream_refs(body) if u.split()[0] not in out]
    return out


def carried_reverts(cfg: Config, git: Git | None = None) -> list[str]:
    """Commits that a carried patch reverts, as that patch names them (7 to 40 hex digits).

    Vendors carry reverts of upstream commits. If the commit is a CVE fix, the
    kernel we ship does not have the fix, whatever the base has.

    The patch can name a backport (a stable or vendor commit), and a CVE record
    names the upstream fix. With `git`, the upstream commits that the reverted
    commit says it is are added."""
    # In the order the patches apply. A later patch can revert an earlier revert:
    # then the commit that the first one removed is back, and neither counts.
    # The revert that is undone does not count either: if it was itself a fix
    # (it removed the commit that brought a flaw), that fix is gone.
    by: dict[str, str] = {}                      # a commit -> the carried patch that reverts it
    for sha, reverts in carried_revert_pairs(cfg, git):
        # Name an earlier revert patch by its full commit, so the chain can be followed.
        target = next((s for s in by.values() if s.startswith(reverts)), reverts)
        by[target] = sha

    def removed(commit: str) -> bool:
        return commit in by and not removed(by[commit])
    out = [c for c in by if removed(c)]
    if git is not None:
        for target in list(out):
            body = git.run("show", "-s", "--format=%b", f"{target}^{{commit}}", check=False)
            out += [u.split()[0] for u in fp.upstream_refs(body) if u.split()[0] not in out]
    return out


def early_reverts(cfg: Config, git: Git, root: str) -> list[tuple[str, str]]:
    """(revert, the commit it names) for each revert in the history of `root`, oldest first.

    That history does not change, and it is long: the list is kept in the cache
    under the commit of `root`."""
    from . import core
    cache = cfg.cache / f"cve-early-reverts-v1-{git.rev(root)[:16]}.json"
    if cache.exists():
        try:
            return [tuple(p) for p in json.loads(cache.read_text())]
        except ValueError:
            cache.unlink()
    pairs = core.revert_pairs(git, root)
    cfg.cache.mkdir(parents=True, exist_ok=True)
    ledger.atomic_write(cache, json.dumps(pairs))
    return pairs


def base_commits(cfg: Config, git: Git) -> set[str] | None:
    """Upstream commits named in the history of an Ubuntu base, from its mainline release on.

    A commit that the base reverts later is left out, with the upstream commits
    it names: a fix that was backported and then reverted is not in the tree.
    The revert itself stays, unless it is reverted too: a revert of the commit
    that brought a flaw is a fix, and can name the upstream fix.

    None if the unified repo does not have that history."""
    root = f"mainline/{cfg.upstream}" if cfg.upstream else ""
    if not root or not git.ok("rev-parse", "--verify", "-q", f"{root}^{{commit}}") \
            or not git.ok("rev-parse", "--verify", "-q", f"{cfg.base_ref}^{{commit}}"):
        return None
    cache = cfg.cache / f"cve-base-v4-{re.sub(r'[^A-Za-z0-9_.-]', '_', cfg.base_tag)}.json"
    if cache.exists():
        try:
            return set(json.loads(cache.read_text()))
        except ValueError:
            cache.unlink()
    from . import core
    reverted = core.reverts_in(git, root, cfg.base_ref)            # {reverted commit: its revert}
    log = git.run("log", "--no-merges", "--format=%H%x00%b%x01", f"{root}..{cfg.base_ref}")
    out: set[str] = set()
    for chunk in log.split("\x01"):
        sha, _, body = chunk.strip("\n").partition("\x00")
        if not sha or any(sha.startswith(t) for t in reverted):
            continue                         # undone in this history
        out.add(sha)
        out.update(u.split()[0] for u in fp.upstream_refs(body))
    ledger.atomic_write(cache, json.dumps(sorted(out)))
    return out


# ── our own assessments ──────────────────────────────────────────────────────

def assessments(cfg: Config, git: Git | None, patch_reverts: list[str] = (),
                restored=()) -> tuple[dict, list[str]]:
    """Entries of kernel/lines/<line>/cves.toml that apply, and why others do not.

    An entry says a CVE that the records report as open does not affect the
    line. It applies only while each `evidence` commit is in the tree we ship:
    in the history of the base, not reverted there, and not reverted by a
    carried patch (patch_reverts, from carried_reverts). A commit that the base
    reverts counts again when a carried patch reverts that revert (restored)."""
    path = cfg.line_dir / "cves.toml"
    if not path.exists():
        return {}, []
    good, problems = {}, []
    for cve, a in tomllib.loads(path.read_text(encoding="utf-8")).items():
        if a.get("status") != "not-affected" or not str(a.get("reason", "")).strip():
            problems.append(f"{cve}: an entry needs status = \"not-affected\" and a reason")
            continue
        missing = []
        for sha in a.get("evidence", []):
            if git is None:
                missing.append(f"{sha} (no unified repo to check)")
            elif not git.ok("merge-base", "--is-ancestor", sha, cfg.base_ref):
                missing.append(sha)
            else:
                from . import core
                # A patch names the commit it reverts with 7 to 40 digits, and so
                # does the entry: compare the two on the full name.
                full = git.rev(str(sha))
                undone = core.reverted_by(git, sha, cfg.base_ref)
                if undone and not any(full.startswith(t) or str(sha).startswith(t) for t in restored):
                    missing.append(f"{sha} (reverted by {undone[:12]})")
                    continue
                if any(full.startswith(t) or str(sha).startswith(t) for t in patch_reverts):
                    missing.append(f"{sha} (reverted by a carried patch)")
        if missing:
            problems.append(f"{cve}: evidence not in {cfg.base_tag}: {', '.join(missing)}")
            continue
        good[cve] = {"reason": " ".join(str(a["reason"]).split()), "evidence": a.get("evidence", [])}
    return good, problems


# ── the report ───────────────────────────────────────────────────────────────

def record(path: Path) -> dict:
    """Title, affected files and publication date from a CVE's JSON record."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cna = d.get("containers", {}).get("cna", {})
    files = sorted({f for a in cna.get("affected", []) for f in a.get("programFiles", [])})
    out = {"title": cna.get("title", ""), "files": files, "severity": "unscored",
           "published": (d.get("cveMetadata", {}).get("datePublished") or "")[:10]}
    best = None
    for m in cna.get("metrics") or []:
        for key, v in m.items():
            if key.startswith("cvss") and isinstance(v, dict) and "baseScore" in v:
                if best is None or v["baseScore"] > best["baseScore"]:
                    best = v
    if best:
        sev = str(best.get("baseSeverity", "")).lower()
        out.update(severity=sev if sev in SEVERITIES else "unscored", score=best["baseScore"],
                   vector=best.get("vectorString", ""))
    return out


def at_least(rows: list[dict], severity: str) -> list[dict]:
    """Rows rated `severity` or worse. "unscored" keeps everything."""
    cut = SEVERITIES.index(severity)
    return [r for r in rows if SEVERITIES.index(r.get("severity", "unscored")) <= cut]


def check(cfg: Config, git: Git | None = None, used_files: set[str] | None = None,
          repo: Path | None = None) -> dict:
    """Assess every published CVE for the line.

    used_files: the source files a build compiled (sbom.used-files.txt). With it,
    each open CVE says whether any affected file was compiled."""
    repo = repo or checkout()
    published = repo / "cve" / "published"
    if not published.is_dir():
        raise FileNotFoundError(f"no CVE records in {repo}; run kpm cves --update")
    base = Base.of(cfg)
    carried = _short_match(carried_commits(cfg))
    in_base, note = (lambda sha: False), ""
    # What the base itself reverts, from the start of its series (a stable line)
    # or from its mainline release (an Ubuntu line).
    undone = early = lambda sha: False
    reverter = lambda sha: ""
    back: set[str] = set()                      # commits that a carried patch puts back, with what they name
    ours_reverted = carried_reverts(cfg, git)
    root = cfg.ref(cfg.series_root) if cfg.kind == "stable" and cfg.series_root else \
        (f"mainline/{cfg.upstream}" if cfg.upstream else "")
    if git and root and git.ok("rev-parse", "--verify", "-q", f"{root}^{{commit}}") \
            and git.ok("rev-parse", "--verify", "-q", f"{cfg.base_ref}^{{commit}}"):
        from . import core
        # One chain for the base and for our patches. A patch can revert a
        # revert of the base, and that revert can be the revert of a revert:
        # what is undone at the end is worked out over all of them together.
        base_only = core.reverts_in(git, root, cfg.base_ref)       # {commit: the commit that reverts it}
        both = core.reverts_in(git, root, cfg.base_ref, extra=carried_revert_pairs(cfg, git))
        # A revert names the commit in this history: a stable or vendor backport.
        # A CVE record names the upstream commit, so that name is undone as well.
        targets = _with_upstream(git, both)
        undone = lambda sha: any(sha.startswith(t) for t in targets)
        # Back because of our patches: not undone, and in the tree again.
        back.update(_with_upstream(git, [t for t in base_only if t not in both]))
        # Gone because of our patches: the commits they revert, and what that brings down.
        # A commit that the base reverts can be applied again by one patch and
        # reverted again by another: it is then gone because of a patch of ours,
        # and the patch that applied it must not count as having it.
        ours_reverted = _with_upstream(git, [t for t in both if both[t] != base_only.get(t)])
        # A fix can be released and reverted before the series starts: its release
        # number is at or below the base, and the tree does not have it. The chain
        # is worked out again with the reverts of all earlier history in front.
        whole = core.reverts_in(git, root, cfg.base_ref, extra=carried_revert_pairs(cfg, git),
                                before=early_reverts(cfg, git, root))
        early = _short_match({t for t in whole if t not in both})
        reverter = lambda sha: next((by for t, by in whole.items() if sha.startswith(t)), "")
    elif cfg.kind == "stable":
        note = (f"The unified repo does not have {cfg.base_tag}: a fix that the base reverts cannot be seen. "
                "Run kpm fetch.")
    if cfg.kind != "stable":
        extra = base_commits(cfg, git) if git else None
        if extra is None:
            note = (f"{cfg.base_tag} is an Ubuntu kernel: the fixes Canonical applied are only visible in its "
                    "history, and the unified repo does not have it (run kpm fetch). Open counts are too high.")
        else:
            in_base = _short_match(extra | back)

    # The patch series can revert a fix too. Such a commit is not there, whether
    # the base, its history or another patch of ours is the evidence for it.
    if ours_reverted:
        def gone(sha):
            return any(sha.startswith(t) for t in ours_reverted)
        base_undone, base_carried, base_in = undone, carried, in_base
        undone = lambda sha: base_undone(sha) or gone(sha)
        carried = lambda sha: base_carried(sha) and not gone(sha)
        in_base = lambda sha: base_in(sha) and not gone(sha)

    now_undone = undone
    fix_undone = lambda sha: now_undone(sha) or early(sha)

    ours, problems = assessments(cfg, git, ours_reverted, back)
    counts = {k: 0 for k in ("not-affected", "fixed-in-base", "fixed-by-patch", "assessed", *OPEN)}
    open_rows, assessed, unused = [], [], set(ours)
    for dyad in sorted(published.glob("*/*.dyad")):
        pairs = read_dyad(dyad)
        if not pairs:
            continue
        res = assess(pairs, base, carried, in_base, undone, fix_undone, reverter)
        unused.discard(dyad.stem)
        if res["status"] in OPEN and dyad.stem in ours:
            # The record says open; we examined it and recorded why it is not.
            counts["assessed"] += 1
            assessed.append({"cve": dyad.stem, "record_status": res["status"], **ours[dyad.stem],
                             **{k: v for k, v in record(dyad.with_suffix(".json")).items() if k in ("title", "severity", "score")}})
            continue
        counts[res["status"]] += 1
        if res["status"] in OPEN:
            row = {"cve": dyad.stem, **res, **record(dyad.with_suffix(".json"))}
            if used_files is not None and row.get("files"):
                row["compiled"] = any(f in used_files for f in row["files"])
            open_rows.append(row)
    order = {s: i for i, s in enumerate(OPEN)}
    # Worst first; code that this build does not compile goes last.
    open_rows.sort(key=lambda r: (r.get("compiled") is False, SEVERITIES.index(r.get("severity", "unscored")),
                                  -r.get("score", 0), order[r["status"]], r["cve"]))
    by_severity = {s: sum(1 for r in open_rows if r.get("severity", "unscored") == s) for s in SEVERITIES}
    # An entry for a CVE that the records no longer report as open is out of date.
    problems += [f"{c}: the records do not report it as open; remove the entry" for c in sorted(unused)]
    return {"line": cfg.line, "base": cfg.base_tag, "records": sum(counts.values()), "counts": counts,
            "open_by_severity": by_severity, "open": open_rows, "assessed": assessed,
            "assessment_problems": problems, "note": note}
