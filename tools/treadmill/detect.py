#!/usr/bin/env python3
"""Detect new Ubuntu kernel publications for every base we track.

Queries the Launchpad publishing API (the stable public signal — NOT
cgit/Forgejo scraping, per the master plan) for the newest Published
version of each base's source package, maps version -> git tag, and
compares against the pin in kernel/upstream-base.txt.

Usage: detect.py [--json]
Exit code: 0 = all current, 10 = at least one base is stale,
2 = publication data could not be checked.

Stdlib only. Each API call is one small JSON request.
"""
import json
import os
import sys
import urllib.request
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
MONO = os.path.dirname(os.path.dirname(HERE))
LP_API = "https://api.launchpad.net/1.0/ubuntu/+archive/primary"
SERIES = "noble"
# pockets that matter for production; -proposed is early-warning only
POCKETS = ("Updates", "Security", "Release")


def read_pins():
    pins = {}
    for line in open(os.path.join(MONO, "kernel", "upstream-base.txt")):
        line = line.split("#", 1)[0].rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            pins[parts[0]] = parts[1]
    return pins


def read_base_cfg():
    cfg = json.load(open(os.path.join(MONO, "tools", "dashboard", "config.json")))
    return {b["name"]: b for b in cfg["bases"]}


def lp_published_version(source_name):
    """Newest Published version across the pockets we ship from."""
    q = urllib.parse.urlencode({
        "ws.op": "getPublishedSources",
        "source_name": source_name,
        "exact_match": "true",
        "distro_series": f"/ubuntu/{SERIES}",
        "status": "Published",
        "order_by_date": "true",
    })
    req = urllib.request.Request(f"{LP_API}?{q}",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        entries = json.load(r).get("entries", [])
    best = None
    for e in entries:
        if e.get("pocket") not in POCKETS:
            continue
        v = e["source_package_version"]
        if best is None or debian_compare(v, best["version"]) > 0:
            best = {"version": v, "pocket": e["pocket"],
                    "published": (e.get("date_published") or "")[:10]}
    return best


def _split_deb_version(version):
    if ":" in version:
        epoch_s, rest = version.split(":", 1)
        epoch = int(epoch_s or 0)
    else:
        epoch, rest = 0, version
    if "-" in rest:
        upstream, debian = rest.rsplit("-", 1)
    else:
        upstream, debian = rest, ""
    return epoch, upstream, debian


def _order_char(ch):
    if ch == "~":
        return -1
    if ch == "":
        return 0
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _verrevcmp(left, right):
    li = ri = 0
    llen, rlen = len(left), len(right)
    while li < llen or ri < rlen:
        while (li < llen and not left[li].isdigit()) or \
              (ri < rlen and not right[ri].isdigit()):
            lc = left[li] if li < llen else ""
            rc = right[ri] if ri < rlen else ""
            lo, ro = _order_char(lc), _order_char(rc)
            if lo != ro:
                return -1 if lo < ro else 1
            if lc:
                li += 1
            if rc:
                ri += 1

        while li < llen and left[li] == "0":
            li += 1
        while ri < rlen and right[ri] == "0":
            ri += 1

        lstart, rstart = li, ri
        while li < llen and left[li].isdigit():
            li += 1
        while ri < rlen and right[ri].isdigit():
            ri += 1

        llen_digits, rlen_digits = li - lstart, ri - rstart
        if llen_digits != rlen_digits:
            return -1 if llen_digits < rlen_digits else 1
        if left[lstart:li] != right[rstart:ri]:
            return -1 if left[lstart:li] < right[rstart:ri] else 1
    return 0


def debian_compare(left, right):
    le, lu, ld = _split_deb_version(left)
    re, ru, rd = _split_deb_version(right)
    if le != re:
        return -1 if le < re else 1
    upstream = _verrevcmp(lu, ru)
    if upstream:
        return upstream
    return _verrevcmp(ld, rd)


def version_to_tag(tag_prefix, version):
    """Ubuntu tag = <tag_prefix> + version with '~' -> '_'.
       e.g. Ubuntu-hwe-6.17- + 6.17.0-38.38~24.04.1
            -> Ubuntu-hwe-6.17-6.17.0-38.38_24.04.1"""
    return tag_prefix + version.replace("~", "_")


def tag_to_version(tag, tag_prefix):
    return tag[len(tag_prefix):].replace("_", "~") if tag.startswith(tag_prefix) else tag


def main():
    as_json = "--json" in sys.argv
    pins, cfg = read_pins(), read_base_cfg()
    rows, stale, errors = [], False, False
    for base, pin in pins.items():
        b = cfg.get(base, {})
        src, prefix = b.get("package"), b.get("tag_prefix", "")
        row = {"base": base, "pin": pin, "source": src}
        if not src or not prefix:
            row["status"] = "no-source-mapping"
        else:
            try:
                pub = lp_published_version(src)
            except Exception as exc:
                row["status"] = f"api-error: {exc}"
                errors = True
                pub = None
            if pub:
                row.update(pub)
                row["expected_tag"] = version_to_tag(prefix, pub["version"])
                pin_v, pub_v = tag_to_version(pin, prefix), pub["version"]
                if row["expected_tag"] == pin:
                    row["status"] = "current"
                elif debian_compare(pin_v, pub_v) > 0:
                    # pinned to an unpublished (-proposed/staging) tag —
                    # production must come DOWN to the archive
                    row["status"] = "AHEAD-OF-ARCHIVE"
                    stale = True
                else:
                    row["status"] = "STALE"
                    stale = True
            elif "status" not in row:
                row["status"] = "no-publication-found"
        rows.append(row)

    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['base']:<12} pin={r['pin']}")
            if "version" in r:
                print(f"{'':<12} archive={r['version']} ({r['pocket']}, "
                      f"{r['published']}) -> {r['expected_tag']}")
            print(f"{'':<12} status={r['status']}")
    sys.exit(2 if errors else 10 if stale else 0)


if __name__ == "__main__":
    main()
