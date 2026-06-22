#!/usr/bin/env python3
"""Lightweight hardening checks for GitHub workflow files.

This intentionally stays text-based instead of depending on a YAML parser. The
repo only needs a few invariants: workflows must opt out of broad default token
permissions, avoid publish/build races, pin external actions by commit SHA, and
make checkout credentials explicit.
"""
import argparse
import os
import re
import sys


SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
USES_RE = re.compile(r"^(\s*)uses:\s*([^#\s]+)")


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def line_indent(line):
    return len(line) - len(line.lstrip(" "))


def block_after(lines, start_idx, indent):
    for line in lines[start_idx + 1:]:
        stripped = line.strip()
        if stripped and line_indent(line) <= indent and not stripped.startswith("#"):
            break
        yield line


def check_file(path):
    rel = os.path.relpath(path)
    lines = open(path, encoding="utf-8").read().splitlines()
    errors = []

    if not any(line.startswith("permissions:") for line in lines):
        errors.append(f"{rel}: missing top-level permissions")
    if not any(line.startswith("concurrency:") for line in lines):
        errors.append(f"{rel}: missing top-level concurrency")

    for idx, line in enumerate(lines):
        m = USES_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        value = unquote(m.group(2))
        if value.startswith("./"):
            continue
        if "@" not in value:
            errors.append(f"{rel}:{idx + 1}: external action is not ref-pinned: {value}")
            continue
        action, ref = value.rsplit("@", 1)
        if not SHA_RE.match(ref):
            errors.append(f"{rel}:{idx + 1}: action must be pinned to a 40-char SHA: {value}")
        if action == "actions/checkout":
            block = "\n".join(block_after(lines, idx, indent))
            if not re.search(r"^\s*persist-credentials:\s*false\s*$", block, re.M):
                errors.append(f"{rel}:{idx + 1}: actions/checkout must set persist-credentials: false")
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workflows", nargs="+")
    args = ap.parse_args()

    errors = []
    for path in args.workflows:
        errors.extend(check_file(path))
    if errors:
        for err in errors:
            print(f"!! {err}", file=sys.stderr)
        return 1
    print(f"workflow hardening: ok ({len(args.workflows)} workflows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
