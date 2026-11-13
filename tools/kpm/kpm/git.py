"""Thin git wrapper. Everything here works on a bare repo: no checkout needed."""
import os
import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo: Path):
        self.repo = Path(repo)
        # Local mirrors may be owned by another user (e.g. a Windows mount).
        self.env = dict(os.environ, GIT_CONFIG_COUNT="1",
                        GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0="*")

    def run(self, *args: str, check: bool = True, input: bytes | None = None) -> str:
        p = subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                           input=input, capture_output=True)
        if check and p.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {p.stderr.decode(errors='replace').strip()}")
        return p.stdout.decode("utf-8", errors="replace")

    def ok(self, *args: str) -> bool:
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              capture_output=True).returncode == 0

    def rev(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", "-q", f"{ref}^{{commit}}").strip()

    def tags(self, pattern: str) -> list[str]:
        out = self.run("tag", "-l", pattern, "--sort=version:refname")
        return [t for t in out.splitlines() if t]

    def pipe(self, first: list[str], second: list[str]) -> str:
        """git <first> | git <second>"""
        a = subprocess.Popen(["git", "-C", str(self.repo), *first], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        b = subprocess.run(["git", *second], env=self.env, stdin=a.stdout, capture_output=True)
        a.stdout.close()
        a.wait()
        return b.stdout.decode("utf-8", errors="replace")


_RC = re.compile(r"-rc\d+$")


def release_tags(git: Git, source: str, upto: str, since: str) -> list[str]:
    """Namespaced stable tags from `since` to `upto` inclusive, in version order, no -rc."""
    prefix = upto.rsplit(".", 1)[0] if upto.count(".") >= 2 else upto
    tags = [t for t in git.tags(f"{source}/{prefix}*") if not _RC.search(t)]
    lo, hi = f"{source}/{since}", f"{source}/{upto}"
    if lo not in tags or hi not in tags:
        raise GitError(f"need tags {lo} and {hi} in the unified repo; run kpm fetch")
    return tags[tags.index(lo): tags.index(hi) + 1]
