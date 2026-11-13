#!/usr/bin/env python3
"""Which carried patches reach a build.

Joins a line's patch series with the list of source files a kernel build
actually used (sbom.used-files.txt, written by the kernel's scripts/sbom/sbom.py)
and sorts every patch into one of:

  compiled   it changes a source file that was used in this build
  build      it only changes Kconfig/Makefile/Kbuild files (it steers the build)
  packaging  it only changes debian*/ files
  other      it only changes documentation, tools, selftests, scripts or samples
  unused     it changes source files, none of which this configuration compiles

Usage: sbom-patches.py <line> <used-files.txt> [-o patches.json]
"""
import argparse
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIFF = re.compile(r"^diff --git a/(\S+) b/(\S+)")
TRAILER = re.compile(r"^(Ledger-Change|Ledger-Origin): (.+)$")
BUILD_NAMES = ("Kconfig", "Makefile", "Kbuild")
OTHER_DIRS = ("Documentation/", "tools/", "scripts/", "samples/", "LICENSES/")


def read_patch(path):
    files, meta, subject = [], {}, ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = DIFF.match(line)
            if m:
                files.append(m.group(2))
                continue
            if not files:
                if line.startswith("Subject: ") and not subject:
                    subject = re.sub(r"^\[PATCH[^\]]*\]\s*", "", line[9:].strip())
                m = TRAILER.match(line)
                if m:
                    meta[m.group(1)] = m.group(2).strip()
    return subject, meta, sorted(set(files))


def kind(path):
    if path.startswith("debian"):
        return "packaging"
    base = os.path.basename(path)
    if base.startswith(BUILD_NAMES) or base.endswith((".mk", "defconfig")):
        return "build"
    if path.startswith(OTHER_DIRS):
        return "other"
    return "source"


def classify(files, used):
    hit = [f for f in files if f in used]
    if hit:
        return "compiled", hit
    kinds = {kind(f) for f in files}
    if "source" in kinds:
        return "unused", []
    for k in ("build", "packaging", "other"):
        if k in kinds:
            return k, []
    return "other", []


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("line")
    ap.add_argument("used_files")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()

    line_dir = os.path.join(ROOT, "kernel", "lines", args.line)
    with open(args.used_files, encoding="utf-8") as f:
        used = {os.path.normpath(l.strip()) for l in f if l.strip()}
    if used and not any(not os.path.isabs(u) and os.path.dirname(u) for u in used):
        sys.exit(f"{args.used_files}: expected paths relative to the kernel tree")
    with open(os.path.join(line_dir, "series"), encoding="utf-8") as f:
        series = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    rows, summary = [], collections.OrderedDict()
    for rel in series:
        subject, meta, files = read_patch(os.path.join(line_dir, "patches", rel))
        status, hit = classify(files, used)
        band = rel.split("/", 1)[0]
        summary.setdefault(band, collections.Counter())[status] += 1
        rows.append({
            "patch": rel, "band": band, "subject": subject,
            "change": meta.get("Ledger-Change"), "origin": meta.get("Ledger-Origin"),
            "status": status, "files": files, "used_files": hit,
        })

    cols = ("compiled", "build", "packaging", "other", "unused")
    print(f"{'band':<18}{'patches':>8}" + "".join(f"{c:>11}" for c in cols))
    total = collections.Counter()
    for band, c in summary.items():
        total.update(c)
        print(f"{band:<18}{sum(c.values()):>8}" + "".join(f"{c[k]:>11}" for k in cols))
    print(f"{'total':<18}{sum(total.values()):>8}" + "".join(f"{total[k]:>11}" for k in cols))

    if args.output:
        doc = {"line": args.line, "used_files": len(used),
               "summary": {b: dict(c) for b, c in summary.items()}, "patches": rows}
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=1)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
