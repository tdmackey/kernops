"""Compare vendor kernel configs (Ubuntu annotations) and compute the union.

Each Ubuntu derivative keeps its config in debian.<name>/config/annotations: a
list of per-option policies that override debian.master's. We read every
source's file at its pinned ref and work out, for our three builds, which
options a vendor turns up relative to Ubuntu master (must be on for a unified
kernel), which it turns down (size trimming; ignored), and which differ in a
non-tristate value (needs a human).
"""
import json
import re
from ast import literal_eval

from . import core
from .config import Config
from .git import Git

_LINE = re.compile(r"^(CONFIG_\w+)\s+policy<(.*)>\s*$")
_RANK = {"y": 3, "m": 2, "n": 1, "-": 0}
# Values that describe the build machine, not a choice.
_TOOLCHAIN = re.compile(r"^CONFIG_(CC_|GCC_|CLANG_|LD_|AS_|RUSTC_|PAHOLE_|LLD_|BINDGEN_|TOOLS_SUPPORT_|HAVE_|ARCH_HAS_|ARCH_SUPPORTS_)")
TARGETS = [("amd64", ""), ("arm64", ""), ("arm64", "-64k")]     # (arch, flavour suffix)


def parse(text: str) -> dict[str, dict]:
    out = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = literal_eval(m.group(2))
    return out


def effective(policy: dict | None, arch: str, flavour: str) -> str:
    if not policy:
        return "-"
    return str(policy.get(f"{arch}-{flavour}", policy.get(arch, "-")))


def target_key(arch: str, suffix: str) -> str:
    return f"{arch}{suffix}"


def compare(cfg: Config, git: Git) -> dict:
    pins = core.pins(cfg)
    vendors = {n: cfg.sources[n] for n in cfg.config_sources() if n in pins}
    files = {n: parse(git.run("show", f"{pins[n]['sha']}:{s.packaging_dir}/config/annotations"))
             for n, s in vendors.items()}
    if cfg.kind == "stable":
        root = next(n for n, s in vendors.items() if not s.parent)          # ubuntu
        master = files[root]
    else:
        # The line is on an Ubuntu kernel: that kernel's own config is the master.
        root = None
        master = parse(git.run("show", f"{cfg.base_ref}:debian.master/config/annotations"))

    def master_value(opt, arch, suffix):
        return effective(master.get(opt), arch, f"generic{suffix}")

    union, review, lowered = {}, {}, {}
    for name, s in vendors.items():
        if name == root:
            continue
        flav = s.packaging_dir.split(".", 1)[1]
        lowered[name] = 0
        for opt, policy in files[name].items():
            for arch, suffix in TARGETS:
                mv = master_value(opt, arch, suffix)
                vv = effective(policy, arch, f"{flav}{suffix}")
                if vv == mv:
                    continue
                key = target_key(arch, suffix)
                if vv in _RANK and mv in _RANK:
                    if _RANK[vv] > _RANK[mv]:
                        cur = union.setdefault(opt, {"wanted_by": [], "master": {}, "value": {}})
                        if _RANK[vv] > _RANK.get(cur["value"].get(key, "-"), 0):
                            cur["value"][key] = vv
                        cur["master"][key] = mv
                        if name not in cur["wanted_by"]:
                            cur["wanted_by"].append(name)
                    else:
                        lowered[name] += 1
                elif not _TOOLCHAIN.match(opt):
                    review.setdefault(opt, {"master": {}, "vendors": {}})
                    review[opt]["master"][key] = mv
                    review[opt]["vendors"].setdefault(name, {})[key] = vv
    return {"vendors": {n: {"ref": pins[n]["ref"], "overrides": len(files[n])} for n in vendors if n != root},
            "master_options": len(master), "union": dict(sorted(union.items())),
            "review": dict(sorted(review.items())), "lowered": lowered}


def write_union(cfg: Config, result: dict) -> str:
    path = cfg.line_dir / "config" / "union.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {opt: {"value": v["value"], "wanted_by": v["wanted_by"]} for opt, v in result["union"].items()}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
    return str(path)


def apply_local(union: dict, local: dict) -> dict:
    """Overlay our own config decisions on the vendor union. Local wins outright."""
    out = dict(union)
    for opt, entry in local.items():
        value = {k: v for k, v in entry.items() if k != "note"}
        if "arm64" in value and "arm64-64k" not in value:
            value["arm64-64k"] = value["arm64"]
        out[opt] = {"value": value, "note": entry["note"]}
    return out


def annotation_lines(union: dict, vendor: str, arches: list[str]) -> str:
    """Render union.json (plus local overrides) as annotations for debian.<vendor>."""
    out = []
    for opt, v in sorted(union.items()):
        policy = {}
        for arch in arches:
            if arch in v["value"]:
                policy[arch] = v["value"][arch]
        k64 = v["value"].get("arm64-64k")
        if "arm64" in arches and k64 and k64 != policy.get("arm64"):
            policy[f"arm64-{vendor}-64k"] = k64
        if not policy:
            continue
        out.append(f"{opt:<47} policy<{policy!r}>")
        note = v.get("note") or f"kpm union: wanted by {' '.join(v['wanted_by'])}"
        out.append(f"{opt:<47} note<{note!r}>")
        out.append("")
    return "\n".join(out)
