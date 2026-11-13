#!/usr/bin/env python3
"""Print the download URLs of an Ubuntu source package's files.

Usage: lp-source.py <source-package> <series> <version>

Asks the Launchpad publishing API for the exact version published in the
series and prints one URL per line (.dsc first). Stdlib only.
Exit 1 if that version is not published there.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.launchpad.net/1.0/ubuntu/+archive/primary"
HEADERS = {"Accept": "application/json", "User-Agent": "kernel-userspace-backport"}


def get(url, tries=4):
    """Launchpad resets a connection now and then; try again before giving up."""
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=120) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code < 500:
                raise
            if attempt == tries - 1:
                sys.exit(f"!! Launchpad did not answer after {tries} tries: {e}")
            time.sleep(5 * (attempt + 1))


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    src, series, version = sys.argv[1:]
    q = urllib.parse.urlencode({"ws.op": "getPublishedSources", "source_name": src, "exact_match": "true",
                                "distro_series": f"/ubuntu/{series}", "version": version})
    # A superseded or deleted publication still lists its files; prefer a live one.
    entries = [e for e in get(f"{API}?{q}").get("entries", []) if e["source_package_version"] == version]
    entries.sort(key=lambda e: e.get("status") != "Published")
    if not entries:
        sys.exit(f"!! {src} {version} is not published in {series}")
    urls = get(entries[0]["self_link"] + "?ws.op=sourceFileUrls")
    for u in sorted(urls, key=lambda u: not u.endswith(".dsc")):
        print(u)


if __name__ == "__main__":
    main()
