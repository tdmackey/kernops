#!/usr/bin/env python3
"""Create the treadmill PR/report body."""
import argparse
import os
import re


RISK_PREFIXES = (
    "arch/arm64/",
    "block/",
    "drivers/iommu/",
    "drivers/firmware/",
    "include/linux/",
    "kernel/",
    "mm/",
    "security/",
)


def patch_info(repo_root, base):
    pdir = os.path.join(repo_root, "kernel", "patches", "gb200", base)
    rows = []
    if not os.path.isdir(pdir):
        return rows
    for name in sorted(os.listdir(pdir)):
        if not name.endswith(".patch"):
            continue
        text = open(os.path.join(pdir, name), encoding="utf-8", errors="replace").read()
        subject = ""
        m = re.search(r"^Subject: (?:\[[^\]]*\] ?)?(.+)", text, re.M)
        if m:
            subject = m.group(1).strip()
        files = re.findall(r"^diff --git a/(\S+)", text, re.M)
        kind = "SAUCE"
        if "cherry picked from commit" in text:
            kind = "cherry picked"
        elif "backported from commit" in text:
            kind = "backported"
        elif subject.startswith("UBUNTU: [Packaging]"):
            kind = "packaging"
        rows.append({
            "file": name,
            "subject": subject,
            "kind": kind,
            "risk": any(f.startswith(RISK_PREFIXES) for f in files),
        })
    return rows


def module_rows(repo_root, base):
    path = os.path.join(repo_root, "modules", "matrix.tsv")
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 5 and parts[0] == base:
            rows.append(parts[1:5])
        elif len(parts) == 4 and parts[0] == base:
            rows.append(["arm64", parts[1], parts[2], parts[3]])
    return rows


def read_range_diff(path, limit):
    if not path or not os.path.exists(path):
        return "n/a"
    data = open(path, encoding="utf-8", errors="replace").read(limit + 1)
    if len(data) > limit:
        return data[:limit] + "\n... truncated ...\n"
    return data or "empty"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--old", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--range-diff", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    patches = patch_info(args.repo_root, args.base)
    modules = module_rows(args.repo_root, args.base)
    risk = sum(1 for p in patches if p["risk"])
    sauce = sum(1 for p in patches if p["kind"] == "SAUCE")

    out = [
        "<!-- gb200-treadmill-report -->",
        f"Archive published **{args.version}** for `{args.base}`.",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| previous pin | `{args.old}` |",
        f"| new pin | `{args.target}` |",
        f"| patch count | {len(patches)} |",
        f"| risky-path patches | {risk} |",
        f"| SAUCE patches | {sauce} |",
        f"| module targets | {len(modules)} |",
        "",
        "kernel-ci on this PR is the gate.",
        "",
        "### Module Matrix",
        "",
    ]
    if modules:
        out.extend(["| arch | module | version | source |", "| --- | --- | --- | --- |"])
        for arch, name, version, source in modules:
            out.append(f"| `{arch}` | `{name}` | `{version}` | `{source}` |")
    else:
        out.append("No module rows found.")

    out.extend([
        "",
        "<details><summary>range-diff</summary>",
        "",
        "```",
        read_range_diff(args.range_diff, 60000),
        "```",
        "",
        "</details>",
        "",
    ])
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


if __name__ == "__main__":
    main()
