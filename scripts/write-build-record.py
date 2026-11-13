#!/usr/bin/env python3
"""Write build-record.json: a short summary of one kernel build.

scripts/build-line.sh calls this in its last stage. The status page
(tools/dashboard/generate.py) reads the record; it does not look at the build
tree itself.

Usage: write-build-record.py --out DIR --line L --series S --arch A --version V
                             --base TAG [--tool name=version ...]
"""
import argparse
import datetime
import json
import os
import sys


def flavours(out):
    """One entry per flavour that has an SBOM directory."""
    rows = []
    sbom = os.path.join(out, "sbom")
    for name in sorted(os.listdir(sbom)) if os.path.isdir(sbom) else []:
        d = os.path.join(sbom, name)
        row = {"name": name}
        roots = os.path.join(d, "roots.txt")
        if os.path.exists(roots):
            with open(roots, encoding="utf-8") as f:
                row["modules"] = max(sum(1 for l in f if l.strip()) - 1, 0)   # the first root is the image
        used = os.path.join(d, "sbom.used-files.txt")
        if os.path.exists(used):
            with open(used, encoding="utf-8") as f:
                row["source_files"] = sum(1 for l in f if l.strip())
        patches = os.path.join(d, "patches.json")
        if os.path.exists(patches):
            with open(patches, encoding="utf-8") as f:
                summary = json.load(f).get("summary", {})
            total = {}
            for band in summary.values():
                for k, v in band.items():
                    total[k] = total.get(k, 0) + v
            row["patches"] = dict(total, total=sum(total.values()))
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for a in ("out", "line", "series", "arch", "version", "base"):
        ap.add_argument(f"--{a}", required=True)
    ap.add_argument("--tool", action="append", default=[], metavar="NAME=VERSION")
    ap.add_argument("--release", action="store_true", help="built with the release profile")
    ap.add_argument("--trusted-cert", action="append", default=[],
                    help="a certificate added to the kernel's trusted keys (subject and fingerprint)")
    args = ap.parse_args()

    record = {
        "line": args.line, "series": args.series, "arch": args.arch, "version": args.version, "base": args.base,
        "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "release": args.release, "trusted_certs": args.trusted_cert,
        "toolchain": dict(t.split("=", 1) for t in args.tool if "=" in t),
        "packages": sorted(f for f in os.listdir(args.out) if f.endswith(".deb")),
        "flavours": flavours(args.out),
    }
    path = os.path.join(args.out, "build-record.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, indent=1)
        f.write("\n")
    print(f"build record: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
