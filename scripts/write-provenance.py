#!/usr/bin/env python3
"""Write a release provenance manifest for a gb200 build output directory."""
import argparse
import datetime
import hashlib
import json
import os
import subprocess


def run(cmd, cwd=None):
    try:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_pin(repo_root, base):
    path = os.path.join(repo_root, "kernel", "upstream-base.txt")
    for line in open(path):
        line = line.split("#", 1)[0].rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == base:
            return parts[1]
    return ""


def patch_records(repo_root, base):
    pdir = os.path.join(repo_root, "kernel", "patches", "gb200", base)
    rows = []
    if not os.path.isdir(pdir):
        return rows
    for name in sorted(os.listdir(pdir)):
        if not name.endswith(".patch"):
            continue
        path = os.path.join(pdir, name)
        rows.append({"path": os.path.relpath(path, repo_root),
                     "sha256": sha256(path)})
    return rows


def module_rows(repo_root, base, arch):
    path = os.path.join(repo_root, "modules", "matrix.tsv")
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 5 and parts[0] == base and parts[1] in (arch, "all"):
            rows.append({"name": parts[2], "version": parts[3], "source": parts[4],
                         "arch": parts[1]})
        elif len(parts) == 4 and parts[0] == base and arch == "arm64":
            rows.append({"name": parts[1], "version": parts[2], "source": parts[3]})
    return rows


def module_abi_records(out_dir):
    rows = []
    mod_dir = os.path.join(out_dir, "modules")
    if not os.path.isdir(mod_dir):
        return rows
    for name in sorted(os.listdir(mod_dir)):
        if not name.endswith("-abi.json"):
            continue
        path = os.path.join(mod_dir, name)
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            data = {"schema": "gb200.module_abi.v1", "error": "unparseable"}
        data["path"] = os.path.relpath(path, out_dir)
        rows.append(data)
    return rows


def artifact_records(out_dir):
    manifest = os.path.join(out_dir, ".publish-manifest")
    rows = []
    if not os.path.exists(manifest):
        return rows
    for line in open(manifest):
        rel = line.strip()
        if not rel or rel.startswith("#"):
            continue
        rel = rel[2:] if rel.startswith("./") else rel
        path = os.path.join(out_dir, rel)
        rows.append({"path": rel, "bytes": os.path.getsize(path),
                     "sha256": sha256(path)})
    return rows


def builder_image_record(series):
    image = f"localhost/gb200-builder:{series}"
    raw = run(["podman", "image", "inspect", image])
    if not raw:
        return {"image": image, "inspect": "unavailable"}
    try:
        data = json.loads(raw)[0]
    except Exception:
        return {"image": image, "inspect": "unparseable"}
    digest = data.get("Digest", "")
    if not digest:
        repo_digests = data.get("RepoDigests") or [""]
        digest = repo_digests[0]
    return {"image": image, "id": data.get("Id", ""),
            "digest": digest, "architecture": data.get("Architecture", "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--kernel-repo", required=True)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--arch", required=True)
    ap.add_argument("--flavour", required=True)
    ap.add_argument("--series", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()

    upstream_tag = read_pin(args.repo_root, args.base)
    manifest = {
        "schema": "gb200.provenance.v1",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "base": args.base,
        "arch": args.arch,
        "upstream_tag": upstream_tag,
        "flavour": args.flavour,
        "series": args.series,
        "run_id": args.run_id,
        "release_profile": bool(os.environ.get("RELEASE")),
        "gb200_repo": {
            "commit": run(["git", "rev-parse", "HEAD"], cwd=args.repo_root),
            "dirty": bool(run(["git", "status", "--porcelain"], cwd=args.repo_root)),
        },
        "kernel_repo": {
            "path": args.kernel_repo,
            "upstream_tag_commit": run(["git", "rev-parse", f"{upstream_tag}^{{commit}}"],
                                       cwd=args.kernel_repo),
            "reconstructed_head": run(["git", "rev-parse", "HEAD"], cwd=args.tree),
        },
        "builder": builder_image_record(args.series),
        "patches": patch_records(args.repo_root, args.base),
        "modules": module_rows(args.repo_root, args.base, args.arch),
        "module_abi": module_abi_records(args.out_dir),
        "artifacts": artifact_records(args.out_dir),
    }
    out = os.path.join(args.out_dir, "gb200-provenance.json")
    with open(out, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f">> provenance: {out}")


if __name__ == "__main__":
    main()
