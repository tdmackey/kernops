"""Thin git wrapper. Everything here works on a bare repo: no checkout needed."""
import os
import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git_env(extra: dict | None = None) -> dict:
    """Environment for every git call kpm makes.

    safe.directory: local mirrors may be owned by another user (a Windows mount).
    core.autocrlf:  kernel sources and patches must never have line endings converted."""
    return dict(os.environ, GIT_CONFIG_COUNT="4",
                GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0="*",
                GIT_CONFIG_KEY_1="core.autocrlf", GIT_CONFIG_VALUE_1="false",
                # A stalled transfer must fail, not hang: under 1 kB/s for two minutes.
                GIT_CONFIG_KEY_2="http.lowSpeedLimit", GIT_CONFIG_VALUE_2="1000",
                GIT_CONFIG_KEY_3="http.lowSpeedTime", GIT_CONFIG_VALUE_3="120", **(extra or {}))


class Git:
    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self.env = git_env()

    def call(self, *args: str, env: dict | None = None, input: bytes | None = None):
        """Run git and return the CompletedProcess (stdout/stderr as bytes)."""
        return subprocess.run(["git", "-C", str(self.repo), *args], env=dict(self.env, **(env or {})),
                              input=input, capture_output=True)

    def stream(self, *args: str) -> int:
        """Run git with its progress going straight to our stderr (long transfers)."""
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              stdout=subprocess.DEVNULL).returncode

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
        # -C matters: without it git would try to use whatever repository the
        # caller happens to be standing in, and fail if that one is unusable.
        b = subprocess.run(["git", "-C", str(self.repo), *second], env=self.env,
                           stdin=a.stdout, capture_output=True)
        if b.returncode:
            raise GitError(f"git {' '.join(second)}: {b.stderr.decode(errors='replace').strip()}")
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
