#!/usr/bin/env python3
"""Print the download URLs of an Ubuntu source package's files.

Usage: lp-source.py <source-package> <series> <version>

Asks the Launchpad publishing API for the exact version published in the
series and prints one URL per line (.dsc first). Stdlib only.
Exit 1 if that version is not published there.
"""
import json
import sys
import urllib.parse
import urllib.request

API = "https://api.launchpad.net/1.0/ubuntu/+archive/primary"


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    src, series, version = sys.argv[1:]
    q = urllib.parse.urlencode({"ws.op": "getPublishedSources", "source_name": src, "exact_match": "true",
                                "distro_series": f"/ubuntu/{series}", "version": version})
    req = urllib.request.Request(f"{API}?{q}", headers={"Accept": "application/json",
                                                        "User-Agent": "kernel-userspace-backport"})
    with urllib.request.urlopen(req, timeout=120) as r:
        entries = [e for e in json.load(r).get("entries", []) if e["source_package_version"] == version]
    if not entries:
        sys.exit(f"!! {src} {version} is not published in {series}")
    req = urllib.request.Request(entries[0]["self_link"] + "?ws.op=sourceFileUrls",
                                 headers={"Accept": "application/json", "User-Agent": "kernel-userspace-backport"})
    with urllib.request.urlopen(req, timeout=120) as r:
        urls = json.load(r)
    for u in sorted(urls, key=lambda u: not u.endswith(".dsc")):
        print(u)


if __name__ == "__main__":
    main()
