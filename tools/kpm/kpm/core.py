"""fetch, harvest and resolve: turning source trees into ledger rows."""
import json
import re
import tomllib

from . import fingerprint as fp
from . import ledger
from .config import Config, Source
from .git import Git, GitError, release_tags

# A commit touching only these paths is packaging or config, not a kernel patch.
_PACKAGING = re.compile(r"^(debian(\.[^/]+)?/|Ubuntu\.md$|Changes[^/]*\.md$)")
_CONFIG = re.compile(r"^debian(\.[^/]+)?/config/")
_GENERIC_NOISE = [r"^Linux \d+\.\d+"]


# ── fetch ────────────────────────────────────────────────────────────────────

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
    if row["disposition"] != "undecided":
        return False
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

def base_index(cfg: Config, git: Git) -> dict:
    """Fingerprints of everything between the line's series root and its base tag.

    Rebuildable cache; keyed by base tag."""
    root = cfg.base_tag.rsplit(".", 1)[0] if cfg.base_tag.count(".") >= 2 else cfg.base_tag
    cache = cfg.cache / f"base-{cfg.base_source.name}-{cfg.base_tag}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    tags = release_tags(git, cfg.base_source.name, cfg.base_tag, root)
    idx = {"patch_id": {}, "delta_id": {}, "upstream": {}, "subject": {}}
    for prev, tag in zip(tags, tags[1:]):
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

def resolve(cfg: Config, git: Git, names: list[str], accept_proposed: bool = False) -> list[dict]:
    idx = base_index(cfg, git)
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

        # Content already carried or decided by an ancestor source.
        inherited_pid, inherited_did = {}, {}
        p = cfg.sources[name].parent
        while p:
            for r in ledgers.get(p, []):
                if r.get("change"):
                    if r.get("patch_id"):
                        inherited_pid.setdefault(r["patch_id"], (p, r))
                    if r.get("delta_id"):
                        inherited_did.setdefault(r["delta_id"], (p, r))
            p = cfg.sources[p].parent

        for r in rows:
            if not r.get("change"):
                dup = inherited_pid.get(r.get("patch_id")) or inherited_did.get(r.get("delta_id"))
                if dup:
                    r["change"] = dup[1]["change"]
                else:
                    r["change"] = f"C-{counter:05d}"
                    counter += 1
            if r["disposition"] != "undecided":
                continue
            dup = inherited_pid.get(r.get("patch_id")) or inherited_did.get(r.get("delta_id"))
            if dup:
                r.update(disposition="duplicate", of=f"{dup[0]}:{dup[1]['sha'][:12]}",
                         match="patch-id" if r.get("patch_id") in inherited_pid else "delta-id")
                stats["duplicate"] += 1
                continue
            hit = match_base(r, idx)
            if hit:
                r.update(disposition="in-base", base_sha=hit[0], landed=hit[1], match=hit[2])
                r.pop("proposed", None)
                stats["in-base"] += 1
                continue
            if r.get("proposed", {}).get("match") == "revert-subject":
                stats["proposed"] += 1
                stats["undecided"] += 1
                continue
            # Weak evidence only: same subject in the base. Never auto-applied.
            weak = next((v for k, v in idx["subject"].items()
                         if k.split("\x00")[0] == fp.subject_key(r["subject"])), None)
            if weak:
                if accept_proposed:
                    r.update(disposition="in-base", base_sha=weak[0], landed=weak[1], match="subject")
                    r.pop("proposed", None)
                    stats["in-base"] += 1
                    continue
                r["proposed"] = {"disposition": "in-base", "base_sha": weak[0],
                                 "landed": weak[1], "match": "subject"}
                stats["proposed"] += 1
            stats["undecided"] += 1
        stats["policy"] = sum(apply_policy(name, r, rules) for r in rows)
        stats["undecided"] -= stats["policy"]
        ledger.write(cfg.ledger_path(name), rows)
        report.append(stats)
    return report
