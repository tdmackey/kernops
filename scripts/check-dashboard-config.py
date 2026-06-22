#!/usr/bin/env python3
"""Validate dashboard configuration against the repo's tracked targets."""
import argparse
import json
import os
import sys


VALID_ARCHES = {"arm64", "amd64"}


def read_pins(repo_root):
    pins = {}
    path = os.path.join(repo_root, "kernel", "upstream-base.txt")
    for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
        raw = line.rstrip("\n")
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            raise ValueError(f"{path}:{lineno}: malformed pin row: {raw}")
        pins[parts[0]] = parts[1]
    return pins


def read_module_targets(repo_root):
    targets = set()
    path = os.path.join(repo_root, "modules", "matrix.tsv")
    for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
        raw = line.rstrip("\n")
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 5:
            base, arch = parts[0], parts[1]
        elif len(parts) == 4:
            base, arch = parts[0], "arm64"
        else:
            raise ValueError(f"{path}:{lineno}: malformed module row: {raw}")
        targets.add((base, arch))
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="tools/dashboard/config.json")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    repo_root = os.environ.get("GB200_REPO_ROOT", cfg.get("repo_root"))
    if not repo_root or not os.path.isdir(repo_root):
        raise SystemExit(f"!! dashboard repo_root missing or not a directory: {repo_root}")
    arches = cfg.get("arches") or []
    invalid = sorted(set(arches) - VALID_ARCHES)
    if invalid:
        raise SystemExit(f"!! dashboard config has unsupported arches: {', '.join(invalid)}")
    if not arches:
        raise SystemExit("!! dashboard config has no arches")

    pins = read_pins(repo_root)
    module_targets = read_module_targets(repo_root)
    bases = cfg.get("bases") or []
    seen = set()
    errors = []
    for idx, base in enumerate(bases, 1):
        name = base.get("name")
        if not name:
            errors.append(f"base entry {idx} has no name")
            continue
        if name in seen:
            errors.append(f"duplicate dashboard base: {name}")
        seen.add(name)
        if name not in pins:
            errors.append(f"{name}: missing from kernel/upstream-base.txt")
        for key in ("clone", "tag_glob", "osv_ecosystem", "package"):
            if not base.get(key):
                errors.append(f"{name}: missing {key}")
        for arch in arches:
            if (name, arch) not in module_targets:
                errors.append(f"{name}/{arch}: missing modules/matrix.tsv rows")
    extra_pins = sorted(set(pins) - seen)
    if extra_pins:
        errors.append("pins missing from dashboard config: " + ", ".join(extra_pins))
    if errors:
        for err in errors:
            print(f"!! {err}", file=sys.stderr)
        return 1
    print(f"dashboard config: ok ({len(bases)} bases, {len(arches)} arches)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
