"""kpm command line. Every command accepts --json for machine-readable output."""
import argparse
import json
import re
import sys
from collections import Counter

from pathlib import Path

from . import config, core, kconfig, ledger, packaging, series
from .git import Git, GitError


def _sources(cfg, names):
    names = names or cfg.vendor_sources()
    for n in names:
        if n not in cfg.sources:
            sys.exit(f"kpm: unknown source '{n}' (known: {', '.join(cfg.sources)})")
    return names


def _emit(args, data, text):
    if args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
    else:
        text(data)


def cmd_fetch(cfg, git, args):
    if args.base_only:
        res = [core.fetch_base_only(cfg, git)]
        _emit(args, res, lambda d: [print(f"{r['source']:12} {r['ref']}  <- {r['from']} (shallow)") for r in d])
        return
    names = args.source or [cfg.base_source_name, *cfg.vendor_sources()]
    res = core.fetch(cfg, git, names)
    _emit(args, res, lambda d: [print(f"{r['source']:12} {r['ref']}  <- {r['from']}") for r in d])


def cmd_harvest(cfg, git, args):
    res = [core.harvest(cfg, git, n) for n in _sources(cfg, args.source)]

    def text(d):
        for r in d:
            print(f"{r['source']:12} {r['ref']}: {r['commits']} commits, {r['new']} new, "
                  f"{r['moved']} re-identified by content, {len(r['vanished'])} vanished")
            for v in r["vanished"]:
                print(f"    vanished ({v['disposition']}): {v['sha'][:12]} {v['subject']}")
    _emit(args, res, text)


def cmd_resolve(cfg, git, args):
    res = core.resolve(cfg, git, _sources(cfg, args.source), args.accept_proposed)
    _emit(args, res, lambda d: [print(
        f"{r['source']:12} +{r['in-base']} in-base  +{r['duplicate']} duplicate  +{r['reverted']} reverted  +{r['policy']} by policy  "
        f"{r['undecided']} undecided ({r['proposed']} with a proposal)") for r in d])


def cmd_status(cfg, git, args):
    ledgers = ledger.read_all(cfg.line_dir / "ledger")
    res = {"line": cfg.line, "base": cfg.base_tag,
           "sources": {n: dict(Counter("pending" if r["disposition"] == "carry" and not r.get("patch")
                                       else r["disposition"] for r in rows)) for n, rows in ledgers.items()}}

    def text(d):
        print(f"line {d['line']} on {d['base']}")
        cols = ["carry", "pending", "undecided", "in-base", "duplicate", "deferred", "dropped",
                "superseded", "config", "packaging", "noise"]
        print(f"{'source':12}" + "".join(f"{c:>11}" for c in cols) + f"{'total':>8}")
        for n, c in d["sources"].items():
            print(f"{n:12}" + "".join(f"{c.get(k, 0):>11}" for k in cols) + f"{sum(c.values()):>8}")
    _emit(args, res, text)


def cmd_why(cfg, git, args):
    q = args.query.lower()
    hits = []
    for name, rows in ledger.read_all(cfg.line_dir / "ledger").items():
        for r in rows:
            if (r["sha"].startswith(q) or q in r["subject"].lower() or r.get("change", "").lower() == q
                    or any(u.startswith(q) for u in r.get("upstream", []))):
                hits.append({"source": name, **r})

    def text(d):
        if not d:
            print("no ledger row matches; it is in no tracked source delta")
        for r in d:
            extra = {"in-base": f"as {r.get('base_sha', '')[:12]} in {r.get('landed')} ({r.get('match')})",
                     "carry": r.get("patch", ""), "duplicate": f"of {r.get('of')}",
                     "dropped": r.get("reason", ""), "deferred": r.get("reason", "")}.get(r["disposition"], "")
            print(f"{r.get('change', '-'):8} {r['source']:10} {r['sha'][:12]} {r['disposition']:10} {extra}")
            print(f"         {r['subject']}")
    _emit(args, hits, text)


def cmd_verify(cfg, git, args):
    ledgers = ledger.read_all(cfg.line_dir / "ledger")
    errors, undecided = [], 0
    patches = cfg.line_dir / "patches"
    for name, rows in ledgers.items():
        errors += ledger.problems(name, rows)
        undecided += sum(r["disposition"] == "undecided" for r in rows)
        for r in rows:
            if r["disposition"] == "carry" and r.get("patch") and not (patches / r["patch"]).exists():
                errors.append(f"{name}:{r['sha'][:12]} carry row points at missing {r['patch']}")
    errors += ledger.orphaned_duplicates(ledgers)
    pending = sum(ledger.pending(rows) for rows in ledgers.values())
    errors += series.check_applies(cfg, git)
    listed = set((cfg.line_dir / "series").read_text().split()) if (cfg.line_dir / "series").exists() else set()
    carried = {r["patch"] for rows in ledgers.values() for r in rows
               if r["disposition"] == "carry" and r.get("patch")}
    errors += [f"series lists {p} but no carry row owns it" for p in sorted(listed - carried)]
    errors += [f"carry row owns {p} but series does not list it" for p in sorted(carried - listed)]
    if args.fixes:
        fx = core.missing_fixes(cfg, git, args.fixes)
        errors += [f"missing upstream fix {m['fix'][:12]} \"{m['fix_subject']}\" for carried {m['change']} \"{m['carried']}\""
                   for m in fx["missing"]]
        errors += [f"{t['source']}:{t['change']} names upstream {t['upstream'][:12]} but subjects differ"
                   for t in fx["bad_trailers"]]
    if args.strict and undecided:
        errors.append(f"{undecided} undecided rows")
    if args.strict and pending:
        errors.append(f"{pending} carry rows have no patch file (run kpm import, kpm export)")
    res = {"ok": not errors, "undecided": undecided, "pending": pending,
           "patches": len(listed), "errors": errors}

    def text(d):
        for e in d["errors"]:
            print(f"FAIL {e}")
        print(f"verify: {'ok' if d['ok'] else 'FAILED'} ({d['patches']} patches apply, "
              f"{d['undecided']} undecided, {d['pending']} pending import)")
    _emit(args, res, text)
    return 0 if res["ok"] else 1


def cmd_decide(cfg, git, args):
    if args.disposition in ("dropped", "deferred") and not args.reason:
        sys.exit(f"kpm: {args.disposition} needs --reason")
    if not (args.ids or args.source):
        sys.exit("kpm: give change ids / shas, or --source")
    for i in args.ids:
        if not re.fullmatch(r"C-\d+|[0-9a-f]{7,40}", i):
            sys.exit(f"kpm: '{i}' is not a change id (C-NNNNN) or a commit id of 7 or more hex digits")
    if args.ids and args.subject:
        sys.exit("kpm: --subject selects rows with --source; it cannot be combined with ids")
    rx = re.compile(args.subject) if args.subject else None
    ledgers = ledger.read_all(cfg.line_dir / "ledger")
    for i in (i for i in args.ids if not i.startswith("C-")):
        hits = {r["sha"] for rows in ledgers.values() for r in rows if r["sha"].startswith(i)}
        if len(hits) > 1:
            sys.exit(f"kpm: commit id '{i}' matches {len(hits)} rows; give more digits")
    changed = []
    for name, rows in ledgers.items():
        if args.source and name != args.source:
            continue
        for r in rows:
            by_id = any(r.get("change") == i or r["sha"].startswith(i) for i in args.ids)
            by_sel = bool(args.source) and not args.ids and (not rx or rx.search(r["subject"]))
            if not (by_id or by_sel):
                continue
            if not by_id and r["disposition"] != "undecided" and not args.force:
                continue                      # bulk selectors only touch undecided rows
            for k in ("reason", "by", "of", "proposed", "decided_by", "match", "base_sha", "landed"):
                r.pop(k, None)
            r["disposition"] = args.disposition
            for k in ("reason", "by", "of"):
                if getattr(args, k):
                    r[k] = getattr(args, k)
            if args.disposition != "carry":
                r.pop("patch", None)
            changed.append({"source": name, "change": r.get("change"), "sha": r["sha"], "subject": r["subject"]})
        bad = ledger.problems(name, rows)
        if bad:
            sys.exit("kpm: " + "; ".join(bad))
    for name, rows in ledgers.items():
        ledger.write(cfg.ledger_path(name), rows)
    _emit(args, changed, lambda d: [print(f"{args.disposition:10} {c['source']:8} {c['sha'][:12]} {c['subject']}")
                                    for c in d] or print(f"{len(d)} row(s) set to {args.disposition}"))


def cmd_import(cfg, git, args):
    res = series.import_(cfg, git, cont=args.cont)

    def text(d):
        print(f"{d['branch']} in {d['worktree']}: {d['applied']} applied "
              f"({d['from_patch']} from patch files, {d['picked']} cherry-picked)")
        for e in d["landed"]:
            print(f"  already in base: {e['change']} {e['subject']}")
        for e in d["empty"]:
            print(f"  empty (content already present): {e['change']} {e['subject']}")
        if d["conflict"]:
            c = d["conflict"]
            print(f"CONFLICT {c.get('change')} {c.get('sha', '')[:12]} {c.get('subject', '')}")
            for f in c["files"]:
                print(f"    {f}")
            print(f"  {c['hint']}")
    _emit(args, res, text)
    return 3 if res["conflict"] else 0


def cmd_export(cfg, git, args):
    res = series.export(cfg, git)

    def text(d):
        print(f"{d['patches']} patches written ({d['adapted']} differ from the vendor original, "
              f"{d['removed_stale']} stale removed)")
        for n, c in d["per_source"].items():
            print(f"  {n}: {c}")
        if d["unknown_commits"]:
            print(f"  WARNING {len(d['unknown_commits'])} commits on the branch have no ledger row: "
                  f"{' '.join(d['unknown_commits'][:8])}")
    _emit(args, res, text)
    return 1 if res["unknown_commits"] else 0


def cmd_config(cfg, git, args):
    res = kconfig.compare(cfg, git)
    if args.write:
        res["written"] = kconfig.write_union(cfg, res)

    def text(d):
        print(f"Ubuntu master: {d['master_options']} options")
        for n, v in d["vendors"].items():
            raised = sum(n in u["wanted_by"] for u in d["union"].values())
            print(f"  {n:8} {v['ref']}: {v['overrides']} overrides, {raised} raise an option, "
                  f"{d['lowered'][n]} lower one (ignored)")
        print(f"union: {len(d['union'])} options must be turned up for a unified kernel")
        shown = d["union"] if args.all else dict(list(d["union"].items())[:25])
        for opt, u in shown.items():
            vals = " ".join(f"{k}:{u['master'].get(k, '-')}->{v}" for k, v in u["value"].items())
            print(f"  {opt:42} {vals}   [{' '.join(u['wanted_by'])}]")
        if len(shown) < len(d["union"]):
            print(f"  ... {len(d['union']) - len(shown)} more (--all)")
        print(f"review: {len(d['review'])} non-tristate options differ between vendors")
        for opt, r in (d["review"] if args.all else dict(list(d["review"].items())[:15])).items():
            vend = "; ".join(f"{n}={sorted(set(v.values()))}" for n, v in r["vendors"].items())
            print(f"  {opt:42} master={sorted(set(r['master'].values()))}  {vend}")
        if d.get("written"):
            print(f"wrote {d['written']}")
    _emit(args, res, text)


def cmd_lines(args):
    out = []
    for name in config.lines():
        c = config.load(line=name)
        if args.kind and c.kind != args.kind:
            continue
        series_file = c.line_dir / "series"
        out.append({"line": name, "kind": c.kind, "base": c.base_tag, "base_source": c.base_source_name,
                    "patches": len(series_file.read_text().split()) if series_file.exists() else 0})
    if args.json:
        json.dump(out, sys.stdout, indent=2)
        print()
    else:
        for r in out:                       # tab-separated: line, base tag, kind, patch count
            print(f"{r['line']}\t{r['base']}\t{r['kind']}\t{r['patches']}")
    return 0


def cmd_updates(args):
    from . import updates
    rows = updates.check([args.line] if args.line else None)
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
    else:
        for r in rows:
            move = "" if r["status"] == "current" else f"  {r.get('pin', '')} -> {r.get('latest', '')}"
            print(f"{r['line']:<10} {r['what']:<12} {r['status']:<18}{move if move.strip(' ->') else ''}")
    return updates.exit_code(rows)


def cmd_cves(cfg, git, args):
    from . import cves
    if args.update:
        cves.update()
    used = None
    if args.used_files:
        used = set(Path(args.used_files).read_text(encoding="utf-8").split())
    try:
        res = cves.check(cfg, git, used_files=used)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"kpm: {e}")
    res["open_total"] = len(res["open"])
    res["open"] = cves.at_least(res["open"], args.severity)

    def text(d):
        c = d["counts"]
        print(f"{d['line']} on {d['base']}: {d['records']} CVE records")
        print(f"  not affected {c['not-affected']}, fixed in the base {c['fixed-in-base']}, "
              f"fixed by a carried patch {c['fixed-by-patch']}")
        print(f"  open: {d['open_total']} (a newer release of the series fixes {c['rebase']}, "
              f"needs a backport {c['backport']}, no fix anywhere {c['no-fix']})")
        if c["assessed"]:
            print(f"  examined and recorded as not affected (cves.toml): {c['assessed']}")
        for p in d["assessment_problems"]:
            print(f"  PROBLEM cves.toml {p}")
        s = d["open_by_severity"]
        print(f"  by severity: critical {s['critical']}, high {s['high']}, medium {s['medium']}, low {s['low']}, "
              f"not scored {s['unscored']}")
        if args.severity != "unscored":
            print(f"  listing {len(d['open'])} rated {args.severity} or worse")
        if d["note"]:
            print(f"  NOTE {d['note']}")
        for r in d["open"][: args.limit]:
            how = {"rebase": f"fixed in v{r.get('fixed_in')}", "backport": f"backport {r.get('fix', '')[:12]} (v{r.get('fixed_in')})",
                   "no-fix": "no fix yet"}[r["status"]]
            built = {True: "", False: "  [not compiled]", None: ""}[r.get("compiled")]
            sev = r["severity"] if r["severity"] == "unscored" else f"{r['severity']} {r.get('score', '')}"
            print(f"  {r['cve']:<16} {sev:<13} {how:<34} {r.get('title', '')[:64]}{built}")
        if len(d["open"]) > args.limit:
            print(f"  ... {len(d['open']) - args.limit} more (--limit N, or --json)")
    _emit(args, res, text)


def cmd_add(cfg, git, args):
    res = core.add_local(cfg, git, args.sha)
    _emit(args, res, lambda d: [print(f"{r['sha'][:12]} {r['result']:30} {r['subject']}") for r in d])


def cmd_apply(cfg, git, args):
    res = series.apply(cfg, git, Path(args.tree))
    _emit(args, res, lambda d: print(f"{d['base']} + {d['patches']} patches -> {d['tree']}"))


def cmd_rebase(cfg, git, args):
    res = series.rebase(cfg, git, args.tag)

    def text(d):
        if d.get("unchanged"):
            print(f"{cfg.line} is already on {d['new']}")
            return
        print(f"{cfg.line}: {d['old']} -> {d['new']}")
        for x in d["landed"]:
            print(f"  now in base ({x['match']}): {x.get('change')} {x['subject']}")
        for x in d["empty"]:
            print(f"  empty: {x['change']} {x['subject']}")
        if d["conflict"]:
            c = d["conflict"]
            print(f"CONFLICT {c.get('change')} {c.get('sha', '')[:12]} {c.get('subject', '')}")
            for f in c["files"]:
                print(f"    {f}")
            print(f"  {c['hint']}; then kpm export")
        else:
            print(f"  {d['export']['patches']} patches carried on {d['new']}"
                  + (f"; range-diff in {d['range_diff']}" if d.get("range_diff") else ""))
    _emit(args, res, text)
    return 3 if res.get("conflict") else 0


def cmd_packaging(cfg, git, args):
    if cfg.kind != "stable":
        sys.exit(f"kpm: line {cfg.line} builds on an Ubuntu tag, which already carries its packaging")
    root = next(n for n in cfg.vendor_sources() if cfg.sources[n].packaging_dir and not cfg.sources[n].parent)
    pin = core.pins(cfg).get(root)
    if not pin:
        sys.exit(f"kpm: no pin for {root}; run kpm harvest {root}")
    packaging.overlay(git, pin["sha"], Path(args.tree).resolve())
    res = packaging.render(cfg.mono, Path(args.tree).resolve(), args.series, cfg.base_tag,
                           union=cfg.line_dir / "config" / "union.json",
                           modprobe=packaging.vendor_modprobe(cfg, git))

    def text(d):
        print(f"{d['source_package']} {d['version']} ({d['series']}) -> {d['dir']}")
        for f, archs in d["flavours"].items():
            print(f"  flavour {f}: {' '.join(archs)}")
        print(f"  perf + bpftool enabled ({d['rule_edits']} rule edits); "
              f"modprobe rules merged from: {' '.join(d['modprobe_from']) or 'none'}")
    _emit(args, res, text)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kpm", description="kernel patch manager")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--line", help="kernel line (default: $KPM_LINE or the only one)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, hlp in [
        ("fetch", cmd_fetch, "update the unified bare repo from each source"),
        ("harvest", cmd_harvest, "enumerate and fingerprint a source's delta into its ledger"),
        ("resolve", cmd_resolve, "decide rows that are provably in the base or duplicates"),
    ]:
        p = sub.add_parser(name, help=hlp)
        p.add_argument("source", nargs="*")
        p.set_defaults(fn=fn)
    sub.choices["fetch"].add_argument(
        "--base-only", action="store_true", help="shallow-fetch only the line's base tag (CI: verify, apply)")
    sub.choices["resolve"].add_argument(
        "--accept-proposed", action="store_true",
        help="also accept subject-only matches (weak evidence; review the diff)")
    sub.add_parser("status", help="disposition counts per source").set_defaults(fn=cmd_status)
    p = sub.add_parser("why", help="look up a commit by sha, change id, upstream sha or subject text")
    p.add_argument("query")
    p.set_defaults(fn=cmd_why)
    p = sub.add_parser("verify", help="check ledger invariants")
    p.add_argument("--strict", action="store_true", help="fail on undecided rows (CI on main)")
    p.add_argument("--fixes", metavar="REF", help="also fail on missing upstream Fixes: follow-ups in REF")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("lines", help="list lines: name, base tag, kind, patch count (tab-separated)")
    p.add_argument("--kind", choices=["stable", "ubuntu"])
    p.set_defaults(fn=None)
    sub.add_parser("updates", help="what moved upstream: newer base tags and vendor sources past their pins "
                                   "(all lines, or --line; exit 10 if anything moved)")
    p = sub.add_parser("cves", help="kernel CVEs the line is affected by and does not fix")
    p.add_argument("--update", action="store_true", help="get or refresh the CVE records first")
    p.add_argument("--used-files", help="sbom.used-files.txt of a build: mark CVEs whose files were not compiled")
    p.add_argument("--limit", type=int, default=30, help="rows to print (default 30)")
    p.add_argument("--severity", choices=["critical", "high", "medium", "low", "unscored"], default="unscored",
                   help="list only CVEs rated this or worse (default: all, also those with no rating)")
    p.set_defaults(fn=cmd_cves)
    p = sub.add_parser("add", help="carry commits of our own choosing (e.g. mainline fixes) in the local band")
    p.add_argument("sha", nargs="+")
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("apply", help="materialise base + series as a plain source tree")
    p.add_argument("tree")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("rebase", help="move the line to a new base tag")
    p.add_argument("tag", help="tag of the line's base source, e.g. v7.3.5")
    p.set_defaults(fn=cmd_rebase)
    p = sub.add_parser("config", help="compare vendor configs with Ubuntu master and compute the union")
    p.add_argument("--write", action="store_true", help="write config/union.json for the packaging renderer")
    p.add_argument("--all", action="store_true", help="list every option, not just the first few")
    p.set_defaults(fn=cmd_config)
    p = sub.add_parser("decide", help="set a disposition on rows by change id, sha, or --source selector")
    p.add_argument("disposition", choices=["carry", "dropped", "deferred", "superseded", "undecided"])
    p.add_argument("ids", nargs="*", help="change ids (C-00123) or sha prefixes")
    p.add_argument("--source", help="select undecided rows of this source")
    p.add_argument("--subject", help="with --source: only subjects matching this regex")
    p.add_argument("--reason")
    p.add_argument("--by", help="for superseded: what replaces it")
    p.add_argument("--of", help=argparse.SUPPRESS)
    p.add_argument("--force", action="store_true", help="with --source: also change already-decided rows")
    p.set_defaults(fn=cmd_decide)
    p = sub.add_parser("import", help="build the workbench branch from carry rows (needs a worktree; Linux)")
    p.add_argument("--continue", dest="cont", action="store_true", help="resume after resolving a conflict")
    p.set_defaults(fn=cmd_import)
    sub.add_parser("export", help="write the workbench branch out as patch files + series").set_defaults(fn=cmd_export)
    p = sub.add_parser("packaging", help="render debian.<vendor>/ into a kernel tree from identity.toml")
    p.add_argument("tree", help="kernel tree (base + series); Ubuntu's debian/ is overlaid from its pin")
    p.add_argument("--series", required=True, help="Ubuntu series to build for, e.g. resolute")
    p.set_defaults(fn=cmd_packaging)

    args = ap.parse_args(argv)
    if args.cmd == "lines":
        return cmd_lines(args)
    if args.cmd == "updates":
        return cmd_updates(args)
    cfg = config.load(line=args.line)
    try:
        return args.fn(cfg, Git(cfg.repo), args) or 0
    except GitError as e:
        print(f"kpm: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
