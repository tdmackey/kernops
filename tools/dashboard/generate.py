#!/usr/bin/env python3
"""Status page for the kernel lines.

Everything on the page comes from the repository's own records, read through
kpm, plus two optional inputs outside the repository:

  build records   $WORK_ROOT/build/<line>/<series>/build-record.json, written by
                  scripts/build-line.sh (and any directory given with --records)
  CVE records     $WORK_ROOT/vulns, fetched by `kpm cves --update`

No kernel clone is needed. With --offline nothing is asked of the network: the
"what moved upstream" and "newer userspace" checks are left out and say so.

Usage: generate.py [-o dashboard.html] [--json dashboard.json] [--offline]
                   [--records DIR ...] [--cve-limit N]

The JSON output is the same data the page shows, for scripts and agents.
"""
import argparse
import collections
import concurrent.futures
import datetime
import html
import json
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("KERNOPS_REPO_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "tools" / "kpm"))
os.environ.setdefault("KPM_MONO", str(ROOT))

from kpm import config, cves, ledger, updates  # noqa: E402
from kpm.git import Git  # noqa: E402

DISPOSITIONS = ["carry", "in-base", "duplicate", "superseded", "deferred", "dropped",
                "config", "packaging", "noise", "undecided"]
LEVELS = {"crit": 0, "warn": 1, "info": 2}


# ── collect ──────────────────────────────────────────────────────────────────

def read_tsv(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.strip() and not line.startswith("#"):
            rows.append(line.split("\t"))
    return rows


def collect_line(name: str, moved: list[dict] | None) -> dict:
    cfg = config.load(line=name)
    ledgers = {n: ledger.read(cfg.ledger_path(n)) for n in cfg.line_sources}
    series_file = cfg.line_dir / "series"
    patches = series_file.read_text(encoding="utf-8").split() if series_file.exists() else []
    per_band = collections.Counter(p.split("/", 1)[0] for p in patches)

    sources, problems, deferred = [], [], collections.defaultdict(lambda: {"count": 0, "sources": set()})
    for src in cfg.line_sources:
        rows = ledgers[src]
        counts = collections.Counter(r["disposition"] for r in rows)
        problems += ledger.problems(src, rows)
        for r in rows:
            if r["disposition"] == "deferred":
                d = deferred[r.get("reason", "(no reason recorded)")]
                d["count"] += 1
                d["sources"].add(src)
        band = cfg.sources[src].band
        sources.append({
            "source": src, "band": band, "commits": len(rows), "decisions": dict(counts),
            "patches": per_band.get(f"{band:04d}-{src}", 0) if band is not None else 0,
            "proposals": sum(1 for r in rows if r.get("proposed")),
            "adapted": sum(1 for r in rows if r.get("adapted") and r["disposition"] == "carry"),
            "pending": ledger.pending(rows),
        })
    problems += ledger.orphaned_duplicates(ledgers)

    mine = [r for r in (moved or []) if r["line"] == name]
    base_row = next((r for r in mine if r["what"] == "base"), None)
    return {
        "name": name, "kind": cfg.kind, "base": cfg.base_tag, "patches": len(patches),
        "base_status": {k: base_row.get(k) for k in ("status", "latest", "pocket", "published")} if base_row else None,
        "sources_moved": [{"source": r["what"], "status": r["status"], "pin": r.get("pin"), "latest": r.get("latest")}
                          for r in mine if r["what"] != "base" and r["status"] == "MOVED"],
        "sources_unchecked": [{"source": r["what"], "status": r["status"]}
                              for r in mine if r["what"] != "base" and r["status"] not in ("current", "MOVED")],
        "sources": sources, "problems": problems,
        "undecided": sum(s["decisions"].get("undecided", 0) for s in sources),
        "proposals": sum(s["proposals"] for s in sources),
        "pending": sum(s["pending"] for s in sources),
        "adapted": sum(s["adapted"] for s in sources),
        "deferred": sorted(({"reason": k, "count": v["count"], "sources": sorted(v["sources"])}
                            for k, v in deferred.items()), key=lambda d: -d["count"]),
    }


def collect_cves(name: str, builds: list[dict], limit: int) -> dict:
    if not (cves.checkout() / "cve" / "published").is_dir():
        return {"available": False}
    cfg = config.load(line=name)
    used, used_from = None, ""
    for b in builds:                                 # newest build of this line that has a file list
        f = Path(b.get("dir", "")) / "sbom" / (b.get("flavours") or [{}])[0].get("name", "") / "sbom.used-files.txt"
        if b["line"] == name and f.is_file():
            used, used_from = set(f.read_text(encoding="utf-8").split()), f"{b['series']} {b['arch']}"
            break
    git = Git(cfg.repo) if cfg.repo.exists() else None
    try:
        res = cves.check(cfg, git, used_files=used)
    except (OSError, ValueError) as e:
        return {"available": False, "error": str(e)}
    # The list is sorted worst first, so a limit keeps the most severe.
    return {"available": True, "records": res["records"], "counts": res["counts"], "note": res["note"],
            "by_severity": res["open_by_severity"], "compiled_from": used_from,
            "assessed": res["assessed"], "assessment_problems": res["assessment_problems"],
            "open_total": len(res["open"]), "open": res["open"][:limit]}


def collect_builds(extra: list[str]) -> list[dict]:
    dirs = [config.work_root() / "build", *(Path(d) for d in extra)]
    out = []
    for d in dirs:
        for rec in sorted(d.glob("**/build-record.json")) if d.is_dir() else []:
            try:
                b = json.loads(rec.read_text(encoding="utf-8"))
            except ValueError:
                continue
            b["dir"] = str(rec.parent)
            boots = []
            for f in sorted(rec.parent.glob("boot-*.json")):
                try:
                    boots.append(json.loads(f.read_text(encoding="utf-8")))
                except ValueError:
                    pass
            b["boot"] = boots
            out.append(b)
    return sorted(out, key=lambda b: b.get("date", ""), reverse=True)


def collect_userspace(online: bool) -> dict:
    rows = read_tsv(ROOT / "userspace" / "matrix.tsv")
    with open(ROOT / "kernel" / "identity.toml", "rb") as f:
        series_names = list(tomllib.load(f).get("series", {})) or sorted({r[0] for r in rows})
    built = {}
    for s in series_names:
        d = config.work_root() / "userspace" / s / ".built"
        for f in d.glob("*.json") if d.is_dir() else []:
            try:
                built[(s, f.stem)] = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                pass
    newer_cache: dict[tuple, str] = {}
    if online:
        def lookup(key):
            try:
                pub = updates.published(*key)
                return key, (pub["version"] if pub else "")
            except Exception as e:                          # one failed lookup must not stop the page
                return key, f"error: {e}"
        keys = sorted({(r[1], r[2]) for r in rows if len(r) >= 4})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            newer_cache = dict(pool.map(lookup, keys))
    packages: dict[str, dict] = {}
    for r in rows:
        if len(r) < 4:
            continue
        series, pkg, frm, version = r[:4]
        why = r[4] if len(r) > 4 else ""
        cell = {"version": version, "from": frm,
                "overlay": any((ROOT / "userspace" / pkg / d).is_dir()
                               for d in ("debian", "patches", f"{series}/debian", f"{series}/patches")),
                "built": None}
        rec = built.get((series, pkg))
        if rec:
            cell["built"] = rec.get("version") == version
            cell["built_version"] = rec.get("our_version", "")
            cell["built_date"] = rec.get("date", "")
        latest = newer_cache.get((pkg, frm), "")
        if latest and not latest.startswith("error") and updates.debian_compare(latest, version) > 0:
            cell["newer"] = latest
        p = packages.setdefault(pkg, {"package": pkg, "why": why, "series": {}})
        p["series"][series] = cell
    return {"series": series_names, "packages": list(packages.values()), "checked_online": online}


def collect_modules() -> list[dict]:
    out = []
    for r in read_tsv(ROOT / "modules" / "matrix.tsv"):
        if len(r) >= 4:
            out.append({"line": r[0], "arch": r[1], "module": r[2], "version": r[3]})
    return out


def build_actions(data: dict) -> list[dict]:
    acts = []

    def add(level, line, text, command=""):
        acts.append({"level": level, "line": line, "text": text, "command": command})

    if data["updates_error"]:
        add("warn", "", f"Could not ask what moved upstream: {data['updates_error']}", "./scripts/kpm updates")
    for l in data["lines"]:
        n = l["name"]
        for p in l["problems"]:
            add("crit", n, f"Ledger problem: {p}", f"./scripts/kpm --line {n} verify --strict")
        if l["undecided"]:
            add("crit", n, f"{l['undecided']} commits have no decision ({l['proposals']} have a proposal to read)",
                f"./scripts/kpm --line {n} status")
        if l["pending"]:
            add("crit", n, f"{l['pending']} carried commits have no patch file",
                f"./scripts/kpm --line {n} import && ./scripts/kpm --line {n} export")
        bs = l["base_status"]
        if bs and bs["status"] in ("STALE", "AHEAD-OF-ARCHIVE"):
            what = "is behind" if bs["status"] == "STALE" else "is ahead of what the archive published:"
            add("warn", n, f"Base {l['base']} {what} {bs['latest']}", f"./scripts/kpm --line {n} rebase {bs['latest']}")
        elif bs and bs["status"] != "current":
            add("warn", n, f"Base could not be checked: {bs['status']}", "./scripts/kpm updates")
        for s in l["sources_moved"]:
            add("warn", n, f"Source {s['source']} has new commits ({s['pin']} -> {s['latest']})",
                f"./scripts/kpm --line {n} fetch {s['source']} && ./scripts/kpm --line {n} harvest {s['source']}")
        for s in l.get("sources_unchecked", []):
            add("info", n, f"Source {s['source']} could not be checked: {s['status']}", "./scripts/kpm updates")
        c = l.get("cves", {})
        if c.get("available"):
            k, sev = c["counts"], c.get("by_severity", {})
            rated = f" ({sev.get('critical', 0)} critical, {sev.get('high', 0)} high)" if sev else ""
            if k["rebase"]:
                add("warn", n, f"{k['rebase']} open CVEs are fixed in a newer release of the series",
                    f"./scripts/kpm --line {n} cves")
            if k["backport"] or k["no-fix"]:
                add("crit" if sev.get("critical") else "warn", n,
                    f"{c['open_total']} open CVEs{rated}: {k['backport']} need a backport, {k['no-fix']} have no fix anywhere",
                    f"./scripts/kpm --line {n} cves --severity high")
        elif "error" in c:
            add("warn", n, f"CVE check failed: {c['error']}", f"./scripts/kpm --line {n} cves")
        for p in c.get("assessment_problems", []):
            add("crit", n, f"CVE assessment no longer holds: {p}", f"edit kernel/lines/{n}/cves.toml")
    for p in data["userspace"]["packages"]:
        for s, cell in p["series"].items():
            if cell["built"] is False:
                add("warn", "", f"Userspace {p['package']} for {s}: the built package is not version {cell['version']}",
                    f"./scripts/in-builder.sh {s} ./scripts/build-userspace.sh {s} {p['package']}")
            if cell.get("newer"):
                add("info", "", f"Userspace {p['package']}: {cell['from']} now has {cell['newer']} (we take {cell['version']})",
                    "edit userspace/matrix.tsv")
    return sorted(acts, key=lambda a: (LEVELS[a["level"]], a["line"]))


def collect(offline: bool, records: list[str], cve_limit: int) -> dict:
    moved, err = None, ""
    if not offline:
        try:
            moved = updates.check()
        except Exception as e:
            err = str(e)
    builds = collect_builds(records)
    lines = []
    for name in config.lines():
        l = collect_line(name, moved)
        l["cves"] = collect_cves(name, builds, cve_limit)
        lines.append(l)
    data = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "offline": offline, "updates_error": err,
        "lines": lines, "builds": builds,
        "userspace": collect_userspace(online=not offline),
        "modules": collect_modules(),
    }
    data["actions"] = build_actions(data)
    return data


# ── render ───────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2330;--mute:#5d6878;--line:#dfe3ea;--crit:#b3261e;--critbg:#fde8e6;
--warn:#8a5a00;--warnbg:#fdf1d6;--ok:#1b6e3c;--okbg:#e2f4e8;--info:#27548a;--infobg:#e5eefa;--code:#eef0f4}
@media(prefers-color-scheme:dark){:root{--bg:#12161c;--card:#1a2029;--ink:#e6e9ee;--mute:#9aa5b5;--line:#2c3440;
--crit:#ff8a80;--critbg:#3a1c1a;--warn:#f2c260;--warnbg:#3a2e12;--ok:#7fd49b;--okbg:#16301f;--info:#9cc2f5;--infobg:#17283e;--code:#232b36}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 2px}
h2{font-size:17px;margin:30px 0 4px}h3{font-size:15px;margin:18px 0 6px}
.sub{color:var(--mute);margin:0 0 10px;font-size:13.5px}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:10px 0}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600;white-space:nowrap}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:0}
code{background:var(--code);padding:1px 5px;border-radius:4px;font:12.5px ui-monospace,Consolas,monospace;word-break:break-word}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;white-space:nowrap}
.crit{color:var(--crit);background:var(--critbg)}.warn{color:var(--warn);background:var(--warnbg)}.ok{color:var(--ok);background:var(--okbg)}
.info{color:var(--info);background:var(--infobg)}.mute{color:var(--mute)}
.kpis{display:flex;flex-wrap:wrap;gap:8px 22px;margin:6px 0 2px}.kpi b{font-size:19px;display:block}.kpi span{color:var(--mute);font-size:12.5px}
a{color:var(--info)}details summary{cursor:pointer;color:var(--info);font-size:13.5px;margin-top:8px}
.filter{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 6px}.filter button{font:inherit;font-size:13px;padding:3px 11px;
border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--ink);cursor:pointer}
.filter button.on{background:var(--info);border-color:var(--info);color:var(--card);font-weight:600}
"""

# The severity filter: each button shows the rows rated that or worse.
SCRIPT = """
(function(){var order=["critical","high","medium","low","unscored"];
document.querySelectorAll(".filter").forEach(function(f){var rows=f.parentNode.querySelectorAll("table.cves tbody tr");
f.addEventListener("click",function(ev){var b=ev.target.closest("button");if(!b)return;
f.querySelectorAll("button").forEach(function(x){x.classList.toggle("on",x===b)});var min=b.dataset.min;
rows.forEach(function(r){var s=r.dataset.sev;
r.hidden=min==="only-unscored"?s!=="unscored":order.indexOf(s)>order.indexOf(min)})})})})();
"""

e = html.escape


def chip(text, cls):
    return f'<span class="chip {cls}">{e(str(text))}</span>'


def table(head, rows, numeric=()):
    th = "".join(f'<th class="{"n" if i in numeric else ""}">{e(h)}</th>' for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td class="{"n" if i in numeric else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
                   for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def render_actions(data):
    acts = data["actions"]
    out = ['<h2>1. What needs action</h2>',
           '<p class="sub">Each row is one thing to do, most urgent first, with the command that starts it.</p>']
    if not acts:
        out.append(f'<div class="card">{chip("nothing to do", "ok")} No line needs action.</div>')
    else:
        names = {"crit": "must fix", "warn": "act", "info": "note"}
        out.append('<div class="card">' + table(
            ["", "Line", "What", "Command"],
            [[chip(names[a["level"]], a["level"]), e(a["line"]) or '<span class="mute">all</span>', e(a["text"]),
              f"<code>{e(a['command'])}</code>" if a["command"] else ""] for a in acts]) + "</div>")
    if data["offline"]:
        out.append('<p class="sub">Offline: upstream was not asked what moved, and newer userspace versions were not looked up.</p>')
    return "".join(out)


def render_cves(l):
    c = l["cves"]
    n = l["name"]
    if not c.get("available"):
        why = c.get("error") or "the CVE records are not on this machine"
        return f'<h3>CVEs</h3><p class="sub">Not checked: {e(why)}. Get them with <code>./scripts/kpm --line {e(n)} cves --update</code>.</p>'
    k = c["counts"]
    out = ['<h3>CVEs</h3><div class="kpis">',
           f'<div class="kpi"><b>{c["open_total"]}</b><span>open</span></div>',
           f'<div class="kpi"><b>{k["rebase"]}</b><span>fixed in a newer release of the series</span></div>',
           f'<div class="kpi"><b>{k["backport"]}</b><span>need a backport</span></div>',
           f'<div class="kpi"><b>{k["no-fix"]}</b><span>no fix anywhere</span></div>',
           f'<div class="kpi"><b>{k["fixed-in-base"]}</b><span>fixed in the base</span></div>',
           f'<div class="kpi"><b>{k["fixed-by-patch"]}</b><span>fixed by a patch we carry</span></div>',
           f'<div class="kpi"><b>{k["not-affected"]}</b><span>not affected</span></div>',
           f'<div class="kpi"><b>{k.get("assessed", 0)}</b><span>examined by us: not affected</span></div></div>',
           f'<p class="sub">From {c["records"]} records of the kernel CVE authority. "Open" means the record says this '
           'kernel is affected and neither the base nor a carried patch has the fix.</p>']
    if c["note"]:
        out.append(f'<p class="sub">{chip("limit", "warn")} {e(c["note"])}</p>')
    if c.get("assessed"):
        rows = [[f'<a href="https://www.cve.org/CVERecord?id={e(a["cve"])}">{e(a["cve"])}</a>', e(a.get("title", "")),
                 e(a["reason"]), " ".join(f"<code>{e(s)}</code>" for s in a["evidence"])] for a in c["assessed"]]
        out.append(f'<details><summary>Examined by us: not affected ({len(c["assessed"])})</summary>'
                   '<p class="sub">The record lists the kernel as affected. We examined each one; the reason is in '
                   f'<code>kernel/lines/{e(n)}/cves.toml</code>. An entry applies only while its evidence is in the base.</p>'
                   + table(["CVE", "Title", "Why it does not affect this line", "Evidence in the base"], rows) + "</details>")
    if c["open"]:
        def closes(r):
            if r["status"] == "rebase":
                return f'move the base to <code>v{e(r["fixed_in"])}</code>'
            if r["status"] == "backport":
                sha = r.get("fix", "")
                return (f'backport <a href="https://git.kernel.org/stable/c/{e(sha)}"><code>{e(sha[:12])}</code></a>'
                        f' (in v{e(r["fixed_in"])})')
            return "no fix exists yet"

        def built(r):
            return {True: chip("yes", "warn"), False: chip("no", "ok"), None: '<span class="mute">not known</span>'}[r.get("compiled")]
        def rating(r):
            s = r.get("severity", "unscored")
            if s == "unscored":
                return '<span class="mute">not scored</span>'
            cls = {"critical": "crit", "high": "warn"}.get(s, "info")
            label = f"{s} {r.get('score', '')}".strip()
            return '<span title="' + e(r.get("vector", "")) + '">' + chip(label, cls) + "</span>"

        label = "Affected code is built" + (f' ({e(c["compiled_from"])} build)' if c["compiled_from"] else "")
        head = "".join(f"<th>{e(h)}</th>" for h in ["CVE", "Severity", "What closes it", "Title", label])
        trs = "".join(
            f'<tr data-sev="{e(r.get("severity", "unscored"))}">'
            f'<td><a href="https://www.cve.org/CVERecord?id={e(r["cve"])}">{e(r["cve"])}</a></td>'
            f'<td>{rating(r)}</td><td>{closes(r)}</td><td>{e(r.get("title", ""))}</td><td>{built(r)}</td></tr>'
            for r in c["open"])
        sev = c.get("by_severity", {})
        shown = collections.Counter(r.get("severity", "unscored") for r in c["open"])
        buttons = "".join(
            f'<button type="button" data-min="{m}" class="{"on" if m == "unscored" else ""}">{e(text)}</button>'
            for m, text in (("unscored", f"All ({c['open_total']})"),
                            ("critical", f"Critical ({sev.get('critical', 0)})"),
                            ("high", f"High or worse ({sev.get('critical', 0) + sev.get('high', 0)})"),
                            ("medium", "Medium or worse"), ("only-unscored", f"Not scored ({sev.get('unscored', 0)})")))
        body = (f'<div class="filter" role="group" aria-label="Severity">{buttons}</div>'
                f'<div class="scroll"><table class="cves"><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table></div>'
                '<p class="sub">Severity is the CVSS rating in the record. The kernel CVE authority rates only part of its '
                'records, so "not scored" does not mean low.</p>')
        more = c["open_total"] - len(c["open"])
        if more > 0:
            body += (f'<p class="sub">The {len(c["open"])} most severe are listed ({shown.get("critical", 0)} critical, '
                     f'{shown.get("high", 0)} high). All {c["open_total"]}: '
                     f'<code>./scripts/kpm --line {e(n)} cves --json</code></p>')
        out.append(f'<details{" open" if c["open_total"] <= 12 else ""}><summary>Open CVEs ({c["open_total"]})</summary>{body}</details>')
    return "".join(out)


def render_lines(data):
    out = ['<h2>2. Lines</h2>',
           '<p class="sub">A line is one kernel we maintain: a base tag plus a patch series. Decisions are per commit of each source.</p>']
    for l in data["lines"]:
        bs = l["base_status"]
        if not bs:
            state = chip("not checked", "info")
        elif bs["status"] == "current":
            state = chip("base is current", "ok")
        else:
            state = chip(f'{bs["status"].lower()}: {bs.get("latest") or ""}', "warn")
        health = chip("ledger complete", "ok") if not (l["problems"] or l["undecided"] or l["pending"]) else chip("ledger incomplete", "crit")
        out.append(f'<div class="card"><h3 style="margin-top:0">{e(l["name"])} <span class="mute">on</span> <code>{e(l["base"])}</code> {state} {health}</h3>')
        out.append('<div class="kpis">'
                   f'<div class="kpi"><b>{l["patches"]}</b><span>patches in the series</span></div>'
                   f'<div class="kpi"><b>{l["adapted"]}</b><span>changed by hand</span></div>'
                   f'<div class="kpi"><b>{l["undecided"]}</b><span>without a decision</span></div>'
                   f'<div class="kpi"><b>{sum(d["count"] for d in l["deferred"])}</b><span>deferred</span></div></div>')
        cols = [d for d in DISPOSITIONS if any(s["decisions"].get(d) for s in l["sources"])]
        rows = []
        for s in l["sources"]:
            band = f'{s["band"]:04d}' if s["band"] is not None else ""
            rows.append([e(s["source"]), band, s["commits"], s["patches"]] + [s["decisions"].get(d, "") or "" for d in cols])
        out.append(table(["Source", "Band", "Commits", "Patches"] + cols, rows, numeric=set(range(2, 4 + len(cols)))))
        if l["deferred"]:
            out.append("<h3>Waiting</h3>" + table(
                ["Commits", "Source", "Waiting for"],
                [[d["count"], e(", ".join(d["sources"])), e(d["reason"])] for d in l["deferred"]], numeric={0}))
        out.append(render_cves(l))
        out.append("</div>")
    return "".join(out)


def render_builds(data):
    out = ['<h2>3. Builds</h2>',
           '<p class="sub">One row per build record. A build with no record does not appear: the page does not guess.</p>']
    if not data["builds"]:
        out.append('<div class="card"><span class="mute">No build records found. <code>scripts/build-line.sh</code> writes one into '
                   '<code>$WORK_ROOT/build/&lt;line&gt;/&lt;series&gt;/</code>.</span></div>')
        return "".join(out)
    rows = []
    for b in data["builds"]:
        tc = b.get("toolchain", {})
        fl = "; ".join(f'{f["name"]}: {f.get("modules", "?")} modules, {f.get("patches", {}).get("compiled", "?")} of '
                       f'{f.get("patches", {}).get("total", "?")} patches reach built code' for f in b.get("flavours", [])) or "no SBOM"
        boots = b.get("boot", [])
        boot = " ".join(chip(f'{x.get("flavour", "")} booted {x.get("date", "")[:10]}', "ok") if x.get("ok")
                        else chip(f'{x.get("flavour", "")} boot failed', "crit") for x in boots) or chip("not booted", "info")
        rows.append([e(b.get("line", "")), e(b.get("series", "")), e(b.get("arch", "")), f'<code>{e(b.get("version", ""))}</code>',
                     e(b.get("date", "")[:16]), e(f'gcc {tc.get("gcc", "?")}, pahole {tc.get("pahole", "?")}'), e(fl),
                     boot + " " + chip("hardware: no record", "info")])
    out.append('<div class="card">' + table(["Line", "Series", "Arch", "Version", "Built", "Toolchain", "Contents", "Tested"], rows) + "</div>")
    return "".join(out)


def render_userspace(data):
    u = data["userspace"]
    out = ['<h2>4. Userspace packages</h2>',
           '<p class="sub">Packages that must agree with the kernel version, rebuilt from a newer Ubuntu series. '
           'An empty cell means the series already has a version that is new enough.</p>']
    rows = []
    for p in u["packages"]:
        cells = []
        for s in u["series"]:
            c = p["series"].get(s)
            if not c:
                cells.append('<span class="mute">not needed</span>')
                continue
            state = {True: chip("built", "ok"), False: chip("rebuild", "warn"), None: chip("no record", "info")}[c["built"]]
            extra = (" " + chip("our patches", "info") if c["overlay"] else "") + \
                    (" " + chip(f'newer: {c["newer"]}', "warn") if c.get("newer") else "")
            cells.append(f'<code>{e(c["version"])}</code> <span class="mute">from {e(c["from"])}</span> {state}{extra}')
        rows.append([e(p["package"])] + cells + [e(p["why"])])
    out.append('<div class="card">' + table(["Package"] + u["series"] + ["Why"], rows) + "</div>")
    if not u["checked_online"]:
        out.append('<p class="sub">Newer versions in the Ubuntu archive were not looked up (offline).</p>')
    return "".join(out)


def render_modules(data):
    out = ['<h2>5. Out-of-tree modules</h2>',
           '<p class="sub">Versions from <code>modules/matrix.tsv</code>. There is no record yet of which were built against which kernel.</p>']
    if not data["modules"]:
        return "".join(out) + '<div class="card"><span class="mute">None listed.</span></div>'
    by = collections.OrderedDict()
    for m in data["modules"]:
        by.setdefault((m["line"], m["module"]), {})[m["arch"]] = m["version"]
    arches = sorted({m["arch"] for m in data["modules"]})
    rows = [[e(line), e(mod)] + [f'<code>{e(v.get(a, ""))}</code>' if v.get(a) else "" for a in arches] for (line, mod), v in by.items()]
    listed = {m["line"] for m in data["modules"]}
    missing = [l["name"] for l in data["lines"] if l["name"] not in listed]
    note = f'<p class="sub">No modules are listed for: {e(", ".join(missing))}.</p>' if missing else ""
    return "".join(out) + '<div class="card">' + table(["Line", "Module"] + arches, rows) + "</div>" + note


def render(data: dict) -> str:
    counts = collections.Counter(a["level"] for a in data["actions"])
    head = (f'<h1>Kernel lines</h1><p class="sub">Generated {e(data["generated"])}'
            f' &middot; {counts["crit"]} must fix, {counts["warn"]} to act on, {counts["info"]} notes</p>')
    body = head + render_actions(data) + render_lines(data) + render_builds(data) + render_userspace(data) + render_modules(data)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kernel lines</title>'
            f"<style>{CSS}</style></head><body><main>{body}</main><script>{SCRIPT}</script></body></html>\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--output", default="dashboard/index.html")
    ap.add_argument("--json", help="also write the data as JSON")
    ap.add_argument("--offline", action="store_true", help="ask nothing of the network")
    ap.add_argument("--records", action="append", default=[], help="another directory to search for build-record.json")
    ap.add_argument("--cve-limit", type=int, default=200, help="open CVEs to list per line (default 200)")
    args = ap.parse_args(argv)
    data = collect(args.offline, args.records, args.cve_limit)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8", newline="\n")
    if args.json:
        Path(args.json).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8", newline="\n")
    counts = collections.Counter(a["level"] for a in data["actions"])
    print(f"{out}: {len(data['lines'])} lines, {counts['crit']} must fix, {counts['warn']} to act on, {counts['info']} notes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
