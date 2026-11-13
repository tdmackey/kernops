"""End-to-end kpm flow on small synthetic git repositories.

Builds a tiny "kernel" with a mainline branch, a stable branch with releases, and
a vendor tree, then drives the real CLI through fetch, harvest, resolve, import,
export, verify, apply, add, fixes and rebase.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "kpm"))

from kpm import cli, ledger  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.invalid",
    "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false",
}


@unittest.skipUnless(shutil.which("git") and shutil.which("tar"), "needs git and tar")
class Flow(unittest.TestCase):
    def g(self, repo, *args):
        p = subprocess.run(["git", "-C", str(repo), *args], env=dict(os.environ, **GIT_ENV), capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        return p.stdout.decode().strip()

    def commit(self, repo, files, message):
        for path, content in files.items():
            f = Path(repo) / path
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "w", newline="\n") as fh:
                fh.write(content)
        self.g(repo, "add", "-A")
        self.g(repo, "commit", "-q", "-m", message)
        return self.g(repo, "rev-parse", "HEAD")

    def kpm(self, *argv, ok=(0,)):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--json", *argv])
        self.assertIn(rc, ok, out.getvalue())
        text = out.getvalue().strip()
        return json.loads(text) if text else None

    def rows(self):
        return {r["subject"]: r for r in ledger.read(self.mono / "kernel/lines/t/ledger/vendor.jsonl")}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kpm-flow-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        mirrors, self.mono = self.tmp / "mirrors", self.tmp / "mono"
        linux, vendor = mirrors / "linux", mirrors / "vendor"

        # mainline, with a v1.0 release
        linux.mkdir(parents=True)
        self.g(linux, "init", "-q", "-b", "master")
        self.commit(linux, {"a.c": "l1\nl2\nl3\n"}, "init")
        self.g(linux, "tag", "v1.0")
        self.m1 = self.commit(linux, {"a.c": "l1\nl2 fixed\nl3\n"}, "a: fix l2")
        self.m2 = self.commit(linux, {"b.c": "b\n"}, "b: add b")
        self.m4 = self.commit(linux, {"b.c": "b better\n"}, f'b: fix b\n\nFixes: {self.m2[:12]} ("b: add b")')

        # stable branch: v1.0.1 takes the mainline fix
        self.g(linux, "checkout", "-q", "-b", "linux-1.0.y", "v1.0")
        self.commit(linux, {"a.c": "l1\nl2 fixed\nl3\n"}, f"a: fix l2\n\ncommit {self.m1} upstream.")
        self.g(linux, "tag", "v1.0.1")

        # vendor tree on v1.0
        self.g(mirrors, "clone", "-q", "linux", "vendor")
        self.g(vendor, "checkout", "-q", "-b", "vendor", "v1.0")
        self.commit(vendor, {"s.c": "sauce\n"}, "UBUNTU: SAUCE: s: add s")
        self.commit(vendor, {"a.c": "l1\nl2 fixed\nl3\n"}, f"a: fix l2\n\n(cherry picked from commit {self.m1})")
        self.commit(vendor, {"debian.v/rules": "rules\n"}, "UBUNTU: [Packaging] rules")
        self.commit(vendor, {"a.c": "l1\nl2 fixed\nl3 tweak\n"}, "UBUNTU: SAUCE: c: tweak")
        self.g(vendor, "revert", "--no-edit", "HEAD")
        self.commit(vendor, {"d.c": "d\n", "debian.v/control": "ctl\n"}, "UBUNTU: SAUCE: d: add d")
        self.g(vendor, "tag", "Vendor-1")

        # v1.0.2 upstreams the vendor's s.c; v1.0.3 adds d.c alongside something else
        self.g(linux, "checkout", "-q", "linux-1.0.y")
        self.commit(linux, {"s.c": "sauce\n"}, "s: add s")
        self.g(linux, "tag", "v1.0.2")
        self.commit(linux, {"d.c": "d\n", "e.c": "e\n"}, "d, e: add")
        self.g(linux, "tag", "v1.0.3")
        self.g(linux, "checkout", "-q", "master")

        k = self.mono / "kernel"
        (k / "lines/t").mkdir(parents=True)
        (k / "sources.toml").write_text(
            '[stable]\nrole = "base"\nurl = "x"\nmirror = "linux"\ntags = "v1.0*"\n'
            '[mainline]\nrole = "reference"\nurl = "x"\nmirror = "linux"\nbranch = "master"\n'
            '[vendor]\nband = 0\nurl = "x"\nmirror = "vendor"\ntags = "Vendor-*"\n'
            'delta_base = "ref:stable/v1.0"\npackaging_dir = "debian.v"\n'
            '[local]\nkind = "local"\nband = 6000\n')
        (k / "lines/t/line.toml").write_text(
            'kind = "stable"\nbase_source = "stable"\nbase = "v1.0.1"\nseries_root = "v1.0"\n'
            'sources = ["vendor", "local"]\n')
        (k / "lines/t/policy.toml").write_text('[[rule]]\nsource = "vendor"\ndisposition = "carry"\nreason = "test: carry everything"\n')

        env = {"KPM_MONO": str(self.mono), "KPM_LINE": "t", "KPM_MIRRORS": str(mirrors),
               "KPM_REPO": str(self.tmp / "kernel.git"), "KPM_WORK": str(self.tmp / "work"),
               "KPM_CACHE": str(self.tmp / "cache"), **GIT_ENV}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_full_flow(self):
        series = self.mono / "kernel/lines/t/series"

        # ── account for every vendor commit ──
        self.kpm("fetch")
        self.kpm("fetch", "mainline")
        self.assertEqual(self.kpm("harvest")[0]["commits"], 6)
        self.kpm("resolve")
        r = self.rows()
        self.assertEqual((r["a: fix l2"]["disposition"], r["a: fix l2"]["match"], r["a: fix l2"]["landed"]),
                         ("in-base", "upstream-ref", "v1.0.1"))
        self.assertEqual(r["UBUNTU: [Packaging] rules"]["disposition"], "packaging")
        self.assertEqual(r["UBUNTU: SAUCE: c: tweak"]["disposition"], "dropped")
        self.assertEqual(r['Revert "UBUNTU: SAUCE: c: tweak"']["match"], "revert-exact")
        self.assertEqual(r["UBUNTU: SAUCE: s: add s"]["disposition"], "carry")
        self.assertTrue(r["UBUNTU: SAUCE: d: add d"]["touches_packaging"])
        self.assertFalse(self.kpm("verify", "--strict", ok=(1,))["ok"])       # nothing exported yet

        # ── ledger -> branch -> patch files ──
        self.assertEqual(self.kpm("import")["applied"], 2)
        self.assertEqual(self.kpm("export")["patches"], 2)
        self.assertEqual(series.read_text().split(), ["0000-vendor/0001-s-add-s.patch", "0000-vendor/0002-d-add-d.patch"])
        d_patch = (self.mono / "kernel/lines/t/patches/0000-vendor/0002-d-add-d.patch").read_text()
        self.assertNotIn("debian.v", d_patch)                                  # packaging stripped
        self.assertIn("Ledger-Origin: vendor@", d_patch)
        self.assertTrue(self.rows()["UBUNTU: SAUCE: d: add d"]["adapted"])
        self.assertTrue(self.kpm("verify", "--strict")["ok"])

        # ── a build tree needs no git ──
        tree = self.tmp / "tree"
        self.assertEqual(self.kpm("apply", str(tree))["patches"], 2)
        self.assertEqual((tree / "a.c").read_text(), "l1\nl2 fixed\nl3\n")
        self.assertTrue((tree / "s.c").exists() and (tree / "d.c").exists())
        self.assertFalse((tree / "debian.v").exists() or (tree / ".git").exists())

        # ── our own backports ──
        added = {a["sha"]: a["result"] for a in self.kpm("add", self.m1, self.m2)}
        self.assertTrue(added[self.m1].startswith("already in base"))
        self.assertEqual(added[self.m2], "added")
        fx = self.kpm("verify", "--fixes", "mainline/master", ok=(1,))        # m4 fixes the m2 we now carry
        self.assertTrue(any(self.m4[:12] in e for e in fx["errors"]), fx["errors"])
        self.kpm("add", self.m4)
        self.assertFalse([e for e in self.kpm("verify", "--fixes", "mainline/master", ok=(0, 1))["errors"]
                          if "missing upstream fix" in e])
        imp = self.kpm("import")                                               # rebuilt: 2 from files, 2 picked
        self.assertEqual((imp["from_patch"], imp["picked"]), (2, 2))
        self.assertEqual(self.kpm("export")["patches"], 4)
        self.assertEqual(series.read_text().split()[2:], ["6000-local/6001-b-add-b.patch", "6000-local/6002-b-fix-b.patch"])

        # ── rebase: content that landed upstream drops out ──
        rb = self.kpm("rebase", "v1.0.2")
        self.assertEqual([(x["subject"], x["match"]) for x in rb["landed"]], [("UBUNTU: SAUCE: s: add s", "patch-id")])
        self.assertIn('base = "v1.0.2"', (self.mono / "kernel/lines/t/line.toml").read_text())
        self.assertEqual(len(series.read_text().split()), 3)
        self.assertTrue(Path(rb["range_diff"]).exists())

        rb = self.kpm("rebase", "v1.0.3")                                      # found by content, not identity
        self.assertEqual([(x["subject"], x["match"]) for x in rb["landed"]],
                         [("UBUNTU: SAUCE: d: add d", "content-present")])
        self.assertEqual(series.read_text().split(), ["6000-local/6001-b-add-b.patch", "6000-local/6002-b-fix-b.patch"])
        r = self.rows()
        self.assertEqual((r["UBUNTU: SAUCE: s: add s"]["disposition"], r["UBUNTU: SAUCE: s: add s"]["landed"]),
                         ("in-base", "v1.0.2"))
        self.assertEqual(r["UBUNTU: SAUCE: d: add d"]["landed"], "v1.0.3")
        self.assertFalse(list((self.mono / "kernel/lines/t/patches/0000-vendor").glob("*.patch")))
        self.assertTrue(self.kpm("verify", "--strict")["ok"])
        self.assertTrue(self.kpm("rebase", "v1.0.3")["unchanged"])

        tree2 = self.tmp / "tree2"
        self.kpm("apply", str(tree2))
        self.assertEqual((tree2 / "b.c").read_text(), "b better\n")
        lines = cli.main(["lines"])                                            # plain listing still works
        self.assertEqual(lines, 0)

    def test_relanded_reverted_and_guarded(self):
        mirrors = self.tmp / "mirrors"
        linux, vendor = mirrors / "linux", mirrors / "vendor"
        line = self.mono / "kernel/lines/t"

        # The base takes s.c in v1.0.2 and reverts it in v1.0.4: it is not in the base.
        self.g(linux, "checkout", "-q", "linux-1.0.y")
        self.g(linux, "revert", "--no-edit", "v1.0.2")
        self.g(linux, "tag", "v1.0.4")
        self.g(linux, "checkout", "-q", "master")
        (line / "line.toml").write_text((line / "line.toml").read_text().replace('"v1.0.1"', '"v1.0.4"'))

        self.kpm("fetch")
        self.kpm("harvest")
        self.kpm("resolve")
        r = self.rows()
        self.assertEqual(r["UBUNTU: SAUCE: s: add s"]["disposition"], "carry")      # not in-base
        self.assertEqual(r["UBUNTU: SAUCE: c: tweak"]["disposition"], "dropped")     # reverted in the vendor tree

        # The vendor applies the reverted patch again. It is a new commit with the
        # old content, and must not inherit "dropped".
        self.commit(vendor, {"a.c": "l1\nl2 fixed\nl3 tweak\n"}, "UBUNTU: SAUCE: c: tweak")
        self.g(vendor, "tag", "Vendor-2")
        self.kpm("fetch")
        h = self.kpm("harvest")[0]
        self.assertEqual((h["commits"], h["new"], h["moved"]), (7, 1, 0))
        self.kpm("resolve")
        rows = ledger.read(line / "ledger/vendor.jsonl")
        tweaks = [x["disposition"] for x in rows if x["subject"] == "UBUNTU: SAUCE: c: tweak"]
        self.assertEqual(tweaks, ["dropped", "carry"])

        # A resync where one commit leaves the tree and a different one arrives with
        # the same changed lines in the same file, at another place. The new commit
        # must not take over the decision of the one that left.
        self.g(vendor, "checkout", "-q", "-b", "resync", "v1.0")
        body = "".join(f"f{i}() {{\n\tx = 0;\n}}\n" for i in range(1, 9))
        self.commit(vendor, {"twin.c": body}, "UBUNTU: SAUCE: twin: add twin.c")
        self.commit(vendor, {"twin.c": body.replace("f1() {\n\tx = 0;", "f1() {\n\tx = 1;")}, "UBUNTU: SAUCE: twin: fix f1")
        self.g(vendor, "tag", "Twin-1")
        (self.mono / "kernel/sources.toml").write_text(
            (self.mono / "kernel/sources.toml").read_text().replace('tags = "Vendor-*"', 'tags = "Twin-*"'))
        self.kpm("fetch"); self.kpm("harvest")
        self.kpm("decide", "dropped", "--source", "vendor", "--subject", "fix f1", "--reason", "test: not wanted")
        self.g(vendor, "reset", "-q", "--hard", "HEAD~1")
        self.commit(vendor, {"twin.c": body.replace("f8() {\n\tx = 0;", "f8() {\n\tx = 1;")}, "UBUNTU: SAUCE: twin: fix f8")
        self.g(vendor, "tag", "Twin-2")
        self.kpm("fetch")
        h = self.kpm("harvest")[0]
        self.assertEqual((h["new"], h["moved"]), (1, 0))
        self.assertEqual([v["subject"] for v in h["vanished"]], ["UBUNTU: SAUCE: twin: fix f1"])
        self.assertEqual(self.rows()["UBUNTU: SAUCE: twin: fix f8"]["disposition"], "undecided")
        # back to the Vendor tags for the rest of the test
        (self.mono / "kernel/sources.toml").write_text(
            (self.mono / "kernel/sources.toml").read_text().replace('tags = "Twin-*"', 'tags = "Vendor-*"'))
        self.g(vendor, "checkout", "-q", "vendor")
        self.kpm("harvest"); self.kpm("resolve")

        # export without a workbench must fail, not empty the series
        self.kpm("import")
        self.assertEqual(self.kpm("export")["patches"], 2)     # s and the re-applied tweak; d.c is in v1.0.3

        # A patch that removes a file that an earlier patch added. Its reverse
        # applies to the base, which has no such file. It must stay in the series.
        self.commit(vendor, {"t.c": "temporary\n"}, "UBUNTU: SAUCE: t: add t")
        self.g(vendor, "rm", "-q", "t.c")
        self.g(vendor, "commit", "-q", "-m", "UBUNTU: SAUCE: t: remove t again, in a different commit")
        self.g(vendor, "tag", "Vendor-3")
        self.kpm("fetch"); self.kpm("harvest"); self.kpm("resolve")
        self.kpm("import")
        self.assertEqual(self.kpm("export")["patches"], 4)
        again = self.kpm("import")                              # now from the patch files
        self.assertEqual((again["from_patch"], again["landed"], again["empty"]), (4, [], []))
        self.assertEqual(self.kpm("export")["patches"], 4)
        shutil.rmtree(self.tmp / "work")
        self.kpm("export", ok=(2,))
        self.assertEqual(len((line / "series").read_text().split()), 4)

        # a short or ambiguous id never selects rows
        with self.assertRaises(SystemExit):
            self.kpm("decide", "dropped", "a", "--reason", "x")


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
