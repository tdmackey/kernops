#!/usr/bin/env python3
"""Generate the gb200 kernel-fork status dashboard (single static HTML page).

Reads:
  - kernel/upstream-base.txt + kernel/patches/gb200/<base>/  (what we carry)
  - the local Ubuntu kernel clones                           (lag/drop checks)
  - modules/matrix.tsv + local build artifacts               (module health)
  - tools/treadmill/detect.py                                (archive status)
  - OSV API (api.osv.dev), cached on disk                    (open CVEs)

Stdlib only. Network use is best-effort: results are cached in
tools/dashboard/cache/ and the page renders fine offline (--offline skips
the network entirely; --refresh ignores the cache).

Usage: generate.py [--offline | --refresh] [--deep-git-checks] [-o OUTPUT.html]
"""
import argparse
import datetime
import email.utils
import functools
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
CACHE_MAX_AGE_S = 24 * 3600
OSV_QUERY_URL = "https://api.osv.dev/v1/query"
MONO = os.path.dirname(os.path.dirname(HERE))
RISK_PATH_PREFIXES = (
    "arch/arm64/", "block/", "debian", "drivers/iommu/", "drivers/firmware/",
    "fs/", "include/linux/", "kernel/", "mm/", "security/",
)


def run_git(repo, *args):
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                         text=True, timeout=60)
    return out.stdout.strip() if out.returncode == 0 else ""


def git_result(repo, *args, input_text=None, env=None, timeout=60):
    cmd = ["git", "-C", repo, *args]
    try:
        return subprocess.run(cmd, input=input_text, capture_output=True,
                              text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


# ---------------------------------------------------------------- versions
def tag_to_version(tag):
    """Ubuntu-hwe-7.0-7.0.0-26.26_24.04.1 -> 7.0.0-26.26~24.04.1"""
    m = re.search(r"((?:\d+:)?\d+\.\d+\.\d+-\d+\.[\d.]+(?:_[\d.]+)?)$", tag)
    return m.group(1).replace("_", "~") if m else tag


def _split_deb_version(version):
    if ":" in version:
        epoch_s, rest = version.split(":", 1)
        epoch = int(epoch_s or 0)
    else:
        epoch, rest = 0, version
    if "-" in rest:
        upstream, debian = rest.rsplit("-", 1)
    else:
        upstream, debian = rest, ""
    return epoch, upstream, debian


def _order_char(ch):
    if ch == "~":
        return -1
    if ch == "":
        return 0
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _verrevcmp(left, right):
    li = ri = 0
    llen, rlen = len(left), len(right)
    while li < llen or ri < rlen:
        while (li < llen and not left[li].isdigit()) or \
              (ri < rlen and not right[ri].isdigit()):
            lc = left[li] if li < llen else ""
            rc = right[ri] if ri < rlen else ""
            lo, ro = _order_char(lc), _order_char(rc)
            if lo != ro:
                return -1 if lo < ro else 1
            if lc:
                li += 1
            if rc:
                ri += 1

        while li < llen and left[li] == "0":
            li += 1
        while ri < rlen and right[ri] == "0":
            ri += 1

        lstart, rstart = li, ri
        while li < llen and left[li].isdigit():
            li += 1
        while ri < rlen and right[ri].isdigit():
            ri += 1

        llen_digits, rlen_digits = li - lstart, ri - rstart
        if llen_digits != rlen_digits:
            return -1 if llen_digits < rlen_digits else 1
        if left[lstart:li] != right[rstart:ri]:
            return -1 if left[lstart:li] < right[rstart:ri] else 1
    return 0


def debian_compare(left, right):
    le, lu, ld = _split_deb_version(left)
    re, ru, rd = _split_deb_version(right)
    if le != re:
        return -1 if le < re else 1
    upstream = _verrevcmp(lu, ru)
    if upstream:
        return upstream
    return _verrevcmp(ld, rd)


DEBIAN_VERSION_KEY = functools.cmp_to_key(debian_compare)


# ------------------------------------------------------------------ pins
def read_pins(repo_root):
    pins = {}
    path = os.path.join(repo_root, "kernel", "upstream-base.txt")
    if not os.path.exists(path):
        return pins
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            pins[parts[0]] = parts[1]
    return pins


# ---------------------------------------------------------------- patches
PATCH_TRAILERS = re.compile(
    r"\((cherry picked|backported) from commit ([0-9a-f]{12,40})", re.I)


def read_patches(repo_root, base):
    pdir = os.path.join(repo_root, "kernel", "patches", "gb200", base)
    patches = []
    if not os.path.isdir(pdir):
        return patches
    for fname in sorted(os.listdir(pdir)):
        if not fname.endswith(".patch"):
            continue
        text = open(os.path.join(pdir, fname), errors="replace").read()
        subject = ""
        m = re.search(r"^Subject: (?:\[[^\]]*\] ?)?(.+(?:\n .+)*)", text, re.M)
        if m:
            subject = re.sub(r"\s+", " ", m.group(1)).strip()
        author = ""
        m = re.search(r"^From: (.+)", text, re.M)
        if m:
            author = m.group(1).strip()
        date = ""
        age_days = None
        m = re.search(r"^Date: (.+)", text, re.M)
        if m:
            date = m.group(1).strip()
            try:
                dt = email.utils.parsedate_to_datetime(date)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                age_days = (datetime.datetime.now(datetime.timezone.utc) - dt).days
            except Exception:
                age_days = None
        buglink = ""
        m = re.search(r"^BugLink: (\S+)", text, re.M)
        if m:
            buglink = m.group(1)
        kind, upstream = "SAUCE", ""
        m = PATCH_TRAILERS.search(text)
        if m:
            kind, upstream = m.group(1).lower(), m.group(2)
        elif subject.startswith("UBUNTU: [Packaging]"):
            kind = "packaging"
        files = re.findall(r"^diff --git a/(\S+)", text, re.M)
        subsystems = sorted({subsystem_for_path(f) for f in files})
        risk = any(f.startswith(RISK_PATH_PREFIXES) for f in files)
        patches.append(dict(file=fname, subject=subject, author=author,
                            date=date, age_days=age_days,
                            kind=kind, upstream=upstream, buglink=buglink,
                            files=files, subsystems=subsystems, risk=risk))
    return patches


def subsystem_for_path(path):
    parts = path.split("/")
    if not parts:
        return path
    if parts[0] == "drivers" and len(parts) > 1:
        return "/".join(parts[:2])
    if parts[0] == "debian" or parts[0].startswith("debian."):
        return "packaging"
    return parts[0]


# ------------------------------------------------------------------- git
def base_git_state(base_cfg, pin_tag):
    clone = base_cfg["clone"]
    state = dict(clone=clone, latest_tag="?", behind=None, pin_exists=False)
    if not os.path.isdir(clone):
        return state
    tags = run_git(clone, "tag", "-l", base_cfg["tag_glob"],
                   "--sort=-creatordate").splitlines()
    if tags:
        state["latest_tag"] = tags[0]
        if pin_tag in tags:
            state["pin_exists"] = True
            state["behind"] = tags.index(pin_tag)
    return state


def git_ref_exists(repo, ref):
    return bool(repo and os.path.isdir(repo) and
                git_result(repo, "rev-parse", "-q", "--verify",
                           f"{ref}^{{commit}}").returncode == 0)


def commit_subject(repo, sha):
    return run_git(repo, "show", "-s", "--format=%s", sha)


def commit_author(repo, sha):
    return run_git(repo, "show", "-s", "--format=%an <%ae>", sha)


def commit_files(repo, sha):
    out = run_git(repo, "show", "--format=", "--name-only", sha)
    return [line for line in out.splitlines() if line]


def reverse_applies(repo, sha, ref):
    tmpidx = tempfile.NamedTemporaryFile(delete=False)
    tmpidx.close()
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = tmpidx.name
    try:
        if git_result(repo, "read-tree", ref, env=env).returncode != 0:
            return False
        patch = git_result(repo, "show", sha).stdout
        if not patch:
            return False
        return git_result(repo, "apply", "--cached", "--check", "--reverse",
                          "-", input_text=patch, env=env).returncode == 0
    finally:
        try:
            os.unlink(tmpidx.name)
        except OSError:
            pass


def range_subject_hits(repo, rev_range):
    if not rev_range:
        return {}
    res = git_result(repo, "log", "--format=%s%x09%h", rev_range, timeout=15)
    hits = {}
    for line in res.stdout.splitlines():
        if "\t" not in line:
            continue
        subject, short = line.rsplit("\t", 1)
        hits.setdefault(subject, short)
    return hits


def commit_presence(repo, sha, ref, subject_hits=None, check_ancestor=False,
                    check_content=False):
    if not sha:
        return dict(status="none", label="no upstream ref", detail="")
    subject = commit_subject(repo, sha)
    if not subject:
        return dict(status="invalid", label="invalid upstream sha", detail=sha[:12])
    if check_ancestor and git_result(repo, "merge-base", "--is-ancestor", sha, ref,
                                     timeout=10).returncode == 0:
        return dict(status="present", label="ancestor", detail=sha[:12])

    if subject and subject_hits is not None:
        hit = subject_hits.get(subject, "")
        if hit:
            return dict(status="present", label=f"subject@{hit}", detail=subject)

    if check_content and reverse_applies(repo, sha, ref):
        return dict(status="present", label="content", detail="reverse-applies")
    if check_ancestor or subject_hits is not None or check_content:
        return dict(status="missing", label="missing", detail="")
    return dict(status="unknown", label="not checked", detail="")


def analyze_patch_stack(repo, pin, latest, patches, deep_git=False):
    if not repo or not os.path.isdir(repo):
        for p in patches:
            p["pin_presence"] = dict(status="unknown", label="clone missing", detail="")
            p["latest_presence"] = dict(status="unknown", label="clone missing", detail="")
            p["warnings"] = []
            if p["kind"] not in ("packaging",) and not p.get("upstream"):
                p["warnings"].append("missing upstream trailer")
            p["drop"] = "manual" if not p.get("upstream") else "not checked"
        return

    carried = {p["upstream"] for p in patches if p.get("upstream")}
    latest_range = f"{pin}..{latest}" if git_ref_exists(repo, pin) and git_ref_exists(repo, latest) else None
    latest_subject_hits = range_subject_hits(repo, latest_range)
    for p in patches:
        upstream = p.get("upstream", "")
        p["pin_presence"] = commit_presence(repo, upstream, pin,
                                            check_ancestor=deep_git,
                                            check_content=deep_git)
        p["latest_presence"] = commit_presence(repo, upstream, latest,
                                               subject_hits=latest_subject_hits,
                                               check_ancestor=deep_git,
                                               check_content=deep_git)
        p["warnings"] = []
        if p["kind"] not in ("packaging",) and not upstream:
            p["warnings"].append("missing upstream trailer")
        if upstream and p["pin_presence"]["status"] == "invalid":
            p["warnings"].append("invalid upstream sha")
        if upstream and p["pin_presence"]["status"] != "invalid":
            usubj = commit_subject(repo, upstream)
            uauthor = commit_author(repo, upstream)
            if usubj and p.get("subject") and usubj != p["subject"]:
                p["warnings"].append("subject differs from upstream")
            if uauthor and p.get("author") and uauthor != p["author"]:
                p["warnings"].append("author differs from upstream")
            if os.environ.get("GB200_DASHBOARD_DEEP_FIXES") == "1":
                fixes = missing_fix_commits(repo, upstream, carried)
                if fixes:
                    p["warnings"].append("missing upstream Fixes: " +
                                         ", ".join(f["short"] for f in fixes[:3]))
        if p["pin_presence"]["status"] == "present":
            p["drop"] = "drop now"
        elif p["latest_presence"]["status"] == "present":
            p["drop"] = "drop on rebase"
        elif upstream:
            p["drop"] = "still carried"
        else:
            p["drop"] = "manual"


def missing_fix_commits(repo, sha, carried_upstreams):
    if not git_ref_exists(repo, "upstream/master"):
        return []
    res = git_result(repo, "log", "--max-count=20", "--format=%H%x09%h %s",
                     f"--grep=Fixes: {sha[:8]}", f"{sha}..upstream/master",
                     timeout=8)
    fixes = []
    for line in res.stdout.splitlines():
        if not line:
            continue
        full, summary = line.split("\t", 1)
        if full not in carried_upstreams:
            fixes.append({"sha": full, "short": summary.split(" ", 1)[0],
                          "summary": summary})
    return fixes


# ------------------------------------------------------------------- osv
def cache_path(eco, pkg):
    return os.path.join(CACHE_DIR, f"osv-{eco.replace(':', '_')}-{pkg}.json")


def fetch_osv(eco, pkg, mode):
    """Return (vulns, source) where source is 'cache'/'network'/'none'."""
    cpath = cache_path(eco, pkg)
    cached = None
    if os.path.exists(cpath):
        cached = json.load(open(cpath))
        age = datetime.datetime.now().timestamp() - os.path.getmtime(cpath)
        if mode == "offline" or (mode == "auto" and age < CACHE_MAX_AGE_S):
            return cached.get("vulns", []), "cache"
    if mode == "offline":
        return [], "none"
    vulns, token = [], None
    try:
        while True:
            q = {"package": {"name": pkg, "ecosystem": eco}}
            if token:
                q["page_token"] = token
            req = urllib.request.Request(
                OSV_QUERY_URL, data=json.dumps(q).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.load(r)
            vulns += data.get("vulns", [])
            token = data.get("next_page_token")
            if not token:
                break
        os.makedirs(CACHE_DIR, exist_ok=True)
        json.dump({"vulns": vulns}, open(cpath, "w"))
        return vulns, "network"
    except Exception as e:
        print(f"  osv fetch failed for {pkg} ({e}); using cache if any",
              file=sys.stderr)
        if cached:
            return cached.get("vulns", []), "cache"
        return [], "none"


def analyze_cves(vulns, pin_version):
    """CVEs fixed in versions NEWER than our pin (we lack the fix) or unfixed."""
    missing, unfixed = [], []
    for v in vulns:
        fixed_versions = []
        for aff in v.get("affected", []):
            for rng in aff.get("ranges", []):
                for ev in rng.get("events", []):
                    if "fixed" in ev:
                        fixed_versions.append(ev["fixed"])
        ids = [v.get("id", "?")] + v.get("aliases", [])
        cve = next((i for i in ids if i.startswith("CVE-")), ids[0])
        entry = dict(
            id=v.get("id", "?"), cve=cve,
            summary=(v.get("summary") or v.get("details", ""))[:160],
            fixed=max(fixed_versions, key=DEBIAN_VERSION_KEY) if fixed_versions else None,
            modified=v.get("modified", "")[:10],
        )
        if not fixed_versions:
            unfixed.append(entry)
        elif debian_compare(entry["fixed"], pin_version) > 0:
            missing.append(entry)
    missing.sort(key=lambda e: DEBIAN_VERSION_KEY(e["fixed"]), reverse=True)
    unfixed.sort(key=lambda e: e["modified"], reverse=True)
    return missing, unfixed


# --------------------------------------------------------------- treadmill
def treadmill_rows(repo_root, mode):
    if mode == "offline":
        return {}, "offline"
    detector = os.path.join(repo_root, "tools", "treadmill", "detect.py")
    if not os.path.exists(detector):
        return {}, "missing detector"
    res = subprocess.run([sys.executable, detector, "--json"],
                         capture_output=True, text=True, timeout=120)
    if res.stdout:
        try:
            return {row["base"]: row for row in json.loads(res.stdout)}, (
                "ok" if res.returncode in (0, 10) else f"error exit {res.returncode}")
        except json.JSONDecodeError:
            pass
    return {}, f"failed: {res.stderr.strip() or res.returncode}"


# --------------------------------------------------------------- modules
def read_module_matrix(repo_root):
    matrix = {}
    path = os.path.join(repo_root, "modules", "matrix.tsv")
    if not os.path.exists(path):
        return matrix
    for line in open(path):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 5:
            base, arch, name, version, source = parts[:5]
        elif len(parts) == 4:
            base, arch, name, version, source = parts[0], "arm64", parts[1], parts[2], parts[3]
        else:
            continue
        matrix.setdefault(base, {}).setdefault(arch, []).append(
            dict(name=name, version=version, source=source, arch=arch))
    return matrix


# ------------------------------------------------------------- artifacts
def walk_limited(root, max_depth):
    if not os.path.isdir(root):
        return
    root = os.path.abspath(root)
    for cur, dirs, files in os.walk(root):
        rel = os.path.relpath(cur, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        if depth >= max_depth:
            dirs[:] = []
        yield cur, files


def artifact_roots(dirs):
    seen = set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        roots = [d]
        out = os.path.join(d, "out")
        if os.path.isdir(out):
            roots.append(out)
        for root in roots:
            root = os.path.abspath(root)
            if root not in seen:
                seen.add(root)
                yield root


def list_artifacts(dirs):
    arts = []
    seen = set()
    for root in artifact_roots(dirs):
        max_depth = 5 if os.path.basename(root) == "out" else 1
        for cur, files in walk_limited(root, max_depth):
            for f in files:
                if not f.endswith(".deb"):
                    continue
                p = os.path.join(cur, f)
                if p in seen:
                    continue
                seen.add(p)
                st = os.stat(p)
                arts.append(dict(name=f, path=p, dir=cur, size=st.st_size,
                                 mtime=st.st_mtime))
    arts.sort(key=lambda a: a["mtime"], reverse=True)
    return arts[:100]


def list_provenance(dirs):
    rows = []
    seen = set()
    for root in artifact_roots(dirs):
        max_depth = 6 if os.path.basename(root) == "out" else 3
        for cur, files in walk_limited(root, max_depth):
            if "gb200-provenance.json" not in files:
                continue
            path = os.path.join(cur, "gb200-provenance.json")
            if path in seen:
                continue
            seen.add(path)
            try:
                data = json.load(open(path))
            except Exception:
                continue
            st = os.stat(path)
            data["_path"] = path
            data["_mtime"] = st.st_mtime
            rows.append(data)
    rows.sort(key=lambda a: a["_mtime"], reverse=True)
    return rows


def parse_kernel_deb(name, kind):
    m = re.match(rf"linux-{kind}-([^_]+)_([^_]+)_([^_]+)\.deb$", name)
    if not m:
        return None
    return dict(kver=m.group(1), version=m.group(2), arch=m.group(3))


def artifact_for_base_arch(artifact, base, arch):
    parts = os.path.normpath(artifact["path"]).split(os.sep)
    if "out" not in parts:
        return False
    after_out = parts[parts.index("out") + 1:]
    if base not in after_out:
        return False
    base_idx = after_out.index(base)
    if len(after_out) > base_idx + 1 and after_out[base_idx + 1] in ("arm64", "amd64"):
        return after_out[base_idx + 1] == arch
    return artifact.get("arch") == arch


def artifact_arch(artifact):
    kern = parse_kernel_deb(artifact["name"], "image") or parse_kernel_deb(artifact["name"], "headers")
    if kern:
        return kern["arch"]
    m = re.match(r"gb200-modules-.+_([^_]+)\.deb$", artifact["name"])
    return m.group(1) if m else ""


def analyze_artifacts(base, arch, artifacts, module_rows, provenance_records=None):
    provenance_records = provenance_records or []
    for a in artifacts:
        a.setdefault("arch", artifact_arch(a))
    base_arts = [a for a in artifacts if artifact_for_base_arch(a, base, arch)]
    images = []
    headers = []
    for a in base_arts:
        img = parse_kernel_deb(a["name"], "image")
        hdr = parse_kernel_deb(a["name"], "headers")
        if img and img["arch"] == arch:
            images.append({**a, **img})
        if hdr and hdr["arch"] == arch:
            headers.append({**a, **hdr})
    images.sort(key=lambda a: a["mtime"], reverse=True)
    headers.sort(key=lambda a: a["mtime"], reverse=True)
    latest = images[0] if images else None
    header_ok = bool(latest and any(h["kver"] == latest["kver"] and
                                    h["version"] == latest["version"]
                                    for h in headers))
    provenance = next((p for p in provenance_records
                       if p.get("base") == base and p.get("arch") == arch), None)
    abi_by_name = {}
    if provenance:
        for abi in provenance.get("module_abi", []):
            if abi.get("name"):
                abi_by_name[abi["name"]] = abi
    modules = []
    for row in module_rows:
        prefix = f"gb200-modules-{row['name']}-"
        matches = [a for a in base_arts if a["name"].startswith(prefix)]
        matches.sort(key=lambda a: a["mtime"], reverse=True)
        current = None
        if latest:
            kernel_suffix = f"+kernel.{latest['version']}_{arch}.deb"
            for a in matches:
                if a["name"].endswith(kernel_suffix):
                    current = a
                    break
        modules.append({**row, "latest": matches[0] if matches else None,
                        "current": current, "abi": abi_by_name.get(row["name"])})
    return dict(kernel=latest, header_ok=header_ok, modules=modules,
                artifacts=base_arts, provenance=provenance)


def range_diff_info(repo_root, base):
    path = os.path.join(repo_root, "dashboard", f"range-diff-{base}.txt")
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    return dict(path=path, name=os.path.basename(path), size=st.st_size,
                mtime=st.st_mtime)


# ------------------------------------------------------------------ html
CSS = """
body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2rem auto;
     max-width:1280px;padding:0 1rem;color:#1a1a2e;background:#fafafa}
h1{font-size:1.5rem} h2{font-size:1.15rem;margin-top:2rem;
   border-bottom:2px solid #e0e0e8;padding-bottom:.3rem}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{text-align:left;vertical-align:top;padding:.45rem .6rem;border-bottom:1px solid #eee}
th{background:#f0f0f5;font-weight:600}
code{background:#eef;padding:.1em .35em;border-radius:3px;font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.7rem;margin:1rem 0}
.card{background:#fff;border:1px solid #e6e6ef;border-radius:6px;padding:.75rem}
.card b{display:block;margin-bottom:.25rem}.actions{background:#fff;border:1px solid #e6e6ef;
      border-radius:6px;margin:.75rem 0;padding:.65rem .9rem}.actions li{margin:.25rem 0}
.chip{display:inline-block;padding:.05rem .5rem;border-radius:9px;
      font-size:11px;font-weight:600;color:#fff}
.chip.cherry{background:#2e7d32}.chip.backported{background:#558b2f}
.chip.SAUCE{background:#e65100}.chip.packaging{background:#1565c0}
.chip.current,.chip.ok{background:#2e7d32}.chip.stale,.chip.warn{background:#e65100}
.chip.crit,.chip.error{background:#c62828}.chip.info{background:#455a64}
.chip.muted{background:#888}.chip.drop{background:#6a1b9a}
.tiny{font-size:12px}.nowrap{white-space:nowrap}
.ok{color:#2e7d32;font-weight:600}.warn{color:#e65100;font-weight:600}
.crit{color:#c62828;font-weight:600}.muted{color:#888}
footer{margin-top:2.5rem;color:#888;font-size:12px}
"""


def esc(s):
    return html.escape(str(s or ""))


def chip(label, cls):
    return f"<span class='chip {esc(cls)}'>{esc(label)}</span>"


def fmt_time(ts):
    if not ts:
        return "?"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def status_class(status):
    status = (status or "").lower()
    if status in ("current", "ok", "present", "cache", "network"):
        return "ok"
    if status in ("stale", "ahead-of-archive", "missing", "offline"):
        return "warn"
    if "error" in status or "invalid" in status:
        return "crit"
    return "info"


def patch_summary(patches):
    if not patches:
        return dict(count=0, sauce=0, risk=0, oldest=None, subsystems=[])
    ages = [p["age_days"] for p in patches if p.get("age_days") is not None]
    subsystems = sorted({s for p in patches for s in p.get("subsystems", [])})
    return dict(count=len(patches),
                sauce=sum(1 for p in patches if p["kind"] == "SAUCE"),
                risk=sum(1 for p in patches if p.get("risk")),
                oldest=max(ages) if ages else None,
                subsystems=subsystems)


def build_actions(bases, detector_source):
    actions = []
    if detector_source not in ("ok", "offline"):
        actions.append(("crit", f"Publication detector: {detector_source}"))
    for b in bases:
        tstatus = b.get("treadmill", {}).get("status")
        if tstatus in ("STALE", "AHEAD-OF-ARCHIVE") or (tstatus and "api-error" in tstatus):
            target = b.get("treadmill", {}).get("expected_tag", "?")
            actions.append(("crit" if "api-error" in tstatus else "warn",
                            f"{b['name']}: archive status {tstatus}, target {target}"))
        drops = [p for p in b["patches"] if p.get("drop") in ("drop now", "drop on rebase")]
        if drops:
            actions.append(("warn", f"{b['name']}: {len(drops)} patch(es) look droppable"))
        patch_warnings = sum(len(p.get("warnings", [])) for p in b["patches"])
        if patch_warnings:
            actions.append(("crit", f"{b['name']}: {patch_warnings} patch provenance warning(s)"))
        for arch, art in b.get("artifacts_by_arch", {}).items():
            label = f"{b['name']}/{arch}"
            if not art.get("kernel"):
                actions.append(("warn", f"{label}: no current build-all kernel artifact found"))
            elif not art.get("matches_pin"):
                actions.append(("warn", f"{label}: newest kernel artifact {art['kernel']['version']} does not match pin {b['version']}"))
            missing_mods = [m for m in art.get("modules", []) if not m.get("current")]
            if missing_mods:
                names = ", ".join(m["name"] for m in missing_mods)
                actions.append(("warn", f"{label}: module artifact missing/stale for {names}"))
        if b.get("cve_source") in ("none", "offline"):
            actions.append(("warn", f"{b['name']}: CVE data unavailable ({b.get('cve_source')})"))
    return actions


def render(repo_root, bases, artifacts, detector_source):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"<!doctype html><meta charset=utf-8><title>gb200 kernel fork status"
           f"</title><style>{CSS}</style>",
           f"<h1>gb200 kernel fork - status</h1>"
           f"<p class=muted>generated {now} | repo {esc(repo_root)}</p>"]

    # --- action queue
    actions = build_actions(bases, detector_source)
    out.append("<h2>Action queue</h2>")
    if actions:
        out.append("<ul class=actions>")
        for cls, text in actions:
            out.append(f"<li>{chip(cls.upper(), cls)} {esc(text)}</li>")
        out.append("</ul>")
    else:
        out.append(f"<p>{chip('OK', 'ok')} No dashboard actions detected.</p>")

    totals = dict(
        bases=len(bases),
        targets=sum(len(b.get("artifacts_by_arch", {})) for b in bases),
        patches=sum(len(b["patches"]) for b in bases),
        drops=sum(1 for b in bases for p in b["patches"]
                  if p.get("drop") in ("drop now", "drop on rebase")),
        modules=sum(len(art.get("modules", [])) for b in bases
                    for art in b.get("artifacts_by_arch", {}).values()),
    )
    out.append("<div class=cards>")
    out.append(f"<div class=card><b>Bases</b>{totals['bases']} tracked, "
               f"{totals['targets']} arch targets</div>")
    out.append(f"<div class=card><b>Patch delta</b>{totals['patches']} carried, "
               f"{totals['drops']} droppable</div>")
    out.append(f"<div class=card><b>Module pins</b>{totals['modules']} matrix rows</div>")
    out.append(f"<div class=card><b>Detector</b>{chip(detector_source, status_class(detector_source))}</div>")
    out.append("</div>")

    # --- bases table
    out.append("<h2>Bases</h2><table><tr><th>base</th><th>pinned tag</th>"
               "<th>archive</th><th>latest local tag</th><th>lag</th>"
               "<th>patch risk</th><th>artifact</th><th>range-diff</th></tr>")
    for b in bases:
        lag = b["git"]["behind"]
        lag_html = ("<span class=muted>?</span>" if lag is None else
                    "<span class=ok>current</span>" if lag == 0 else
                    f"<span class=warn>{lag} tag(s) behind</span>")
        t = b.get("treadmill", {})
        tstatus = t.get("status", "offline")
        archive = chip(tstatus, status_class(tstatus))
        if t.get("expected_tag"):
            archive += f"<br><code>{esc(t['expected_tag'])}</code>"
        if t.get("published"):
            archive += f"<br><span class=muted>{esc(t['pocket'])} {esc(t['published'])}</span>"
        ps = patch_summary(b["patches"])
        risk = (f"{ps['count']} patches"
                f"<br>{ps['risk']} risky-path, {ps['sauce']} SAUCE"
                f"<br><span class=muted>{esc(', '.join(ps['subsystems'][:5]))}"
                f"{' ...' if len(ps['subsystems']) > 5 else ''}</span>")
        if ps["oldest"] is not None:
            risk += f"<br><span class=muted>oldest {ps['oldest']}d</span>"
        art_bits = []
        for arch, art in b.get("artifacts_by_arch", {}).items():
            kernel = art.get("kernel")
            if not kernel:
                art_bits.append(f"<b>{esc(arch)}</b>: {chip('missing', 'warn')}")
            else:
                art_bits.append(f"<b>{esc(arch)}</b>: "
                                f"{chip('pin match', 'ok') if art.get('matches_pin') else chip('pin mismatch', 'warn')} "
                                f"{chip('headers ok', 'ok') if art.get('header_ok') else chip('headers missing', 'warn')}"
                                f"<br><code>{esc(kernel['name'])}</code>")
        artifact = "<br>".join(art_bits) if art_bits else "<span class=muted>none</span>"
        rd = b.get("range_diff")
        rd_html = ("<span class=muted>none</span>" if not rd else
                   f"<a href='{esc(rd['name'])}'><code>{esc(rd['name'])}</code></a>"
                   f"<br><span class=muted>{fmt_time(rd['mtime'])}</span>")
        out.append(
            f"<tr><td><b>{esc(b['name'])}</b></td>"
            f"<td><code>{esc(b['pin'])}</code></td>"
            f"<td>{archive}</td>"
            f"<td><code>{esc(b['git']['latest_tag'])}</code></td>"
            f"<td>{lag_html}</td><td>{risk}</td><td>{artifact}</td><td>{rd_html}</td></tr>")
    out.append("</table>")

    # --- CI / build health
    out.append("<h2>Build health</h2><table><tr><th>target</th><th>pin</th>"
               "<th>archive detector</th><th>drop candidates</th>"
               "<th>kernel artifact</th><th>module artifacts</th><th>CVE data</th></tr>")
    for b in bases:
        for arch, art in b.get("artifacts_by_arch", {}).items():
            mods = art.get("modules", [])
            current_mods = sum(1 for m in mods if m.get("current"))
            drop_count = sum(1 for p in b["patches"]
                             if p.get("drop") in ("drop now", "drop on rebase"))
            out.append(
                f"<tr><td><b>{esc(b['name'])}/{esc(arch)}</b></td>"
                f"<td>{chip('exists', 'ok') if b['git']['pin_exists'] else chip('missing', 'crit')}</td>"
                f"<td>{chip(b.get('treadmill', {}).get('status', 'offline'), status_class(b.get('treadmill', {}).get('status', 'offline')))}</td>"
                f"<td>{chip(str(drop_count), 'warn' if drop_count else 'ok')}</td>"
                f"<td>{chip('pin match', 'ok') if art.get('matches_pin') else chip('mismatch' if art.get('kernel') else 'missing', 'warn')}</td>"
                f"<td>{chip(f'{current_mods}/{len(mods)}', 'ok' if current_mods == len(mods) else 'warn')}</td>"
                f"<td>{chip(b.get('cve_source', 'none'), status_class(b.get('cve_source', 'none')))}</td></tr>")
    out.append("</table>")

    # --- patch stacks
    for b in bases:
        out.append(f"<h2>Patch stack - {esc(b['name'])}</h2>")
        if not b["patches"]:
            out.append("<p class=muted>No patches carried - stock Ubuntu.</p>")
            continue
        out.append("<table><tr><th>#</th><th>type</th><th>subject</th>"
                   "<th>drop readiness</th><th>provenance</th>"
                   "<th>subsystems/files</th><th>warnings</th></tr>")
        for i, p in enumerate(b["patches"], 1):
            kind_cls = {"cherry picked": "cherry", "backported": "backported",
                        "packaging": "packaging"}.get(p["kind"], "SAUCE")
            drop_cls = {"drop now": "crit", "drop on rebase": "drop",
                        "still carried": "ok", "manual": "info"}.get(p.get("drop"), "info")
            prov = []
            if p["upstream"]:
                prov.append(f'<a href="https://git.kernel.org/pub/scm/linux/'
                            f'kernel/git/torvalds/linux.git/commit/?id='
                            f'{esc(p["upstream"])}"><code>'
                            f'{esc(p["upstream"][:12])}</code></a>')
            if p["buglink"]:
                lp = p["buglink"].rsplit("/", 1)[-1]
                prov.append(f'<a href="{esc(p["buglink"])}">LP#{esc(lp)}</a>')
            readiness = (f"{chip(p.get('drop', 'unknown'), drop_cls)}"
                         f"<br><span class=tiny>pin: {esc(p.get('pin_presence', {}).get('label', '?'))}</span>"
                         f"<br><span class=tiny>latest: {esc(p.get('latest_presence', {}).get('label', '?'))}</span>")
            files = (f"<span class=muted>{esc(', '.join(p.get('subsystems', [])[:4]))}</span>"
                     f"<br><code>{esc(', '.join(p['files'][:3]))}"
                     f"{' ...' if len(p['files']) > 3 else ''}</code>")
            warnings = ("<span class=muted>none</span>" if not p.get("warnings") else
                        "<br>".join(f"<span class=crit>{esc(w)}</span>" for w in p["warnings"]))
            out.append(
                f"<tr><td>{i}</td>"
                f"<td><span class='chip {kind_cls}'>{esc(p['kind'])}</span></td>"
                f"<td>{esc(p['subject'])}<br><span class=muted>"
                f"{esc(p['author'])}</span>"
                f"{'<br><span class=muted>age ' + str(p['age_days']) + 'd</span>' if p.get('age_days') is not None else ''}</td>"
                f"<td>{readiness}</td>"
                f"<td>{' | '.join(prov) or '<span class=muted>-</span>'}</td>"
                f"<td>{files}</td><td>{warnings}</td></tr>")
        out.append("</table>")

    # --- module matrix
    out.append("<h2>Module matrix</h2>")
    for b in bases:
        for arch, art in b.get("artifacts_by_arch", {}).items():
            mods = art.get("modules", [])
            out.append(f"<h3>{esc(b['name'])}/{esc(arch)}</h3>")
            if not mods:
                out.append("<p class=muted>No module rows for this target.</p>")
                continue
            out.append("<table><tr><th>module</th><th>pin</th><th>source</th>"
                       "<th>current package</th><th>ABI</th><th>latest package</th></tr>")
            for m in mods:
                current = m.get("current")
                latest = m.get("latest")
                abi = m.get("abi")
                current_html = (f"{chip('current', 'ok')}<br><code>{esc(current['name'])}</code>"
                                if current else f"{chip('missing/stale', 'warn')}")
                if abi:
                    abi_modules = abi.get("modules", [])
                    first = abi_modules[0].get("modinfo", {}) if abi_modules else {}
                    signer = first.get("signer") or (
                        "signed" if any(k.get("signature_appended") for k in abi_modules)
                        else "unsigned")
                    undef = sum(len(k.get("undefined_symbols", [])) for k in abi_modules)
                    abi_html = (f"{chip(str(len(abi_modules)) + ' .ko', 'ok')}"
                                f"<br><code>{esc(first.get('vermagic', '?'))}</code>"
                                f"<br><span class=tiny>signer: {esc(signer)}</span>"
                                f"<br><span class=tiny>undefined: {undef}</span>")
                else:
                    abi_html = "<span class=muted>unavailable</span>"
                latest_html = ("<span class=muted>none</span>" if not latest else
                               f"<code>{esc(latest['name'])}</code><br>"
                               f"<span class=muted>{fmt_time(latest['mtime'])}</span>")
                out.append(f"<tr><td><b>{esc(m['name'])}</b></td>"
                           f"<td><code>{esc(m['version'])}</code></td>"
                           f"<td class=tiny>{esc(m['source'])}</td>"
                           f"<td>{current_html}</td><td>{abi_html}</td>"
                           f"<td>{latest_html}</td></tr>")
            out.append("</table>")

    # --- CVEs
    for b in bases:
        missing, unfixed = b.get("cves") or ([], [])
        out.append(f"<h2>CVEs - {esc(b['name'])} "
                   f"(package <code>{esc(b['package'])}</code>)</h2>")
        source = b.get("cve_source", "none")
        out.append(f"<p><span class=crit>{len(missing)}</span> fixed upstream "
                   f"of our pin (missing the fix) | "
                   f"<span class=warn>{len(unfixed)}</span> with no fixed "
                   f"version yet {chip(source, status_class(source))}</p>")
        if missing:
            out.append("<table><tr><th>CVE</th><th>fixed in</th>"
                       "<th>summary</th></tr>")
            for e in missing[:30]:
                out.append(
                    f'<tr><td><a href="https://ubuntu.com/security/'
                    f'{esc(e["cve"])}">{esc(e["cve"])}</a></td>'
                    f"<td><code>{esc(e['fixed'])}</code></td>"
                    f"<td>{esc(e['summary'])}</td></tr>")
            if len(missing) > 30:
                out.append(f"<tr><td colspan=3 class=muted>... and "
                           f"{len(missing)-30} more</td></tr>")
            out.append("</table>")

    # --- artifacts
    out.append("<h2>Recent build artifacts</h2>")
    if artifacts:
        out.append("<table><tr><th>deb</th><th>path</th><th>size</th><th>built</th></tr>")
        for a in artifacts[:30]:
            when = fmt_time(a["mtime"])
            out.append(f"<tr><td><code>{esc(a['name'])}</code></td>"
                       f"<td class=tiny>{esc(a['dir'])}</td>"
                       f"<td>{a['size']//1024//1024} MB</td>"
                       f"<td>{when}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class=muted>none found</p>")

    out.append("<footer>gb200 kernel pipeline | tools/dashboard/generate.py"
               "</footer>")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--deep-git-checks", action="store_true",
                    help="also run reverse-apply content checks; slower on kernel trees")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()
    mode = "offline" if args.offline else "refresh" if args.refresh else "auto"

    cfg = json.load(open(os.path.join(HERE, "config.json")))
    repo_root = os.environ.get("GB200_REPO_ROOT", cfg["repo_root"])
    arches = cfg.get("arches", ["arm64"])
    pins = read_pins(repo_root)

    module_matrix = read_module_matrix(repo_root)
    artifacts = list_artifacts(cfg["artifact_dirs"])
    provenance_records = list_provenance(cfg["artifact_dirs"])
    detected, detector_source = treadmill_rows(repo_root, mode)

    bases = []
    for bc in cfg["bases"]:
        pin = pins.get(bc["name"], "?")
        git_state = base_git_state(bc, pin)
        patches = read_patches(repo_root, bc["name"])
        analyze_patch_stack(bc["clone"], pin, git_state["latest_tag"], patches,
                            deep_git=args.deep_git_checks)
        b = dict(name=bc["name"], pin=pin, version=tag_to_version(pin),
                 package=bc["package"],
                 git=git_state, patches=patches,
                 treadmill=detected.get(bc["name"], {"status": detector_source}),
                 range_diff=range_diff_info(repo_root, bc["name"]))
        b["artifacts_by_arch"] = {}
        for arch in arches:
            art = analyze_artifacts(bc["name"], arch, artifacts,
                                    module_matrix.get(bc["name"], {}).get(arch, []),
                                    provenance_records)
            kernel = art.get("kernel")
            art["matches_pin"] = bool(
                kernel and debian_compare(kernel["version"], b["version"]) == 0)
            b["artifacts_by_arch"][arch] = art
        vulns, source = fetch_osv(bc["osv_ecosystem"], bc["package"], mode)
        b["cve_source"] = source
        b["cves"] = analyze_cves(vulns, b["version"]) if vulns else ([], [])
        bases.append(b)

    page = render(repo_root, bases, artifacts, detector_source)
    out_path = args.output or os.path.join(repo_root, "dashboard",
                                           "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, "w").write(page)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
