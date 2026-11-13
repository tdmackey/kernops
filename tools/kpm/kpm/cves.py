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


def assess(pairs: list[tuple], base: Base, carried, in_base=lambda sha: False) -> dict:
    """Status of one CVE for one base.

    carried(sha): a patch of the line is that commit. in_base(sha): the base's
    own history names that commit (an Ubuntu base)."""
    fixes = sorted({p[3] for p in pairs if p[3] != "0"})
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
            continue
        ours = next((p for p in group if p[2] != "0" and (v := parse(p[2])) and v[0] == "stable"
                     and v[1] == base.series), None)
        main = next((p for p in group if p[2] != "0" and (v := parse(p[2])) and v[0] == "mainline"), None)
        if (main and base.has(main[2])) or (ours and base.has(ours[2])) or any(in_base(f) for f in fixes):
            status, why = "fixed-in-base", {}
        elif any(carried(f) for f in fixes):
            status, why = "fixed-by-patch", {}
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
    by12 = {h[:12] for h in have}
    return lambda sha: sha[:12] in by12


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


def base_commits(cfg: Config, git: Git) -> set[str] | None:
    """Upstream commits named in the history of an Ubuntu base, from its mainline release on.

    None if the unified repo does not have that history."""
    root = f"mainline/{cfg.upstream}" if cfg.upstream else ""
    if not root or not git.ok("rev-parse", "--verify", "-q", f"{root}^{{commit}}") \
            or not git.ok("rev-parse", "--verify", "-q", f"{cfg.base_ref}^{{commit}}"):
        return None
    cache = cfg.cache / f"cve-base-{re.sub(r'[^A-Za-z0-9_.-]', '_', cfg.base_tag)}.json"
    if cache.exists():
        try:
            return set(json.loads(cache.read_text()))
        except ValueError:
            cache.unlink()
    log = git.run("log", "--no-merges", "--format=%H%x00%b%x01", f"{root}..{cfg.base_ref}")
    out: set[str] = set()
    for chunk in log.split("\x01"):
        sha, _, body = chunk.strip("\n").partition("\x00")
        if sha:
            out.add(sha)
            out.update(u.split()[0] for u in fp.upstream_refs(body))
    ledger.atomic_write(cache, json.dumps(sorted(out)))
    return out


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
    if cfg.kind != "stable":
        extra = base_commits(cfg, git) if git else None
        if extra is None:
            note = (f"{cfg.base_tag} is an Ubuntu kernel: the fixes Canonical applied are only visible in its "
                    "history, and the unified repo does not have it (run kpm fetch). Open counts are too high.")
        else:
            in_base = _short_match(extra)

    counts = {k: 0 for k in ("not-affected", "fixed-in-base", "fixed-by-patch", *OPEN)}
    open_rows = []
    for dyad in sorted(published.glob("*/*.dyad")):
        pairs = read_dyad(dyad)
        if not pairs:
            continue
        res = assess(pairs, base, carried, in_base)
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
    return {"line": cfg.line, "base": cfg.base_tag, "records": sum(counts.values()), "counts": counts,
            "open_by_severity": by_severity, "open": open_rows, "note": note}
