"""Content identity for commits, independent of SHA.

Strongest to weakest:
  upstream   explicit reference to another commit, parsed from the message
  patch_id   git patch-id --stable: exact diff modulo line numbers/whitespace
  delta_id   hash of only the added/removed lines per file; survives context drift
  subject    normalised subject + author; a hint, never proof
"""
import hashlib
import re
from dataclasses import dataclass, field

from .git import Git

_M = "\x01"
_FORMAT = f"{_M}COMMIT{_M}%H{_M}%an{_M}%ae{_M}%as{_M}%s{_M}%b{_M}ENDMSG{_M}"
_DIFF_OPTS = ["--no-merges", "--no-renames", "--no-color", "--no-ext-diff", "-p"]

_UPSTREAM = [
    re.compile(r"\((?:cherry picked|backported) from commit ([0-9a-f]{8,40})(?:[ \t]+([^)\n]+?))?\)"),
    re.compile(r"^commit ([0-9a-f]{40}) upstream\.?[ \t]*$", re.M),
    re.compile(r"^\[ Upstream commit ([0-9a-f]{40}) \][ \t]*$", re.M),
]
_PREFIX = re.compile(
    r"^(?:(?:UBUNTU|NVIDIA)(?:: [A-Z]{2,6})?: SAUCE: |UBUNTU: |NVIDIA: |\(no-up\) |\(upstream\) )+")
_REVERTS = re.compile(r"^This reverts commit ([0-9a-f]{7,40})", re.M)
_DIFF_HEAD = re.compile(r"^diff --git a/(.*) b/(.*)$")


@dataclass
class Commit:
    sha: str
    author: str
    email: str
    date: str
    subject: str
    body: str = ""
    patch_id: str = ""
    delta_id: str = ""
    inverse_id: str = ""      # delta_id this commit would have with +/- swapped
    inverse_patch_id: str = ""  # patch_id of this commit's diff reversed: equals X.patch_id if this exactly undoes X
    reverts: str = ""         # sha named by "This reverts commit ..."
    upstream: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    @property
    def subject_key(self) -> str:
        return subject_key(self.subject)


def subject_key(subject: str) -> str:
    return " ".join(_PREFIX.sub("", subject).lower().split())


def upstream_refs(body: str) -> list[str]:
    """SHAs this commit says it came from. Non-mainline trees are kept as 'sha tree'."""
    out: list[str] = []
    for rx in _UPSTREAM:
        for m in rx.finditer(body):
            tree = (m.group(2) or "").strip() if m.lastindex and m.lastindex >= 2 else ""
            ref = m.group(1) if not tree or tree == "upstream" else f"{m.group(1)} {tree}"
            if ref not in out:
                out.append(ref)
    return out


def delta(diff: str) -> tuple[str, list[str], str]:
    """(delta_id, files, inverse_id) for one commit's unified diff text.

    inverse_id is the delta_id of the exact opposite change: a clean revert of
    commit X has inverse_id == X.delta_id."""
    fwd: dict[str, list[str]] = {}
    touched: list[str] = []
    cur, in_hunk = None, False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            m = _DIFF_HEAD.match(line)
            cur, in_hunk = (m.group(2) if m else line), False
            touched.append(cur)
        elif line.startswith("@@"):
            in_hunk = True
        elif cur and in_hunk and line[:1] in "+-":
            norm = " ".join(line[1:].split())
            if norm:
                fwd.setdefault(cur, []).append(f"{line[0]}{norm}")
        elif cur and line.startswith(("Binary files ", "GIT binary patch")):
            fwd.setdefault(cur, []).append("binary")
    if not fwd:
        return "", touched, ""

    def digest(files: dict[str, list[str]]) -> str:
        h = hashlib.sha1()
        for path in sorted(files):
            body = "".join(l + "\n" for l in files[path])
            h.update(f"{path}:{hashlib.sha1(body.encode()).hexdigest()}\n".encode())
        return h.hexdigest()

    return digest(fwd), touched, digest({p: _invert(ls) for p, ls in fwd.items()})


_MODE_LINES = ("old mode ", "new mode ", "new file mode ", "deleted file mode ")


def binary_files(diff: str) -> list[str]:
    """The files that a diff changes as binary. Their content is not in any id here:
    delta_id has the word "binary" for each of them."""
    out, cur = [], None
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            m = _DIFF_HEAD.match(line)
            cur = m.group(2) if m else line
        elif cur and line.startswith(("Binary files ", "GIT binary patch")) and cur not in out:
            out.append(cur)
    return out


def modes(diff: str) -> list[str]:
    """The file-mode lines of a diff, each with its file: a change of the executable bit, a new or a deleted file."""
    out, cur = [], None
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            m = _DIFF_HEAD.match(line)
            cur = m.group(2) if m else line
        elif cur and line.startswith(_MODE_LINES):
            out.append(f"{cur} {line.strip()}")
    return sorted(out)


def places(diff: str) -> str:
    """Where a diff makes its changes, as an id: for each run of changed lines,
    the file, the lines, and the unchanged line before and after the run.

    delta_id leaves out context, so the same replacement in another function of
    the same file has the same delta_id. This id is different then. Line numbers
    are not part of it: a change that only moved down the file is the same.

    File modes are part of it, and so is indentation. delta_id leaves both out, and in some files indentation is
    the meaning: a line moved into an `if` block changes only its indentation."""
    runs: list[str] = []
    cur, in_hunk, before, run = None, False, "", []

    def close(after: str) -> None:
        if run:
            runs.append("\n".join([cur or "", before, *run, after]))
            run.clear()

    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            close("")
            m = _DIFF_HEAD.match(line)
            cur, in_hunk, before = (m.group(2) if m else line), False, ""
        elif line.startswith("@@"):
            close("")
            in_hunk, before = True, ""
        elif cur and not in_hunk and line.startswith(_MODE_LINES):
            # The mode of a file is part of the change: a script that loses its
            # executable bit has the same text.
            runs.append(f"{cur}\n{line.strip()}")
        elif cur and in_hunk and line[:1] in "+-":
            norm = line[1:].rstrip("\r")       # the line as it is: a space after a backslash is a change
            if norm:
                run.append(f"{line[0]}{norm}")
        elif cur and in_hunk and line[:1] == " ":
            norm = line[1:].rstrip("\r")       # the line as it is: a space after a backslash is a change
            if run:
                close(norm)
            if norm:
                before = norm
    close("")
    return hashlib.sha1("\n\x00".join(sorted(runs)).encode()).hexdigest() if runs else ""


def _invert(lines: list[str]) -> list[str]:
    """The same hunk lines as the reverse patch would list them.

    A diff emits each run as removals then additions; reversing swaps the signs
    and therefore the order of the two halves of every run."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and lines[j].startswith("-"):
            j += 1
        k = j
        while k < len(lines) and lines[k].startswith("+"):
            k += 1
        if k == i:                       # "binary" marker
            out.append(lines[i])
            i += 1
            continue
        out += ["-" + l[1:] for l in lines[j:k]] + ["+" + l[1:] for l in lines[i:j]]
        i = k
    return out


def parse_log(text: str) -> list[Commit]:
    commits: list[Commit] = []
    for chunk in text.split(f"{_M}COMMIT{_M}")[1:]:
        head, _, diff = chunk.partition(f"{_M}ENDMSG{_M}")
        sha, author, email, date, subject, body = (head.split(_M) + [""] * 6)[:6]
        c = Commit(sha=sha, author=author, email=email, date=date, subject=subject, body=body)
        c.upstream = upstream_refs(body)
        c.delta_id, c.files, c.inverse_id = delta(diff)
        m = _REVERTS.search(body)
        c.reverts = m.group(1) if m else ""
        commits.append(c)
    return commits


def scan(git: Git, rev_range: str) -> list[Commit]:
    """All non-merge commits in rev_range, oldest first, fully fingerprinted."""
    text = git.run("log", "--reverse", f"--format={_FORMAT}", *_DIFF_OPTS, rev_range)
    commits = parse_log(text)
    ids = git.pipe(["log", *_DIFF_OPTS, rev_range], ["patch-id", "--stable"])
    by_sha = {c.sha: c for c in commits}
    for line in ids.splitlines():
        pid, _, sha = line.partition(" ")
        if sha in by_sha:
            by_sha[sha].patch_id = pid
    # The reversed diff of each commit that looks like a revert. A patch-id covers
    # the context lines, so it tells a revert of X from a commit that makes the
    # opposite change at another place in the same file. The delta ids cannot.
    reverts = [c for c in commits if c.inverse_id and (c.reverts or c.subject.lstrip().startswith(("Revert", "revert"))
                                                        or 'Revert "' in c.subject)]
    for c in reverts:
        # -R also swaps the a/ and b/ path prefixes, which are part of a patch-id: swap them back.
        out = git.pipe(["show", "-R", "--src-prefix=b/", "--dst-prefix=a/",
                        *[o for o in _DIFF_OPTS if o != "--no-merges"], c.sha],
                       ["patch-id", "--stable"])
        c.inverse_patch_id = out.split(" ", 1)[0].strip() if out.strip() else ""
    return commits
