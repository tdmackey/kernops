#!/usr/bin/env python3
"""Write a concise Markdown summary for release artifacts/provenance."""
import argparse
import json
import os


def find_provenance(root):
    rows = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        if "gb200-provenance.json" in files:
            path = os.path.join(cur, "gb200-provenance.json")
            data = json.load(open(path, encoding="utf-8"))
            data["_path"] = path
            rows.append(data)
    rows.sort(key=lambda d: (d.get("base", ""), d.get("arch", ""), d.get("run_id", "")))
    return rows


def artifact_name(path):
    return os.path.basename(path)


def module_abi_by_name(provenance):
    out = {}
    for abi in provenance.get("module_abi", []):
        out[abi.get("name", "?")] = abi
    return out


def render(rows, title):
    out = [f"## {title}", ""]
    if not rows:
        out.append("No provenance manifests found.")
        return "\n".join(out) + "\n"
    for row in rows:
        out.append(f"### {row['base']} / {row['arch']}")
        out.append("")
        out.append(f"- upstream: `{row.get('upstream_tag', '?')}`")
        out.append(f"- flavour: `{row.get('flavour', '?')}`")
        out.append(f"- run: `{row.get('run_id', '?')}`")
        builder = row.get("builder", {})
        digest = builder.get("digest") or builder.get("id") or "unavailable"
        out.append(f"- builder: `{builder.get('image', '?')}` `{digest}`")
        out.append(f"- patches: {len(row.get('patches', []))}")

        artifacts = row.get("artifacts", [])
        if artifacts:
            out.append("")
            out.append("| artifact | bytes | sha256 |")
            out.append("| --- | ---: | --- |")
            for artifact in artifacts:
                out.append(
                    f"| `{artifact_name(artifact['path'])}` | {artifact.get('bytes', 0)} | "
                    f"`{artifact.get('sha256', '')[:16]}` |")

        abi = module_abi_by_name(row)
        if abi:
            out.append("")
            out.append("| module | .ko count | signer | vermagic |")
            out.append("| --- | ---: | --- | --- |")
            for name, meta in sorted(abi.items()):
                modules = meta.get("modules", [])
                first = modules[0].get("modinfo", {}) if modules else {}
                signer = first.get("signer") or ("unsigned" if modules else "?")
                vermagic = first.get("vermagic", "?")
                out.append(f"| `{name}` | {len(modules)} | `{signer}` | `{vermagic}` |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--title", default="gb200 Release Summary")
    args = ap.parse_args()

    rows = []
    for root in args.roots:
        if os.path.isdir(root):
            rows.extend(find_provenance(root))
    print(render(rows, args.title), end="")


if __name__ == "__main__":
    main()
