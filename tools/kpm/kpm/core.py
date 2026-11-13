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


def _negotiation_tips(cfg: Config, git: Git, name: str) -> list[str]:
    """Refs to offer the server as common history.

    By default git offers every ref in the repository. In a repo that holds
    several unrelated vendor trees that is slow enough to stall some servers
    outright (Launchpad never answered). Offer only what this source can share:
    its own refs, its parent's, and the upstream release the line is built on."""
    s = cfg.sources[name]
    patterns = [f"refs/tags/{name}/*", f"refs/remotes/{name}/*"]
    if s.upstream:
        # A tree built on a different release shares nothing with the line's
        # base or with its parent's newer tree. Offering those makes git walk
        # thousands of commits the server has never seen before it reaches
        # common ground, which is the same stall by another route.
        patterns.append(f"refs/tags/mainline/{s.upstream}")
    else:
        if s.parent:
            patterns.append(f"refs/tags/{s.parent}/*")
        if cfg.series_root:
            patterns.append(f"refs/tags/{cfg.base_source_name}/{cfg.series_root}")
        if cfg.upstream:
            patterns.append(f"refs/tags/mainline/{cfg.upstream}")
    return [p for p in patterns if git.run("for-each-ref", "--count=1", p, check=False).strip()]


def _ensure_upstream_tag(cfg: Config, git: Git, upstream: str) -> None:
    """Have refs/tags/mainline/<upstream> locally, so it can anchor negotiation."""
    if not upstream or "mainline" not in cfg.sources:
        return
    if git.ok("rev-parse", "--verify", "-q", f"refs/tags/mainline/{upstream}"):
        return
    loc = cfg.sources["mainline"].remote_location()
    depth = [] if _has_history(git) else ["--depth", "1"]
    git.stream("fetch", "--no-tags", *depth, loc, f"+refs/tags/{upstream}:refs/tags/mainline/{upstream}")


def _has_history(git: Git) -> bool:
    return bool(git.run("for-each-ref", "--count=1", check=False).strip())


def fetch(cfg: Config, git: Git, names: list[str]) -> list[dict]:
    if not (cfg.repo / "HEAD").exists():
        cfg.repo.mkdir(parents=True, exist_ok=True)
        git.run("init", "--bare", "-q")
    _ensure_upstream_tag(cfg, git, cfg.upstream)
    out = []
    for name in names:
        s = cfg.sources[name]
        _ensure_upstream_tag(cfg, git, s.upstream)
        loc = s.remote_location()
        if git.ok("remote", "get-url", name):
            git.run("remote", "set-url", name, loc)
        else:
            git.run("remote", "add", name, loc)
        tags = cfg.base_tags if (name == cfg.base_source_name and cfg.base_tags) else s.tags
        specs = []
        if tags:
            specs.append(f"+refs/tags/{tags}:refs/tags/{name}/{tags}")
        if s.branch:
            specs.append(f"+refs/heads/{s.branch}:refs/remotes/{name}/{s.branch}")
        tips = [f"--negotiation-tip={t}" for t in _negotiation_tips(cfg, git, name)]
        if git.stream("fetch", "--no-tags", *tips, name, *specs):
            raise GitError(f"fetch of {name} from {loc} failed (see git's message above)")
        ref = cfg.base_ref if name == cfg.base_source_name else tracked_ref(git, s)
        out.append({"source": name, "from": loc, "ref": ref, "negotiated_from": len(tips)})
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
    original: its reversed diff has the original's patch-id, which covers the
    context lines, so the change is undone at the same place. The same changed
    lines alone (inverse_id == delta_id) are not enough: a commit can make the
    opposite change in another function of the same file. That case, and a
    revert that changes different lines, is left undecided with a proposal. Reverts of commits outside this delta are real patches and
    are not touched. Returns (pairs cancelled, proposals)."""
    paired, proposed = 0, 0
    used: set[int] = set()
    for i, r in enumerate(rows):
        m = _REVERT_SUBJECT.match(r["subject"])
        if not m or r["disposition"] != "undecided":
            continue
        want_sha, want_subject = r.get("reverts", ""), m.group(1)
        r.pop("proposed", None)                 # re-derived below; an old one may be stale
        earlier = [j for j in range(i - 1, -1, -1)
                   if j not in used and rows[j]["disposition"] == "undecided"]
        # The commit the revert names, else the nearest earlier one with that subject.
        cands = [j for j in earlier if want_sha and rows[j]["sha"].startswith(want_sha)] \
            or [j for j in earlier if rows[j]["subject"] == want_subject]
        for j in cands[:1]:
            o = rows[j]
            if r.get("inverse_patch_id") and r["inverse_patch_id"] == o.get("patch_id"):
                o.update(disposition="dropped", match="revert-exact",
                         reason=f"reverted in source by {r['sha'][:12]}")
                r.update(disposition="dropped", match="revert-exact",
                         reason=f"reverts {o['sha'][:12]} in source; net no change")
                r.pop("proposed", None)
                used.update((i, j))
                paired += 1
            else:
                same_lines = bool(r.get("inverse_id")) and r["inverse_id"] == o.get("delta_id")
                r["proposed"] = {"disposition": "dropped", "match": "revert-subject",
                                 "note": (f"names {o['sha'][:12]} and reverses the same lines, but not provably at the "
                                          "same place: compare the two diffs" if same_lines else
                                          f"names {o['sha'][:12]} but is not its exact inverse")}
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
    # Vendor rebased: the same content under a new SHA keeps its decision. Only a
    # row whose SHA left the tree can be the "same" commit, and only once: a patch
    # that is re-applied later in the same tree is a new commit, not a moved one.
    still = {c.sha for c in commits}
    gone_pid, gone_did = {}, {}
    for r in old:
        if r["sha"] not in still:
            if r.get("patch_id"):
                gone_pid.setdefault(r["patch_id"], []).append(r)
            if r.get("delta_id"):
                gone_did.setdefault(r["delta_id"], []).append(r)
    taken: set[str] = set()

    def moved_from(c):
        # The same patch-id is the same diff. The same delta-id is only the same
        # changed lines per file: two different fixes can share it. It counts
        # here only with a second sign, the same title; otherwise the commit is
        # new and gets its own decision.
        for r in gone_pid.get(c.patch_id, []):
            if r["sha"] not in taken:
                taken.add(r["sha"])
                return r
        for r in gone_did.get(c.delta_id, []):
            if r["sha"] not in taken and fp.subject_key(r["subject"]) == c.subject_key:
                taken.add(r["sha"])
                return r
        return None

    rows, new, moved = [], 0, 0
    kept = set()
    for c in commits:
        prev = by_sha.get(c.sha)
        if prev is None:
            prev = moved_from(c)
            moved += prev is not None
            if prev and (prev["disposition"] == "duplicate" or str(prev.get("match", "")).startswith("revert")):
                # That decision came from the commits around it, which have moved too.
                prev = {k: v for k, v in prev.items()
                        if k not in ("of", "match", "reason", "decided_by", "proposed")}
                prev["disposition"] = "undecided"
        row = dict(prev) if prev else {"disposition": classify(c, noise)}
        new += prev is None
        if prev:
            kept.add(prev["sha"])
        row.update(sha=c.sha, date=c.date, author=c.author, subject=c.subject,
                   patch_id=c.patch_id, delta_id=c.delta_id, upstream=c.upstream)
        if c.reverts or _REVERT_SUBJECT.match(c.subject):
            row.update(reverts=c.reverts, inverse_id=c.inverse_id, inverse_patch_id=c.inverse_patch_id)
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
    ledger.atomic_write(cfg.line_dir / "pins.json", json.dumps(dict(sorted(all_pins.items())), indent=2) + "\n")


# ── policy ───────────────────────────────────────────────────────────────────

def load_policy(cfg: Config) -> list[dict]:
    p = cfg.line_dir / "policy.toml"
    if not p.exists():
        return []
    rules = tomllib.loads(p.read_text(encoding="utf-8")).get("rule", [])
    for r in rules:
        r["source"] = [r["source"]] if isinstance(r.get("source"), str) else r.get("source", [])
        r["_rx"] = re.compile(r["subject"]) if r.get("subject") else None
        if r.get("disposition") not in ledger.DECIDED:
            raise GitError(f"policy rule {r} has no valid disposition")
        if not str(r.get("reason", "")).strip():
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
        row["reason"] = rule["reason"]
        return True
    return False


# ── base index ───────────────────────────────────────────────────────────────

_EMPTY_INDEX = {"patch_id": {}, "delta_id": {}, "upstream": {}, "subject": {}}
_INDEX_VERSION = 3        # bump when the fingerprints or what is indexed change


def index_range(cfg: Config, git: Git, since: str, upto: str, since_ref: str = "") -> dict:
    """Fingerprints of the base source's commits in since..upto. Rebuildable cache.

    For a stable line the range is walked release by release, so each hit knows
    which release it landed in. since_ref: the ref of `since` when it is not a
    tag of the base source (the mainline release under an Ubuntu base)."""
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{cfg.base_source_name}-{since}-{upto}")
    cache = cfg.cache / f"base-v{_INDEX_VERSION}-{key}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except ValueError:
            cache.unlink()                      # an interrupted write; rebuild
    tags = None
    if cfg.kind == "stable":
        try:
            tags = release_tags(git, cfg.base_source_name, upto, since)
        except GitError:
            tags = None
    steps = list(zip(tags, tags[1:])) if tags else [(since_ref or cfg.ref(since), cfg.ref(upto))]
    idx = {k: {} for k in _EMPTY_INDEX}
    for prev, tag in steps:
        landed = tag.split("/", 1)[1]
        for c in fp.scan(git, f"{prev}..{tag}"):
            # A commit that exactly undoes an earlier one in the range: neither is
            # "in the base". (If it is applied again later, that one is indexed.)
            undone = idx["patch_id"].get(c.inverse_patch_id) if c.inverse_patch_id else None
            if undone and (c.reverts or _REVERT_SUBJECT.match(c.subject)):
                for table in idx.values():
                    for k in [k for k, v in table.items() if v[0] == undone[0]]:
                        del table[k]
                continue
            hit = [c.sha, landed]
            if c.patch_id:
                idx["patch_id"].setdefault(c.patch_id, hit)
            if c.delta_id:
                idx["delta_id"].setdefault(c.delta_id, hit)
            for u in c.upstream:
                idx["upstream"].setdefault(u.split()[0], hit)
            idx["subject"].setdefault(f"{c.subject_key}\x00{c.email}", hit)
    ledger.atomic_write(cache, json.dumps(idx))
    return idx


def base_index(cfg: Config, git: Git) -> dict:
    """Everything between the start of the base's series and the base tag.

    An Ubuntu base has no series of its own: its start is the mainline release
    that Canonical built it on. Without this, no vendor commit on such a line
    is found in the base."""
    if cfg.kind != "stable":
        root = f"mainline/{cfg.upstream}" if cfg.upstream else ""
        if not root or not git.ok("rev-parse", "--verify", "-q", f"{root}^{{commit}}"):
            return {k: {} for k in _EMPTY_INDEX}
        return index_range(cfg, git, cfg.upstream, cfg.base_tag, since_ref=root)
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


def match_base(row: dict, idx: dict):
    """(base_sha, landed, level) if this row's content is already in the base.

    A "delta-id" hit is not proof: the hash covers the changed lines per file,
    not where they are, so two small changes to one file can collide. Callers
    confirm it against the tree (content_in) before acting on it."""
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

        def unproven(r, prop):
            r["proposed"] = prop
            stats["proposed"] += 1
            stats["undecided"] += 1

        for r in rows:
            if not r.get("change"):
                dup, how = sibling(r)
                if dup and how != "delta-id":
                    r["change"] = dup[1]["change"]
                else:
                    r["change"] = f"C-{counter:05d}"
                    counter += 1
            if r["disposition"] != "undecided":
                continue
            dup, how = sibling(r)
            if dup:
                prop = {"disposition": "duplicate", "of": f"{dup[0]}:{dup[1]['sha'][:12]}", "match": how}
                # The same changed lines are not yet the same change. Ask for a second
                # sign: the same title, or the other commit's tree containing ours (which
                # only holds when both trees are on the same kernel).
                if how == "delta-id" and fp.subject_key(r["subject"]) != fp.subject_key(dup[1]["subject"])                         and not content_in(git, r["sha"], dup[1]["sha"]):
                    unproven(r, prop)
                    continue
                accept(r, prop)
                continue
            hit = match_base(r, idx)
            if hit:
                prop = {"disposition": "in-base", "base_sha": hit[0], "landed": hit[1], "match": hit[2]}
                if hit[2] == "delta-id" and not content_in(git, r["sha"], cfg.base_ref):
                    unproven(r, prop)
                    continue
                accept(r, prop)
                continue
            anc = next((u for u in r.get("upstream", []) if " " not in u
                        and git.ok("merge-base", "--is-ancestor", u, cfg.base_ref)), None)
            if anc:
                prop = {"disposition": "in-base", "base_sha": git.rev(anc),
                        "landed": f"by {cfg.series_root or cfg.base_tag}", "match": "ancestor"}
                # In the history is not in the tree: the base can have reverted it.
                undone = reverted_by(git, anc, cfg.base_ref)
                if undone:
                    unproven(r, dict(prop, note=f"{anc[:12]} is in the base's history, but {undone[:12]} reverts it there"))
                else:
                    accept(r, prop)
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
        if rows or cfg.ledger_path(name).exists():
            ledger.write(cfg.ledger_path(name), rows)
        report.append(stats)
    return report


# ── presence, local patches, missing fixes ───────────────────────────────────

def present(cfg: Config, git: Git, sha: str, ref: str | None = None) -> str:
    """How `sha`'s change is already in `ref` (default: the base), or "" if it is not.

    Proof is one of: the commit is in the history and not reverted there; a
    commit that names it as its origin is, likewise; or its diff applies in
    reverse to the tree. A commit with the same title is not proof: two
    different fixes can share a title (see same_subject)."""
    ref = ref or cfg.base_ref
    full = git.rev(sha)
    if git.ok("merge-base", "--is-ancestor", full, ref) and not reverted_by(git, full, ref):
        return "ancestor"
    files = [f for f in git.run("show", "--format=", "--name-only", full).split("\n") if f]
    # A revert ("This reverts commit <sha>") and a Fixes: line mention the same
    # SHA, so the mention has to be the right kind: the commit's stated origin.
    log = git.run("log", "--format=%H%x00%s%x00%b%x01", "--fixed-strings", f"--grep={full[:12]}", ref, "--", *files,
                  check=False)
    for chunk in log.split("\x01"):
        parts = chunk.strip("\n").split("\x00")
        if len(parts) != 3 or _REVERT_SUBJECT.match(parts[1]):
            continue
        if any(full.startswith(u.split()[0]) for u in fp.upstream_refs(parts[2])) and not reverted_by(git, parts[0], ref):
            return f"named by {parts[0][:12]}"
    return "content present" if content_in(git, full, ref) else ""


def same_subject(git: Git, sha: str, ref: str) -> str:
    """A commit in `ref` with the same title that touches the same files, or "". A hint, never proof."""
    full = git.rev(sha)
    files = [f for f in git.run("show", "--format=", "--name-only", full).split("\n") if f]
    subject = git.run("show", "-s", "--format=%s", full).strip()
    log = git.run("log", "--format=%h%x00%s", "--fixed-strings", f"--grep={subject}", ref, "--", *files, check=False)
    for line in log.splitlines():
        h, _, s = line.partition("\x00")
        if not _REVERT_SUBJECT.match(s) and fp.subject_key(s) == fp.subject_key(subject):
            return h
    return ""


_REVERTS_LINE = re.compile(r"^This reverts commit ([0-9a-f]{7,40})", re.M)


def reverts_in(git: Git, since: str, upto: str, extra=()) -> dict[str, str]:
    """The commits that are undone at the end of since..upto: {commit: the commit that reverts it}.

    A key is the commit as the revert names it (7 to 40 hex digits). A revert
    is a commit too, and can be reverted: with A, B = revert of A, C = revert
    of B, the result is {B: C}. A stands again, B does not.

    extra: (revert, the commit it names) pairs that come after `upto`, oldest
    first: the reverts of a patch series on that base. They go through the
    same chain, so a patch that reverts C above gives {A: B, C: patch}."""
    log = git.run("log", "--reverse", "--format=%H%x00%b%x01", "-E", "--grep=^This reverts commit [0-9a-f]{7,40}",
                  f"{since}..{upto}", check=False)
    target_of: dict[str, str] = {}      # a revert commit -> the commit it names
    undone: dict[str, str] = {}         # a commit -> the revert that is in effect on it

    def revert_named(name: str) -> str:
        """The revert commit that `name` (possibly short) refers to, if it is one of ours."""
        return next((r for r in target_of if r.startswith(name)), "")

    def undo(name: str, by: str, depth: int = 0) -> None:
        undone[name] = by
        r = revert_named(name)
        if r and depth < 32:
            restore(target_of[r], depth + 1)            # undoing a revert puts its target back

    def restore(name: str, depth: int = 0) -> None:
        for k in [k for k in undone if k.startswith(name) or name.startswith(k)]:
            del undone[k]
        r = revert_named(name)
        if r and depth < 32:
            undo(target_of[r], r, depth + 1)             # a revert that is back undoes its target again

    for chunk in log.split("\x01"):                      # oldest first
        rev, _, body = chunk.strip("\n").partition("\x00")
        for target in _REVERTS_LINE.findall(body) if rev else []:
            target_of[rev] = target
            undo(target, rev)
    for rev, target in extra:
        target_of[rev] = target
        undo(target, rev)
    return undone


def still_in_base(cfg: Config, git: Git, row: dict, old_ref: str, new_ref: str, reverted: dict[str, str]) -> str:
    """Why an `in-base` row no longer holds after a move from old_ref to new_ref, or "" if it does.

    The row was right for the old base. It stops being right if the new base
    does not contain the commit it rests on, or reverts it, or (for a row that
    rests on content alone) no longer has the content."""
    sha = row.get("base_sha", "")
    if row.get("match") != "content-present":
        if sha and not git.ok("merge-base", "--is-ancestor", sha, new_ref):
            return f"{sha[:12]} is not in the history of the new base"
        by = next((r for t, r in reverted.items() if sha and sha.startswith(t)), "")
        return f"the new base reverts {sha[:12]} ({by[:12]})" if by else ""
    # Content alone. A patch that we had changed by hand landed as we changed it,
    # not as the vendor wrote it: check the diff that was kept when it landed.
    kept = cfg.line_dir / row["landed_patch"] if row.get("landed_patch") else None
    if kept and kept.exists():
        if diff_in(git, kept.read_bytes(), new_ref):
            return ""
        return f"the change as we adapted it ({row['landed_patch']}) is not in the new base any more"
    # Nothing to recheck if the files did not change between the two bases.
    if not git.ok("cat-file", "-e", f"{row['sha']}^{{commit}}"):
        return ""
    files = [f for f in git.run("show", "--format=", "--name-only", row["sha"]).split("\n") if f]
    if not files or git.ok("diff", "--quiet", old_ref, new_ref, "--", *files):
        return ""
    return "" if content_in(git, row["sha"], new_ref) else "its content is not in the new base any more"


def reverted_by(git: Git, sha: str, ref: str, _depth: int = 0) -> str:
    """The commit in sha..ref that reverts `sha` and is not itself reverted, or "".

    A commit that is an ancestor of a tree is in its history, not necessarily
    in its content."""
    full = git.rev(sha)
    log = git.run("log", "--format=%H%x00%b%x01", "--fixed-strings", f"--grep=This reverts commit {full[:7]}",
                  f"{full}..{ref}", check=False)
    for chunk in log.split("\x01"):
        rev, _, body = chunk.strip("\n").partition("\x00")
        if rev and any(full.startswith(m) for m in _REVERTS_LINE.findall(body)):
            if _depth < 4 and reverted_by(git, rev, ref, _depth + 1):
                continue                     # the revert was reverted: the commit stands
            return rev
    return ""


def content_in(git: Git, sha: str, tree: str) -> bool:
    """True if commit `sha`'s whole change is present in `tree`: its diff applies in reverse."""
    return diff_in(git, git.call("show", "--format=", "--binary", "--no-renames", sha).stdout, tree)


def diff_in(git: Git, diff: bytes, tree: str) -> bool:
    """True if the whole change of `diff` (a patch, with or without a mail header) is present in `tree`."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if git.call("read-tree", f"{tree}^{{tree}}", env=env).returncode:
            return False
        return bool(diff) and git.call("apply", "--cached", "--check", "--reverse", "-",
                                       env=env, input=diff).returncode == 0


def add_local(cfg: Config, git: Git, shas: list[str], reason: str = "") -> list[dict]:
    """Decide to carry commits of our own choosing (usually mainline fixes)."""
    reason = reason.strip() or "our own backport of an upstream commit"
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
        twin = same_subject(git, full, cfg.base_ref)
        c = fp.scan(git, f"{full}^!")[0]
        rows.append({"change": f"C-{counter:05d}", "sha": full, "date": c.date, "author": c.author,
                     "subject": c.subject, "disposition": "carry", "reason": reason, "patch_id": c.patch_id,
                     "delta_id": c.delta_id, "upstream": [full] + [u for u in c.upstream if u != full]})
        if c.reverts or _REVERT_SUBJECT.match(c.subject):
            # As harvest does. Without it, kpm cves cannot see that the series removes a commit.
            rows[-1].update(reverts=c.reverts, inverse_id=c.inverse_id, inverse_patch_id=c.inverse_patch_id)
        counter += 1
        added = {"sha": full, "subject": subject, "result": "added"}
        if twin:
            # Not proof that the change is there, so the commit is added. A person compares.
            added["note"] = f"the base has {twin} with the same title and different content: compare the two"
        out.append(added)
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
