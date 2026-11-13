"""fetch, harvest and resolve: turning source trees into ledger rows."""
import json
import re
import tempfile
import tomllib
from pathlib import Path

from . import fingerprint as fp
from . import ledger
from .config import Config, Source
from .git import Git, GitError, release_tags

# A commit touching only these paths is packaging or config, not a kernel patch.
_PACKAGING = re.compile(r"^(debian(\.[^/]+)?/|Ubuntu\.md$|Changes[^/]*\.md$)")
_CONFIG = re.compile(r"^debian(\.[^/]+)?/config/")
_GENERIC_NOISE = [r"^Linux \d+\.\d+"]


# ── fetch ────────────────────────────────────────────────────────────────────

def fetch_base_only(cfg: Config, git: Git) -> dict:
    """Fetch just the line's base tag, shallowly. Enough for `kpm verify` and
    `kpm apply`; not enough to harvest or rebase."""
    if not (cfg.repo / "HEAD").exists():
        cfg.repo.mkdir(parents=True, exist_ok=True)
        git.run("init", "--bare", "-q")
    loc = cfg.base_source.remote_location()
    git.run("fetch", "-q", "--no-tags", "--depth", "1", loc,
            f"+refs/tags/{cfg.base_tag}:refs/tags/{cfg.base_ref}")
    return {"source": cfg.base_source_name, "from": loc, "ref": cfg.base_ref}


def fetch(cfg: Config, git: Git, names: list[str]) -> list[dict]:
    if not (cfg.repo / "HEAD").exists():
        cfg.repo.mkdir(parents=True, exist_ok=True)
        git.run("init", "--bare", "-q")
    out = []
    for name in names:
        s = cfg.sources[name]
        loc = s.remote_location()
        if git.ok("remote", "get-url", name):
            git.run("remote", "set-url", name, loc)
        else:
            git.run("remote", "add", name, loc)
        specs = []
        if s.tags:
            specs.append(f"+refs/tags/{s.tags}:refs/tags/{name}/{s.tags}")
        if s.branch:
            specs.append(f"+refs/heads/{s.branch}:refs/remotes/{name}/{s.branch}")
        git.run("fetch", "-q", "--no-tags", name, *specs)
        out.append({"source": name, "from": loc, "ref": tracked_ref(git, s)})
    return out


def tracked_ref(git: Git, s: Source) -> str:
    """Newest ref this source tracks, as a name in the unified repo."""
    if s.tags:
        tags = git.tags(f"{s.name}/{s.tags}")
        if not tags:
            raise GitError(f"{s.name}: no tags match {s.tags}; run kpm fetch {s.name}")
        return tags[-1]
    return f"refs/remotes/{s.name}/{s.branch}"


def delta_range(git: Git, s: Source, ref: str) -> str:
    kind, _, arg = s.delta_base.partition(":")
    if kind == "ref":
        return f"{arg}..{ref}"
    if kind == "grep":
        base = git.run("log", "-1", "--format=%H", "-E", f"--grep={arg}", ref).strip()
        if not base:
            raise GitError(f"{s.name}: no ancestor of {ref} matches /{arg}/")
        return f"{base}..{ref}"
    raise GitError(f"{s.name}: delta_base must be 'ref:<name>' or 'grep:<regex>'")


# ── revert pairs ─────────────────────────────────────────────────────────────

_REVERT_SUBJECT = re.compile(r'^(?:[A-Z]+: (?:[A-Z]+: )?SAUCE: )?Revert "(.*)"$')


def pair_reverts(rows: list[dict]) -> tuple[int, int]:
    """Cancel a patch and its later revert inside one source's delta.

    Both rows become `dropped` only when the revert is the exact inverse of the
    original (inverse_id == delta_id). A revert that names the original but
    changes different lines is left undecided with a proposal: it may be a
    partial revert. Reverts of commits outside this delta are real patches and
    are not touched. Returns (pairs cancelled, proposals)."""
    paired, proposed = 0, 0
    used: set[int] = set()
    for i, r in enumerate(rows):
        m = _REVERT_SUBJECT.match(r["subject"])
        if not m or r["disposition"] != "undecided":
            continue
        want_sha, want_subject = r.get("reverts", ""), m.group(1)
        for j in range(i - 1, -1, -1):          # nearest earlier original
            o = rows[j]
            if j in used or o["disposition"] != "undecided":
                continue
            named = (want_sha and o["sha"].startswith(want_sha)) or o["subject"] == want_subject
            if not named:
                continue
            if r.get("inverse_id") and r["inverse_id"] == o.get("delta_id"):
                o.update(disposition="dropped", match="revert-exact",
                         reason=f"reverted in source by {r['sha'][:12]}")
                r.update(disposition="dropped", match="revert-exact",
                         reason=f"reverts {o['sha'][:12]} in source; net no change")
                r.pop("proposed", None)
                used.update((i, j))
                paired += 1
            else:
                r["proposed"] = {"disposition": "dropped", "match": "revert-subject",
                                 "note": f"names {o['sha'][:12]} but is not its exact inverse"}
                proposed += 1
            break
    return paired, proposed


# ── harvest ──────────────────────────────────────────────────────────────────

def classify(c: fp.Commit, noise: list[re.Pattern]) -> str:
    if any(rx.search(c.subject) for rx in noise):
        return "noise"
    if c.files and all(_PACKAGING.match(f) for f in c.files):
        return "config" if all(_CONFIG.match(f) for f in c.files) else "packaging"
    if not c.files:
        return "noise"            # empty commit
    return "undecided"


def harvest(cfg: Config, git: Git, name: str) -> dict:
    s = cfg.sources[name]
    if s.kind != "vendor":
        raise GitError(f"{name} is a {s.kind} source; its rows come from `kpm add`, not harvest")
    ref = tracked_ref(git, s)
    rng = delta_range(git, s, ref)
    commits = fp.scan(git, rng)
    noise = [re.compile(p) for p in s.noise + _GENERIC_NOISE]

    path = cfg.ledger_path(name)
    old = ledger.read(path)
    by_sha = {r["sha"]: r for r in old}
    by_pid = {r["patch_id"]: r for r in old if r.get("patch_id")}
    by_did = {r["delta_id"]: r for r in old if r.get("delta_id")}

    rows, new, moved = [], 0, 0
    kept = set()
    for c in commits:
        prev = by_sha.get(c.sha)
        if prev is None:
            # Vendor rebased: same content under a new SHA keeps its decision.
            prev = by_pid.get(c.patch_id) or by_did.get(c.delta_id)
            moved += prev is not None
        row = dict(prev) if prev else {"disposition": classify(c, noise)}
        new += prev is None
        if prev:
            kept.add(prev["sha"])
        row.update(sha=c.sha, date=c.date, author=c.author, subject=c.subject,
                   patch_id=c.patch_id, delta_id=c.delta_id, upstream=c.upstream)
        if c.reverts or _REVERT_SUBJECT.match(c.subject):
            row.update(reverts=c.reverts, inverse_id=c.inverse_id)
        if any(_PACKAGING.match(f) for f in c.files) and not all(_PACKAGING.match(f) for f in c.files):
            row["touches_packaging"] = True
        rows.append(row)

    vanished = [r for r in old if r["sha"] not in kept]
    ledger.write(path, rows)
    _pin(cfg, name, {"ref": ref, "sha": git.rev(ref), "range": rng})
    return {"source": name, "ref": ref, "range": rng, "commits": len(rows), "new": new,
            "moved": moved, "vanished": [{"sha": r["sha"], "subject": r["subject"],
                                          "disposition": r["disposition"]} for r in vanished]}


def pins(cfg: Config) -> dict:
    p = cfg.line_dir / "pins.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _pin(cfg: Config, name: str, pin: dict) -> None:
    """Record exactly which ref a ledger describes. Force-updated branches move; this does not."""
    all_pins = pins(cfg)
    all_pins[name] = pin
    with open(cfg.line_dir / "pins.json", "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(dict(sorted(all_pins.items())), indent=2) + "\n")


# ── policy ───────────────────────────────────────────────────────────────────

def load_policy(cfg: Config) -> list[dict]:
    p = cfg.line_dir / "policy.toml"
    if not p.exists():
        return []
    rules = tomllib.loads(p.read_text(encoding="utf-8")).get("rule", [])
    for r in rules:
        r["source"] = [r["source"]] if isinstance(r.get("source"), str) else r.get("source", [])
        r["_rx"] = re.compile(r["subject"]) if r.get("subject") else None
        if r["disposition"] in ("dropped", "deferred") and not r.get("reason"):
            raise GitError(f"policy rule {r} needs a reason")
    return rules


def apply_policy(source: str, row: dict, rules: list[dict]) -> bool:
    if row["disposition"] != "undecided" or row.get("proposed"):
        return False              # a proposal needs a person or `resolve --accept-proposed`
    for rule in rules:
        if rule["source"] and source not in rule["source"]:
            continue
        if rule["_rx"] and not rule["_rx"].search(row["subject"]):
            continue
        row["disposition"] = rule["disposition"]
        row["decided_by"] = "policy"
        if rule["disposition"] != "carry":
            row["reason"] = rule["reason"]
        return True
    return False


# ── base index ───────────────────────────────────────────────────────────────

_EMPTY_INDEX = {"patch_id": {}, "delta_id": {}, "upstream": {}, "subject": {}}


def index_range(cfg: Config, git: Git, since: str, upto: str) -> dict:
    """Fingerprints of the base source's commits in since..upto. Rebuildable cache.

    For a stable line the range is walked release by release, so each hit knows
    which release it landed in."""
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{cfg.base_source_name}-{since}-{upto}")
    cache = cfg.cache / f"base-{key}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    tags = None
    if cfg.kind == "stable":
        try:
            tags = release_tags(git, cfg.base_source_name, upto, since)
        except GitError:
            tags = None
    steps = list(zip(tags, tags[1:])) if tags else [(cfg.ref(since), cfg.ref(upto))]
    idx = {k: {} for k in _EMPTY_INDEX}
    for prev, tag in steps:
        landed = tag.split("/", 1)[1]
        for c in fp.scan(git, f"{prev}..{tag}"):
            hit = [c.sha, landed]
            if c.patch_id:
                idx["patch_id"].setdefault(c.patch_id, hit)
            if c.delta_id:
                idx["delta_id"].setdefault(c.delta_id, hit)
            for u in c.upstream:
                idx["upstream"].setdefault(u.split()[0], hit)
            idx["subject"].setdefault(f"{c.subject_key}\x00{c.email}", hit)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(idx))
    return idx


def base_index(cfg: Config, git: Git) -> dict:
    """Everything between the start of the base's series and the base tag."""
    if not cfg.series_root or cfg.series_root == cfg.base_tag:
        return {k: {} for k in _EMPTY_INDEX}
    return index_range(cfg, git, cfg.series_root, cfg.base_tag)


def _upstream_hit(table: dict, ref: str):
    sha = ref.split()[0]
    if sha in table:
        return table[sha]
    if len(sha) < 40:
        return next((v for k, v in table.items() if k.startswith(sha)), None)
    return None


def match_base(row: dict, idx: dict, email: str = ""):
    """(base_sha, landed, level) if this row's content is already in the base."""
    for u in row.get("upstream", []):
        hit = _upstream_hit(idx["upstream"], u)
        if hit:
            return *hit, "upstream-ref"
    if row.get("patch_id") in idx["patch_id"]:
        return *idx["patch_id"][row["patch_id"]], "patch-id"
    if row.get("delta_id") in idx["delta_id"]:
        return *idx["delta_id"][row["delta_id"]], "delta-id"
    return None


# ── resolve ──────────────────────────────────────────────────────────────────

_ACCOUNTED = ("carry", "in-base", "duplicate")


def resolve(cfg: Config, git: Git, names: list[str], accept_proposed: bool = False) -> list[dict]:
    """Decide every row that evidence can decide.

    Strong evidence is applied: the same content or the same upstream commit
    already accounted for in a source this one is layered on (duplicate), or
    already in the base (in-base), including an upstream commit that is simply
    an ancestor of the base. That last case is what makes a source built on an
    older kernel tractable: most of its backports are plain history by now.

    Weak evidence (an identical subject) is only recorded as a proposal, unless
    accept_proposed is set."""
    idx = base_index(cfg, git)
    base_subjects = {k.split("\x00")[0]: v for k, v in idx["subject"].items()}
    rules = load_policy(cfg)
    ledgers = ledger.read_all(cfg.line_dir / "ledger")
    counter = ledger.next_change(ledgers)
    report = []

    def order(n):  # parents before children
        depth, s = 0, cfg.sources[n]
        while s.parent:
            depth, s = depth + 1, cfg.sources[s.parent]
        return depth

    for name in sorted(names, key=order):
        rows = ledgers.get(name, [])
        stats = {"source": name, "in-base": 0, "duplicate": 0, "proposed": 0, "undecided": 0}
        pairs, _ = pair_reverts(rows)
        stats["reverted"] = pairs * 2

        # What the sources this one is layered on have already accounted for.
        by_pid, by_did, by_up, by_subject = {}, {}, {}, {}
        p = cfg.sources[name].parent
        while p:
            for r in ledgers.get(p, []):
                if not r.get("change") or r["disposition"] not in _ACCOUNTED:
                    continue
                if r.get("patch_id"):
                    by_pid.setdefault(r["patch_id"], (p, r))
                if r.get("delta_id"):
                    by_did.setdefault(r["delta_id"], (p, r))
                for u in r.get("upstream", []):
                    if " " not in u:
                        by_up.setdefault(u, (p, r))
                by_subject.setdefault(fp.subject_key(r["subject"]), (p, r))
            p = cfg.sources[p].parent

        def sibling(r):
            if r.get("patch_id") in by_pid:
                return by_pid[r["patch_id"]], "patch-id"
            if r.get("delta_id") in by_did:
                return by_did[r["delta_id"]], "delta-id"
            for u in (x for x in r.get("upstream", []) if " " not in x):
                hit = by_up.get(u) or next((v for k, v in by_up.items() if k.startswith(u) or u.startswith(k)), None)
                if hit:
                    return hit, "upstream-ref"
            return None, ""

        def accept(r, prop):
            r.pop("proposed", None)
            r.update(prop)
            stats[prop["disposition"]] += 1

        for r in rows:
            if not r.get("change"):
                dup, _how = sibling(r)
                if dup:
                    r["change"] = dup[1]["change"]
                else:
                    r["change"] = f"C-{counter:05d}"
                    counter += 1
            if r["disposition"] != "undecided":
                continue
            dup, how = sibling(r)
            if dup:
                accept(r, {"disposition": "duplicate", "of": f"{dup[0]}:{dup[1]['sha'][:12]}", "match": how})
                continue
            hit = match_base(r, idx)
            if hit:
                accept(r, {"disposition": "in-base", "base_sha": hit[0], "landed": hit[1], "match": hit[2]})
                continue
            anc = next((u for u in r.get("upstream", []) if " " not in u
                        and git.ok("merge-base", "--is-ancestor", u, cfg.base_ref)), None)
            if anc:
                accept(r, {"disposition": "in-base", "base_sha": git.rev(anc),
                           "landed": f"by {cfg.series_root or cfg.base_tag}", "match": "ancestor"})
                continue
            if r.get("proposed", {}).get("match") == "revert-subject":
                stats["proposed"] += 1
                stats["undecided"] += 1
                continue
            # Weak evidence only: an identical subject. Never applied on its own.
            key = fp.subject_key(r["subject"])
            prop = None
            if key in by_subject:
                src, other = by_subject[key]
                prop = {"disposition": "duplicate", "of": f"{src}:{other['sha'][:12]}", "match": "subject"}
            elif key in base_subjects:
                prop = {"disposition": "in-base", "base_sha": base_subjects[key][0],
                        "landed": base_subjects[key][1], "match": "subject"}
            if prop and accept_proposed:
                accept(r, prop)
                continue
            if prop:
                r["proposed"] = prop
                stats["proposed"] += 1
            else:
                r.pop("proposed", None)
            stats["undecided"] += 1
        stats["policy"] = sum(apply_policy(name, r, rules) for r in rows)
        stats["undecided"] -= stats["policy"]
        ledger.write(cfg.ledger_path(name), rows)
        report.append(stats)
    return report


# ── presence, local patches, missing fixes ───────────────────────────────────

def present(cfg: Config, git: Git, sha: str, ref: str | None = None) -> str:
    """How `sha`'s change is already in `ref` (default: the base), or "" if it is not.

    SHAs differ once a commit is backported, so ancestry alone is not enough:
    we also look for a commit that names it, one with the same subject touching
    the same files, and finally whether its diff reverse-applies to the tree."""
    ref = ref or cfg.base_ref
    full = git.rev(sha)
    if git.ok("merge-base", "--is-ancestor", full, ref):
        return "ancestor"
    files = [f for f in git.run("show", "--format=", "--name-only", full).split("\n") if f]
    for label, needle in (("named by", full[:12]), ("same subject as", git.run("show", "-s", "--format=%s", full).strip())):
        hit = git.run("log", "-1", "--format=%h", "--fixed-strings", f"--grep={needle}", ref, "--", *files,
                      check=False).strip()
        if hit:
            return f"{label} {hit}"
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if git.call("read-tree", ref, env=env).returncode == 0:
            diff = git.call("show", "--format=", "--binary", full).stdout
            if diff and git.call("apply", "--cached", "--check", "--reverse", "-", env=env, input=diff).returncode == 0:
                return "content present"
    return ""


def add_local(cfg: Config, git: Git, shas: list[str]) -> list[dict]:
    """Decide to carry commits of our own choosing (usually mainline fixes)."""
    name = cfg.local_source()
    if not name:
        raise GitError(f"line {cfg.line} has no local source in line.toml")
    rows = ledger.read(cfg.ledger_path(name))
    counter = ledger.next_change(ledger.read_all(cfg.line_dir / "ledger"))
    out = []
    for sha in shas:
        full = git.rev(sha)
        subject = git.run("show", "-s", "--format=%s", full).strip()
        if any(r["sha"] == full or full in r.get("upstream", []) for r in rows):
            out.append({"sha": full, "subject": subject, "result": "already carried"})
            continue
        how = present(cfg, git, full)
        if how:
            out.append({"sha": full, "subject": subject, "result": f"already in base ({how})"})
            continue
        c = fp.scan(git, f"{full}^!")[0]
        rows.append({"change": f"C-{counter:05d}", "sha": full, "date": c.date, "author": c.author,
                     "subject": c.subject, "disposition": "carry", "patch_id": c.patch_id,
                     "delta_id": c.delta_id, "upstream": [full] + [u for u in c.upstream if u != full]})
        counter += 1
        out.append({"sha": full, "subject": subject, "result": "added"})
    ledger.write(cfg.ledger_path(name), rows)
    return out


_FIXES = re.compile(r"^Fixes: ([0-9a-f]{8,40})", re.M)


def missing_fixes(cfg: Config, git: Git, ref: str) -> dict:
    """Upstream follow-ups to patches we carry that we do not have.

    For every carried patch that names a mainline commit, find commits in `ref`
    whose `Fixes:` tag names that commit, and report those that are neither in
    the base nor accounted for in a ledger. Also checks that each named upstream
    commit exists and has the same subject (a mangled trailer hides the patch
    from every later check)."""
    if not git.ok("rev-parse", "--verify", "-q", f"{ref}^{{commit}}"):
        raise GitError(f"{ref} is not in the unified repo; fetch it first (e.g. kpm fetch mainline)")
    text = git.run("log", "--no-merges", "--format=%H%x00%s%x00%b%x01", "-E",
                   "--grep=^Fixes: [0-9a-f]{8,}", f"{cfg.base_ref}..{ref}")
    fixers = []
    for chunk in text.split("\x01"):
        parts = chunk.strip("\n").split("\x00")
        if len(parts) == 3:
            fixers.append((parts[0], parts[1], _FIXES.findall(parts[2])))
    ledgers = {n: ledger.read(cfg.ledger_path(n)) for n in cfg.line_sources}
    known = {u.split()[0] for rows in ledgers.values() for r in rows
             if r["disposition"] != "undecided" for u in [r["sha"], *r.get("upstream", [])]}
    idx = base_index(cfg, git)

    def same(a: str, b: str) -> bool:
        n = min(len(a), len(b))
        return n >= 8 and a[:n] == b[:n]

    def covered(fsha: str) -> bool:
        return (any(same(k, fsha) for k in known) or _upstream_hit(idx["upstream"], fsha) is not None
                or git.ok("merge-base", "--is-ancestor", fsha, cfg.base_ref))

    missing, bad_trailers = [], []
    for name, rows in ledgers.items():
        for r in rows:
            if r["disposition"] != "carry":
                continue
            for u in (x for x in r.get("upstream", []) if " " not in x):
                if git.ok("cat-file", "-e", f"{u}^{{commit}}"):
                    upstream_subject = git.run("show", "-s", "--format=%s", u).strip()
                    if fp.subject_key(upstream_subject) != fp.subject_key(r["subject"]):
                        bad_trailers.append({"source": name, "change": r.get("change"), "subject": r["subject"],
                                             "upstream": u, "upstream_subject": upstream_subject})
                for fsha, fsubject, prefixes in fixers:
                    if any(same(u, pfx) for pfx in prefixes) and not covered(fsha):
                        missing.append({"source": name, "change": r.get("change"), "carried": r["subject"],
                                        "fix": fsha, "fix_subject": fsubject})
    return {"ref": ref, "missing": missing, "bad_trailers": bad_trailers}
