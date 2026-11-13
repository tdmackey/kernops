#!/usr/bin/env python3
"""Measure what a kernel build ships, grouped by purpose.

Reads the file list of a linux-modules package (or a directory of modules) and
sorts every module into a group from kernel/lines/<line>/config/surface.tsv.
Each group has a verdict for our use (cloud instances and modern servers):

  keep     we need it
  drop     no cloud instance or server we target has this hardware or use
  review   depends on the hardware we deploy; decide with an inventory

Prints one row per group with the number of modules and their size, and the
totals per verdict. With --list GROUP it prints the modules of one group.

Usage: config-surface.py <line> <linux-modules .deb | modules directory> [--list GROUP] [--json FILE]
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def modules_of(path):
    """[(path below kernel/, size in bytes)] for every module."""
    out = []
    if os.path.isdir(path):
        for d, _dirs, files in os.walk(path):
            for f in files:
                if re.search(r"\.ko(\.\w+)?$", f):
                    full = os.path.join(d, f)
                    rel = full.split("/kernel/", 1)[-1] if "/kernel/" in full else os.path.relpath(full, path)
                    out.append((rel, os.path.getsize(full)))
        return out
    listing = subprocess.run(["dpkg-deb", "-c", path], capture_output=True, text=True, check=True).stdout
    for line in listing.splitlines():
        f = line.split()
        if len(f) >= 6 and re.search(r"\.ko(\.\w+)?$", f[5]) and "/kernel/" in f[5]:
            out.append((f[5].split("/kernel/", 1)[1], int(f[2])))
    return out


def load_rules(line):
    path = os.path.join(ROOT, "kernel", "lines", line, "config", "surface.tsv")
    rules = []
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            if not raw.strip() or raw.startswith("#"):
                continue
            parts = raw.rstrip("\n").split("\t")
            if len(parts) != 4 or parts[1] not in ("keep", "drop", "review"):
                sys.exit(f"{path}:{n}: need <path regex><TAB>keep|drop|review<TAB>group<TAB>reason")
            rules.append((re.compile(parts[0]), parts[1], parts[2], parts[3]))
    return rules


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("line")
    ap.add_argument("modules")
    ap.add_argument("--list", metavar="GROUP")
    ap.add_argument("--json")
    args = ap.parse_args()

    rules = load_rules(args.line)
    groups = collections.OrderedDict()
    unmatched = []
    mods = modules_of(args.modules)
    for rel, size in mods:
        for rx, verdict, group, reason in rules:              # first match wins
            if rx.search(rel):
                g = groups.setdefault(group, {"verdict": verdict, "reason": reason, "modules": 0, "bytes": 0, "files": []})
                g["modules"] += 1
                g["bytes"] += size
                g["files"].append(rel)
                break
        else:
            unmatched.append(rel)

    if args.list:
        for f in sorted(groups.get(args.list, {}).get("files", [])):
            print(f)
        return 0

    total_n, total_b = len(mods), sum(s for _r, s in mods) or 1
    print(f"{total_n} modules, {total_b / 1e6:.0f} MB (compressed, as packaged)\n")
    print(f"{'group':<34}{'verdict':<8}{'modules':>8}{'MB':>8}  reason")
    order = {"drop": 0, "review": 1, "keep": 2}
    for name, g in sorted(groups.items(), key=lambda kv: (order[kv[1]["verdict"]], -kv[1]["bytes"])):
        print(f"{name:<34}{g['verdict']:<8}{g['modules']:>8}{g['bytes'] / 1e6:>8.1f}  {g['reason']}")
    print()
    for v in ("drop", "review", "keep"):
        n = sum(g["modules"] for g in groups.values() if g["verdict"] == v)
        b = sum(g["bytes"] for g in groups.values() if g["verdict"] == v)
        print(f"{v:<8}{n:>6} modules ({100 * n / total_n:4.1f}%) {b / 1e6:>7.1f} MB ({100 * b / total_b:4.1f}%)")
    if unmatched:
        print(f"\n{len(unmatched)} modules match no rule, for example: {', '.join(sorted(unmatched)[:8])}")
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"modules": total_n, "bytes": total_b, "unmatched": sorted(unmatched),
                       "groups": {k: {x: y for x, y in g.items()} for k, g in groups.items()}}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
