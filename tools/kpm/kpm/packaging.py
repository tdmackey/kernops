"""Render debian.<vendor>/ in a kernel tree from Ubuntu's debian.master/.

The result is a derivative packaging directory of the same shape Canonical uses
for linux-aws / linux-azure: its own changelog, flavours and rules, with config
annotations that include debian.master's and only list our architectures.
Nothing here is checked in to the kernel tree; it is regenerated per build.
"""
import json
import re
import shutil
import tomllib
from email.utils import formatdate
from pathlib import Path

_BRACKET = re.compile(r"\[([a-z0-9! ]+)\]")
_TOP = re.compile(r"^\S+ \((\d+\.\d+\.\d+)-")


def load_identity(mono: Path) -> dict:
    return tomllib.loads((mono / "kernel" / "identity.toml").read_text(encoding="utf-8"))


def flavours(ident: dict) -> dict[str, list[str]]:
    """flavour -> architectures. <vendor> everywhere, <vendor>-64k on arm64."""
    v = ident["vendor"]
    out = {v: list(ident["arches"])}
    if "arm64" in ident["arches"]:
        out[f"{v}-64k"] = ["arm64"]
    return out


def filter_arches(text: str, keep: list[str]) -> str:
    """Drop other architectures from control-file text.

    `[amd64 armhf s390x]` qualifiers are narrowed; a dependency line that only
    applied to dropped architectures is removed; `Architecture:` fields are
    narrowed. Negated qualifiers (`[!i386]`) are left alone."""
    out = []
    for line in text.split("\n"):
        if line.startswith("Architecture:"):
            archs = line.split(":", 1)[1].split()
            if not {"all", "any"} & set(archs):
                kept = [a for a in archs if a in keep]
                line = "Architecture: " + " ".join(kept or archs)
            out.append(line)
            continue
        drop = False

        def narrow(m):
            nonlocal drop
            toks = m.group(1).split()
            if any(t.startswith("!") for t in toks):
                return m.group(0)
            kept = [t for t in toks if t in keep]
            if not kept:
                drop = True
            return "[" + " ".join(kept) + "]"
        new = _BRACKET.sub(narrow, line)
        if drop and line.startswith(" ") and "|" not in line:
            continue
        out.append(line if drop else new)
    return "\n".join(out)


def version(ident: dict, tree: Path, base_tag: str, series: str) -> str:
    """The package version.

    kver = "stable": the kernel version is the base, e.g. 7.3.4-1.1~24.04.1.
    kver = "ubuntu": Ubuntu's convention, where the kernel version stays X.Y.0.
    The base is then added at the end, e.g. 7.3.0-1.1~24.04.1+7.3.4: a move to a
    new base must give a higher version, or a machine that has the old package
    never gets the new kernel. It is after the "~" part because Ubuntu's rules
    read the ABI and upload numbers from the text before it.

    A change of the patch series with no change of base needs a higher
    `upload` in identity.toml."""
    tail = f"~{ident['series'][series]}.{ident['respin']}"
    if ident["kver"] == "stable":
        return f"{base_tag.lstrip('v')}-{ident['abi']}.{ident['upload']}{tail}"
    m = _TOP.match((tree / "debian.master" / "changelog").read_text(encoding="utf-8"))
    return f"{m.group(1)}-{ident['abi']}.{ident['upload']}{tail}+{base_suffix(base_tag)}"


_UBUNTU_TAG = re.compile(r"^Ubuntu-(?:.*-)?\d+\.\d+\.\d+-(\d+(?:\.\d+)+)")


def base_suffix(base_tag: str) -> str:
    """The part of the version that says which base this is.

    v7.3.4 gives 7.3.4. An Ubuntu tag gives Ubuntu's ABI and upload numbers:
    Ubuntu-7.0.0-44.44 gives 44.44, which grows with each Ubuntu release."""
    m = _UBUNTU_TAG.match(base_tag)
    return m.group(1) if m else base_tag.lstrip("v")


_TOOLS = "$(toolspkgdir)/usr/lib/$(DEB_SOURCE)-tools-$(abi_release)"

# Edits to Ubuntu's shared debian/ rules. Each is (file, old, new, required).
# Ubuntu builds perf and bpftool only from the source package named "linux" and
# ships them as the unversioned linux-perf and bpftool packages. We build both
# from our own tree and install them in the versioned tools directory, as
# linux-nvidia does for perf, so they never collide with the archive's packages.
_RULE_EDITS = [
    ("debian/rules",
     "ifneq ($(DEB_SOURCE),linux)\n\tdo_tools_bpftool = false\n\tdo_tools_perf = false\nendif\n",
     "# kpm: perf and bpftool are built for this source package too\n"),
    ("debian/rules.d/2-binary-arch.mk",          # CoreSight trace decoding in perf on arm64
     "NO_LIBPERL=1 WERROR=0\n",
     "NO_LIBPERL=1 WERROR=0 $(if $(filter arm64,$(build_arch)),CORESIGHT=1)\n"),
    ("debian/rules.d/2-binary-arch.mk", "\tinstall -d $(perfpkgdir)/usr/bin\n", ""),
    ("debian/rules.d/2-binary-arch.mk", "$(perfpkgdir)/usr/bin/perf", _TOOLS + "/perf"),
    ("debian/rules.d/2-binary-arch.mk", "\tinstall -d $(perfpkgdir)/usr/lib\n", ""),
    ("debian/rules.d/2-binary-arch.mk", "$(perfpkgdir)/usr/lib/\n", _TOOLS + "/\n"),
    ("debian/rules.d/2-binary-arch.mk",
     "install -d $(perfpkgdir)/usr/lib/python3/dist-packages", "install -d " + _TOOLS + "/python"),
    ("debian/rules.d/2-binary-arch.mk",
     "$(perfpkgdir)/usr/lib/python3/dist-packages/", _TOOLS + "/python/"),
    ("debian/rules.d/2-binary-arch.mk", "\tinstall -d $(bpftoolpkgdir)/usr/sbin\n", ""),
    ("debian/rules.d/2-binary-arch.mk", "$(bpftoolpkgdir)/usr/sbin/bpftool", _TOOLS + "/bpftool"),
    ("debian/rules",                             # every deb: zstd members, stated explicitly
     "do_tools_perf_python?=false\n",              # so it does not depend on a series' dpkg default.
     "do_tools_perf_python?=false\n\n"            # Rules that pass -Z themselves still win
     "# kpm: deb members are zstd on every series unless a rule passes -Z itself\n"
     "export DPKG_DEB_COMPRESSOR_TYPE = zstd\n"),  # (image and modules debs stay -Znone).
    ("debian/rules.d/2-binary-arch.mk",          # debug symbol debs: zstd members, not xz
     "$(call dh_all,$(dbgpkg)) -- -Zxz", "$(call dh_all,$(dbgpkg)) -- -Zzstd"),
    ("debian/rules.d/3-binary-indep.mk",         # docs for the unversioned bpftool package only
     "  ifeq ($(do_tools_bpftool),true)\n\tdh_prep -p$(bpftoolpkg)",
     "  ifneq ($(filter $(bpftool_pkg_name),$(packages_enabled)),)\n\tdh_prep -p$(bpftoolpkg)"),
]


_HWE_TOOLS = "do_tools_noble_hwe"


def enable_tools(tree: Path) -> int:
    """Apply _RULE_EDITS. Fails loudly if Ubuntu's rules changed shape, so a
    resync of the Ubuntu pin cannot silently lose perf or bpftool."""
    texts: dict[str, str] = {}
    # Ubuntu's 6.17 packaging can already put perf and bpftool in the versioned
    # tools directory (do_tools_noble_hwe, made for the noble hwe kernel). render()
    # sets that switch; the edits that move the two by hand are for packaging
    # that does not have it.
    has_switch = _HWE_TOOLS in (tree / "debian" / "rules").read_text(encoding="utf-8")
    done = 0
    for rel, old, new in _RULE_EDITS:
        s = texts.setdefault(rel, (tree / rel).read_text(encoding="utf-8"))
        if has_switch and ("perfpkgdir" in old or "bpftoolpkgdir" in old):
            continue
        done += 1
        if old not in s:
            raise SystemExit(f"kpm: {rel} no longer contains {old.strip()[:60]!r}; "
                             "update _RULE_EDITS in tools/kpm/kpm/packaging.py")
        texts[rel] = s.replace(old, new, 1)
    for rel, s in texts.items():
        with open(tree / rel, "w", encoding="utf-8", newline="\n") as f:
            f.write(s)
    return done


def merge_modprobe(master: dict[str, str], vendors: dict[str, dict[str, str]],
                   drop: list[str] | None = None) -> dict[str, str]:
    """Ubuntu master's modprobe.d files plus every line a vendor adds to them,
    minus lines local.toml says to drop."""
    out = dict(master)
    dropped = set(drop or [])
    for vendor, files in vendors.items():
        for name, text in files.items():
            have = set(out.get(name, "").splitlines())
            extra = [l for l in text.splitlines() if l.strip() and l not in have and l not in dropped]
            if extra and all(l.lstrip().startswith("#") for l in extra):
                extra = []                      # only the comments of a dropped rule were left
            if extra:
                out[name] = (out.get(name, "").rstrip("\n") + f"\n\n# --- from {vendor} ---\n"
                             + "\n".join(extra) + "\n").lstrip("\n")
    return out


def vendor_modprobe(cfg, git) -> dict[str, dict[str, str]]:
    """modprobe.d files of every pinned vendor source, read from the unified repo."""
    from . import core
    out = {}
    pins = core.pins(cfg)
    names = cfg.config_sources()
    if cfg.kind == "stable":                  # the root (ubuntu) is the master, read from the tree
        names = [n for n in names if cfg.sources[n].parent]
    for name in names:
        s, pin = cfg.sources[name], pins.get(name)
        if not pin:
            continue
        d = f"{s.packaging_dir}/modprobe.d"
        names = git.run("ls-tree", "--name-only", f"{pin['sha']}", f"{d}/", check=False).split()
        out[name] = {n.rsplit("/", 1)[1]: git.run("show", f"{pin['sha']}:{n}") for n in names}
    return out


def _set(mk: str, key: str, value: str) -> str:
    line = f"{key}\t= {value}"
    rx = re.compile(rf"^{re.escape(key)}\s*[?:]?=.*$", re.M)
    return rx.sub(line, mk) if rx.search(mk) else mk.rstrip("\n") + f"\n{line}\n"


def overlay(git, sha: str, tree: Path) -> None:
    """Replace debian/ and debian.master/ in the tree with Ubuntu's, taken from the
    pinned Ubuntu tag. Packaging is never part of the patch series."""
    import subprocess
    for d in ("debian", "debian.master"):
        shutil.rmtree(tree / d, ignore_errors=True)
    ar = subprocess.Popen(["git", "-C", str(git.repo), "archive", sha, "debian", "debian.master"],
                          env=git.env, stdout=subprocess.PIPE)
    rc = subprocess.run(["tar", "-x"], cwd=tree, stdin=ar.stdout).returncode
    ar.stdout.close()
    if ar.wait() or rc:
        raise SystemExit(f"kpm: could not extract packaging from {sha}")


_HOOK_TEST = re.compile(r"\[ -d (/etc/kernel/(\w+)\.d) \] \|\| \[ -d /usr/share/kernel/\2\.d \]")
_HOOK_ARGS = re.compile(r"(/etc/kernel/(\w+)\.d) /usr/share/kernel/\2\.d")


def single_hook_dir(tree: Path) -> int:
    """Make the package scripts call run-parts with one directory.

    The 7.3 packaging runs the hooks of /etc/kernel/<event>.d and
    /usr/share/kernel/<event>.d in one run-parts call. run-parts accepts two
    directories only from debianutils 5.20; on noble (5.17) the call fails and
    the kernel packages do not install. A series with the old run-parts keeps
    its hooks in /etc/kernel only, so only that directory is used there.
    Returns the number of scripts changed."""
    changed = 0
    for f in sorted((tree / "debian" / "templates").glob("*.in")):
        text = f.read_text(encoding="utf-8")
        new = _HOOK_ARGS.sub(r"\1", _HOOK_TEST.sub(r"[ -d \1 ]", text))
        if new != text:
            with open(f, "w", encoding="utf-8", newline="\n") as out:
                out.write(new)
            changed += 1
    return changed


def render(mono: Path, tree: Path, series: str, base_tag: str, union: Path | None = None,
           modprobe: dict[str, dict[str, str]] | None = None, trust_certs: list[Path] | None = None) -> dict:
    ident = load_identity(mono)
    v, arches = ident["vendor"], ident["arches"]
    if series not in ident["series"]:
        raise SystemExit(f"kpm: series '{series}' is not in identity.toml [series]")
    src, dst = tree / "debian.master", tree / f"debian.{v}"
    if not src.is_dir():
        raise SystemExit(f"kpm: {src} not found; the tree needs Ubuntu's packaging applied")
    if dst.exists():
        shutil.rmtree(dst)
    for d in ("config", "control.d", "rules.d", "etc"):
        (dst / d).mkdir(parents=True)

    def put(rel: str, text: str):
        with open(dst / rel, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    fl = flavours(ident)
    ver = version(ident, tree, base_tag, series)

    put("changelog",
        f"linux-{v} ({ver}) {series}; urgency=medium\n\n"
        f"  * Base: {'Ubuntu kernel' if base_tag.startswith('Ubuntu-') else 'upstream stable'} {base_tag}\n\n"
        f" -- {ident['maintainer']}  {formatdate(localtime=False)}\n")

    names = [f"{a}-{f}" for f, archs in fl.items() for a in archs]
    deps = {f"{a}-{f}": f"{a}-generic" + ("-64k" if f.endswith("-64k") else "")
            for f, archs in fl.items() for a in archs}
    from .kconfig import annotation_lines, apply_local
    local_path = union.parent / "local.toml" if union else None
    local = tomllib.loads(local_path.read_text(encoding="utf-8")) if local_path and local_path.exists() else {}
    merged = apply_local(json.loads(union.read_text()) if union and union.exists() else {},
                         local.get("config", {}))
    overrides = "\n" + annotation_lines(merged, v, arches) if merged else ""
    put("config/annotations",
        "# Menu: HEADER\n# FORMAT: 4\n"
        f"# ARCH: {' '.join(arches)}\n"
        f"# FLAVOUR: {' '.join(sorted(names))}\n"
        f"# FLAVOUR_DEP: {deps!r}\n\n"
        'include "../../debian.master/config/annotations"\n' + overrides)

    for stub in ("flavour-control.stub", "flavour-signed-control.stub"):
        put(f"control.d/{stub}", filter_arches((src / "control.d" / stub).read_text(encoding="utf-8"), arches))
    for f, archs in fl.items():
        boot = " | ".join(b for a, b in (("amd64", "grub-efi-amd64 [amd64] | grub-pc [amd64]"),
                                        ("arm64", "grub-efi-arm64 [arm64]")) if a in archs)
        put(f"control.d/vars.{f}",
            f'arch="{" ".join(archs)}"\nsupported="{ident["supported"]}"\n'
            f'target="{ident["target"]}"\nbootloader="{boot}"\nprovides="kvm-api-4"\n')

    control = filter_arches((src / "control.stub.in").read_text(encoding="utf-8"), arches)
    if "arm64" in arches and "libopencsd-dev" not in control:
        control = control.replace(" libelf-dev <!stage1>,\n",
                                  " libelf-dev <!stage1>,\n libopencsd-dev [arm64] <!stage1>,\n", 1)
    # LLVM. Ubuntu's packaging names the LLVM release of the series it was made
    # for (clang-22, libllvm22). An older series has an older LLVM, set as
    # [packaging.<series>] llvm in identity.toml. The rules read the version for
    # llvm-config from an llvm-<N>-dev entry, which Ubuntu's list no longer
    # has, so perf was built with no LLVM support: add the entry.
    named = re.search(r"\bclang-(\d+)", control)
    llvm = str(ident.get("packaging", {}).get(series, {}).get("llvm") or (named.group(1) if named else ""))
    if llvm:
        if named and named.group(1) != llvm:
            control = re.sub(rf"\b(clang|llvm)-{named.group(1)}\b", rf"\1-{llvm}", control)
            control = control.replace(f"libllvm{named.group(1)}", f"libllvm{llvm}")
        if f"llvm-{llvm}-dev" not in control:
            control = control.replace(" libelf-dev <!stage1>,\n",
                                      f" libelf-dev <!stage1>,\n llvm-{llvm}-dev <!stage1>,\n", 1)
    put("control.stub.in", control)

    for a in arches:
        mk = (src / "rules.d" / f"{a}.mk").read_text(encoding="utf-8")
        mk = _set(mk, "flavours", " ".join(f for f, archs in fl.items() if a in archs))
        mk = _set(mk, "do_zfs", "false")
        for tool in ("perf", "bpftool", "hyperv"):     # one tools set for every cloud
            mk = _set(mk, f"do_tools_{tool}", "true")
        if _HWE_TOOLS in (tree / "debian" / "rules").read_text(encoding="utf-8"):
            mk = _set(mk, _HWE_TOOLS, "true")          # perf and bpftool in the versioned tools directory
        for key, value in ident.get("rules", {}).get(series, {}).items():
            mk = _set(mk, key, str(value))
        put(f"rules.d/{a}.mk", mk)

    put("etc/update.conf", "DEBIAN_MASTER=debian.master\n")
    if (src / "dkms-versions").exists():
        # Older packaging (6.17) builds DKMS modules from the Ubuntu archive into
        # the kernel packages: zfs, the Intel camera drivers, a Wi-Fi backport.
        # We build none of them; our out-of-tree modules have their own
        # packages (modules/matrix.tsv). The rules read the file, so it exists
        # and is empty.
        put("dkms-versions", "")
    put("variants", "--\n")
    master_mp = {p.name: p.read_text(encoding="utf-8") for p in sorted((src / "modprobe.d").glob("*"))} \
        if (src / "modprobe.d").is_dir() else {}
    (dst / "modprobe.d").mkdir()
    for name, text in merge_modprobe(master_mp, modprobe or {}, local.get("modprobe", {}).get("drop")).items():
        put(f"modprobe.d/{name}", text)
    rule_edits = enable_tools(tree)
    quirks = ident.get("packaging", {}).get(series, {})
    unknown = set(quirks) - {"single_hook_dir", "llvm"}
    if unknown:
        raise SystemExit(f"kpm: identity.toml [packaging.{series}] has unknown keys: {', '.join(sorted(unknown))}")
    if quirks.get("single_hook_dir") and not single_hook_dir(tree):
        # Nothing was changed. That is right for older packaging (6.17), whose
        # scripts know /etc/kernel only. It is wrong if the scripts do use
        # /usr/share/kernel and the call no longer has the form we look for.
        templates = "".join(f.read_text(encoding="utf-8") for f in sorted((tree / "debian" / "templates").glob("*.in")))
        if "/usr/share/kernel/" in templates:
            raise SystemExit("kpm: single_hook_dir is set but no package script calls run-parts with two "
                             "directories; Ubuntu's templates changed, check debian/templates")
    # Certificates the kernel trusts for module signatures, besides Canonical's.
    # debian/rules builds the kernel's trusted keyring from debian/certs/*.pem
    # and $(DEBIAN)/certs/*.pem, so a file here is enough. Modules signed with
    # the matching key then load when signatures are enforced.
    trusted = []
    for i, cert in enumerate(trust_certs or [], 1):
        text = Path(cert).read_text(encoding="ascii")
        if text.count("-----BEGIN CERTIFICATE-----") != 1 or "PRIVATE KEY" in text:
            raise SystemExit(f"kpm: {cert} must be one PEM certificate, and no key")
        (dst / "certs").mkdir(exist_ok=True)
        put(f"certs/{v}-extra-{i}-all.pem", text if text.endswith("\n") else text + "\n")
        trusted.append(str(cert))
    for extra in ("reconstruct", "tracking-bug"):
        p = src / extra
        if p.is_dir():
            shutil.copytree(p, dst / extra)
        elif p.exists():
            shutil.copy(p, dst / extra)

    with open(tree / "debian" / "debian.env", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"DEBIAN=debian.{v}\n")

    return {"dir": str(dst), "source_package": f"linux-{v}", "version": ver, "series": series,
            "arches": arches, "flavours": fl, "rule_edits": rule_edits, "trusted_certs": trusted,
            "modprobe_from": sorted(n for n, f in (modprobe or {}).items() if f)}
