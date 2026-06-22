#!/usr/bin/env python3
"""Emit ABI metadata for built kernel modules."""
import argparse
import datetime
import hashlib
import json
import os
import subprocess


MODINFO_FIELDS = (
    "name",
    "version",
    "license",
    "description",
    "author",
    "srcversion",
    "depends",
    "retpoline",
    "intree",
    "vermagic",
    "signer",
    "sig_key",
    "sig_hashalgo",
)


def run(cmd):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "", 127
    return res.stdout.strip(), res.returncode


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def has_signature_marker(path):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 4096), os.SEEK_SET)
        return b"Module signature appended" in f.read()


def modinfo(path):
    data = {}
    available = False
    for field in MODINFO_FIELDS:
        out, rc = run(["modinfo", "-F", field, path])
        if rc == 0:
            available = True
        if not out:
            continue
        key = field.replace("-", "_")
        data[key] = [v for v in out.split(",") if v] if field == "depends" else out
    data["available"] = available
    return data


def undefined_symbols(path):
    out, rc = run(["nm", "-u", path])
    if rc != 0 or not out:
        return []
    symbols = []
    for line in out.splitlines():
        parts = line.split()
        if parts:
            symbols.append(parts[-1])
    return sorted(set(symbols))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--kver", required=True)
    ap.add_argument("--arch", required=True)
    ap.add_argument("--kernel-package-version", required=True)
    ap.add_argument("--ko-dir", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    modules = []
    for root, _, files in os.walk(args.ko_dir):
        for fname in sorted(files):
            if not fname.endswith(".ko"):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, args.ko_dir)
            mi = modinfo(path)
            modules.append({
                "file": rel,
                "bytes": os.path.getsize(path),
                "sha256": sha256(path),
                "signature_appended": has_signature_marker(path),
                "modinfo": mi,
                "undefined_symbols": undefined_symbols(path),
            })
    if not modules:
        raise SystemExit(f"!! no .ko files found in {args.ko_dir}")

    doc = {
        "schema": "gb200.module_abi.v1",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "name": args.name,
        "version": args.version,
        "kver": args.kver,
        "arch": args.arch,
        "kernel_package_version": args.kernel_package_version,
        "modules": modules,
    }
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f">> module ABI metadata: {args.output}")


if __name__ == "__main__":
    main()
