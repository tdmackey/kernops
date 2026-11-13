"""The ledger: one JSON line per source commit, in the source's own order."""
import json
import re
from pathlib import Path

# Dispositions that count as a decision. "undecided" is the only one that is not.
DECIDED = {"carry", "in-base", "duplicate", "superseded", "deferred", "dropped",
           "noise", "config", "packaging"}
NEEDS = {  # disposition -> field that must be present
    "in-base": "base_sha", "duplicate": "of",
    "superseded": "by", "deferred": "reason", "dropped": "reason",
}
_KEY_ORDER = ["change", "sha", "date", "author", "subject", "disposition"]
_CHANGE = re.compile(r"^C-(\d+)$")


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for r in rows:
        ordered = {k: r[k] for k in _KEY_ORDER if k in r}
        ordered.update({k: v for k, v in r.items() if k not in ordered and v not in ("", [], None)})
        lines.append(json.dumps(ordered, ensure_ascii=False))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def read_all(ledger_dir: Path) -> dict[str, list[dict]]:
    return {p.stem: read(p) for p in sorted(ledger_dir.glob("*.jsonl"))} if ledger_dir.exists() else {}


def next_change(ledgers: dict[str, list[dict]]) -> int:
    n = 0
    for rows in ledgers.values():
        for r in rows:
            m = _CHANGE.match(r.get("change", ""))
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


def pending(rows: list[dict]) -> int:
    """Rows we decided to carry that have no patch file yet (import/export not run)."""
    return sum(r["disposition"] == "carry" and not r.get("patch") for r in rows)


def problems(source: str, rows: list[dict]) -> list[str]:
    """Structural problems in one ledger. Empty list means valid."""
    out = []
    seen = set()
    for r in rows:
        tag = f"{source}:{r.get('sha', '?')[:12]}"
        d = r.get("disposition", "")
        if r.get("sha") in seen:
            out.append(f"{tag} duplicate row")
        seen.add(r.get("sha"))
        if d != "undecided" and d not in DECIDED:
            out.append(f"{tag} unknown disposition {d!r}")
        need = NEEDS.get(d)
        if need and not r.get(need):
            out.append(f"{tag} disposition {d} requires '{need}'")
    return out
