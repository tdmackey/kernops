"""What has moved upstream of what each line records (`kpm updates`).

Reads nothing but the repo's own records and the remotes: no unified repo, no
fetch. For every line it reports

  base     a newer tag of the base's series than the one in line.toml
  <source> a vendor source whose tracked branch or tag moved past pins.json

A stable base is current when no newer vX.Y.Z tag exists. An Ubuntu base is
compared with what the archive has *published* (Launchpad), not with git tags:
Canonical pushes tags for kernels still in -proposed, and a production line
follows the archive.
"""
import json
import re
import subprocess
import urllib.parse
import urllib.request

from . import config
from .git import git_env

LP_API = "https://api.launchpad.net/1.0/ubuntu/+archive/primary"
POCKETS = ("Updates", "Security", "Release")     # -proposed is not production
_RC = re.compile(r"-rc\d+$")
_UBUNTU_TAG = re.compile(r"^Ubuntu-(?:(.+)-)?(\d+\.\d+\.\d+-.*)$")


# ── version ordering ─────────────────────────────────────────────────────────

def natural_key(s: str):
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", s) if p]


def _order(ch: str) -> int:
    if ch == "~":
        return -1
    if not ch:
        return 0
    return ord(ch) if ch.isalpha() else ord(ch) + 256


def _verrevcmp(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ca, cb = a[i:i + 1], b[j:j + 1]
            if _order(ca) != _order(cb):
                return -1 if _order(ca) < _order(cb) else 1
            i += bool(ca)
            j += bool(cb)
        m, n = re.match(r"\d*", a[i:]).group(), re.match(r"\d*", b[j:]).group()
        i, j = i + len(m), j + len(n)
        if int(m or 0) != int(n or 0):
            return -1 if int(m or 0) < int(n or 0) else 1
    return 0


def debian_compare(a: str, b: str) -> int:
    """dpkg --compare-versions, as -1/0/1."""
    def split(v):
        epoch, _, rest = v.rpartition(":") if ":" in v else ("0", "", v)
        up, _, rev = rest.rpartition("-") if "-" in rest else (rest, "", "")
        return int(epoch or 0), up, rev
    (ea, ua, ra), (eb, ub, rb) = split(a), split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _verrevcmp(ua, ub) or _verrevcmp(ra, rb)


# ── remotes ──────────────────────────────────────────────────────────────────

def ls_remote(url: str, *patterns: str) -> dict:
    """{ref name: sha} for the refs of `url` matching the patterns."""
    out = subprocess.run(["git", "ls-remote", "--refs", url, *patterns], env=git_env(),
                         capture_output=True, text=True, timeout=300)
    if out.returncode:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "git ls-remote failed")
    return {ref: sha for sha, ref in (l.split("\t") for l in out.stdout.splitlines() if "\t" in l)}


def newest_tag(url: str, glob: str) -> str:
    tags = [r[len("refs/tags/"):] for r in ls_remote(url, f"refs/tags/{glob}")]
    tags = sorted((t for t in tags if not _RC.search(t)), key=natural_key)
    return tags[-1] if tags else ""


def published(package: str, series: str) -> dict | None:
    """Newest version of a source package published in the pockets we ship from."""
    q = urllib.parse.urlencode({"ws.op": "getPublishedSources", "source_name": package, "exact_match": "true",
                                "distro_series": f"/ubuntu/{series}", "status": "Published"})
    req = urllib.request.Request(f"{LP_API}?{q}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        entries = json.load(r).get("entries", [])
    best = None
    for e in entries:
        if e.get("pocket") in POCKETS and (best is None or debian_compare(e["source_package_version"], best["version"]) > 0):
            best = {"version": e["source_package_version"], "pocket": e["pocket"],
                    "published": (e.get("date_published") or "")[:10]}
    return best


# ── checks ───────────────────────────────────────────────────────────────────

def ubuntu_package(tag: str) -> tuple[str, str]:
    """('linux-hwe-7.0', 'Ubuntu-hwe-7.0-') from 'Ubuntu-hwe-7.0-7.0.0-43.43_24.04.1'."""
    m = _UBUNTU_TAG.match(tag)
    if not m:
        return "", ""
    name = m.group(1)
    return (f"linux-{name}", f"Ubuntu-{name}-") if name else ("linux", "Ubuntu-")


def check_base(cfg) -> dict:
    row = {"line": cfg.line, "base": cfg.line, "what": "base", "pin": cfg.base_tag}
    src = cfg.base_source
    if cfg.kind == "ubuntu" and src.name.startswith("ubuntu-"):
        package, prefix = ubuntu_package(cfg.base_tag)
        pub = published(package, src.name[len("ubuntu-"):]) if package else None
        if not pub:
            return dict(row, status="no-publication-found")
        latest = prefix + pub["version"].replace("~", "_")
        mine = cfg.base_tag[len(prefix):].replace("_", "~")
        row.update(pub, latest=latest, expected_tag=latest)
        cmp = debian_compare(mine, pub["version"])
        return dict(row, status="current" if cmp == 0 else "STALE" if cmp < 0 else "AHEAD-OF-ARCHIVE")
    root = cfg.series_root or cfg.base_tag.rsplit(".", 1)[0]
    latest = newest_tag(src.url, f"{root}*") or cfg.base_tag
    newer = natural_key(latest) > natural_key(cfg.base_tag)
    return dict(row, latest=latest, expected_tag=latest, version=latest.lstrip("v"),
                status="STALE" if newer else "current")


def check_source(cfg, name: str, pin: dict) -> dict:
    s = cfg.sources[name]
    row = {"line": cfg.line, "what": name}
    if not pin:
        return dict(row, pin="", status="never-harvested")
    if s.tags:
        mine = pin["ref"].split("/", 1)[1]
        latest = newest_tag(s.url, s.tags)
        return dict(row, pin=mine, latest=latest or mine,
                    status="current" if not latest or latest == mine else "MOVED")
    refs = ls_remote(s.url, f"refs/heads/{s.branch}")
    sha = refs.get(f"refs/heads/{s.branch}", "")
    return dict(row, pin=f"{s.branch}@{pin['sha'][:12]}", latest=f"{s.branch}@{sha[:12]}",
                status="current" if sha == pin["sha"] else "MOVED" if sha else "branch-gone")


def check(lines: list[str] | None = None) -> list[dict]:
    rows = []
    for line in lines or config.lines():
        cfg = config.load(line=line)
        pins_file = cfg.line_dir / "pins.json"
        pins = json.loads(pins_file.read_text()) if pins_file.exists() else {}
        todo = [("base", lambda: check_base(cfg))]
        todo += [(n, lambda n=n: check_source(cfg, n, pins.get(n))) for n in cfg.line_sources
                 if cfg.sources[n].kind == "vendor"]
        for what, fn in todo:
            try:
                rows.append(fn())
            except Exception as e:              # one unreachable remote must not hide the rest
                rows.append({"line": line, "what": what, "status": f"error: {e}"})
    return rows


def exit_code(rows: list[dict]) -> int:
    """0 everything current, 10 something moved, 2 something could not be checked."""
    if any(r["status"].startswith("error") for r in rows):
        return 2
    return 10 if any(r["status"] != "current" for r in rows) else 0
