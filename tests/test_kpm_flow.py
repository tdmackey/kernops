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
        # A normal fetch never leaves a shallow repository: ancestry must be whole.
        self.assertEqual(self.g(self.tmp / "kernel.git", "rev-parse", "--is-shallow-repository"), "false")
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

        # The patch that landed was not the vendor's commit (its packaging part was
        # left out): the diff that landed is kept, and a later base is checked with it.
        kept = self.mono / "kernel/lines/t" / r["UBUNTU: SAUCE: d: add d"]["landed_patch"]
        self.assertIn("+d", kept.read_text())
        linux = self.tmp / "mirrors/linux"
        self.g(linux, "checkout", "-q", "linux-1.0.y")
        self.commit(linux, {"d.c": "d changed\n"}, "d: change d")
        self.g(linux, "tag", "v1.0.4")
        self.g(linux, "checkout", "-q", "master")
        self.kpm("fetch")
        rb = self.kpm("rebase", "v1.0.4", ok=(0, 3))
        self.assertEqual([(x["subject"], "as we adapted it" in x["why"]) for x in rb["invalidated"]],
                         [("UBUNTU: SAUCE: d: add d", True)])
        # Carried again, it is the kept diff that must come back, not the vendor's
        # commit. Here the base changed the file, so the import stops and says so.
        self.kpm("decide", "carry", "--source", "vendor", "--reason", "test: carry it again")
        imp = self.kpm("import", ok=(3,))
        self.assertIn("landed/", imp["conflict"]["hint"])
        self.assertIn("adapted by hand", imp["conflict"]["hint"])
        self.kpm("decide", "superseded", self.rows()["UBUNTU: SAUCE: d: add d"]["change"], "--by", "v1.0.4",
                 "--reason", "test: the base changed d.c", "--force")
        self.kpm("import")

        tree2 = self.tmp / "tree2"
        self.kpm("apply", str(tree2))
        self.assertEqual((tree2 / "b.c").read_text(), "b better\n")
        lines = cli.main(["lines"])                                            # plain listing still works
        self.assertEqual(lines, 0)

    def test_rebase_follows_a_revert_chain_that_starts_before_the_old_base(self):
        """v1.0.2 takes s.c. v1.0.4 reverts it (B) and reverts B (C): s.c is there.
        v1.0.5 reverts C (D): B holds again and s.c is gone. Only D is new in the second move."""
        linux = self.tmp / "mirrors" / "linux"
        self.g(linux, "checkout", "-q", "linux-1.0.y")
        self.g(linux, "revert", "--no-edit", "v1.0.2")             # B
        self.g(linux, "revert", "--no-edit", "HEAD")               # C
        self.g(linux, "tag", "v1.0.4")
        self.g(linux, "revert", "--no-edit", "HEAD")               # D
        self.g(linux, "tag", "v1.0.5")
        self.g(linux, "checkout", "-q", "master")
        self.kpm("fetch"); self.kpm("harvest"); self.kpm("resolve")
        self.kpm("import"); self.kpm("export")
        self.kpm("rebase", "v1.0.2")
        rb = self.kpm("rebase", "v1.0.4", ok=(0, 3))
        self.assertEqual(rb["invalidated"], [])
        self.assertEqual(self.rows()["UBUNTU: SAUCE: s: add s"]["disposition"], "in-base")
        rb = self.kpm("rebase", "v1.0.5", ok=(0, 3))
        self.assertEqual([x["subject"] for x in rb["invalidated"]], ["UBUNTU: SAUCE: s: add s"])
        self.assertEqual(self.rows()["UBUNTU: SAUCE: s: add s"]["disposition"], "undecided")

    def test_rebase_checks_in_base_rows_again(self):
        """v1.0.2 takes the vendor's s.c; v1.0.4 reverts it. After the second move the row must not say in-base."""
        linux = self.tmp / "mirrors" / "linux"
        self.g(linux, "checkout", "-q", "linux-1.0.y")
        self.g(linux, "revert", "--no-edit", "v1.0.2")
        self.g(linux, "tag", "v1.0.4")
        self.g(linux, "checkout", "-q", "master")
        self.kpm("fetch"); self.kpm("harvest"); self.kpm("resolve")
        self.kpm("import"); self.kpm("export")

        rb = self.kpm("rebase", "v1.0.2")
        self.assertEqual(rb["invalidated"], [])
        self.assertEqual(self.rows()["UBUNTU: SAUCE: s: add s"]["disposition"], "in-base")

        rb = self.kpm("rebase", "v1.0.4")
        self.assertEqual([x["subject"] for x in rb["invalidated"]], ["UBUNTU: SAUCE: s: add s"])
        row = self.rows()["UBUNTU: SAUCE: s: add s"]
        self.assertEqual(row["disposition"], "undecided")
        self.assertIn("reverts", row["proposed"]["note"])
        self.assertNotIn("base_sha", row)
        self.assertFalse(self.kpm("verify", "--strict", ok=(1,))["ok"])      # a person must decide
        # the row that rests on the upstream fix v1.0.1 still has stays in-base
        self.assertEqual(self.rows()["a: fix l2"]["disposition"], "in-base")

        self.kpm("decide", "carry", row["change"], "--reason", "test: the base reverted it")
        self.kpm("import")
        self.assertIn("0000-vendor/0001-s-add-s.patch", self.kpm("export")["per_source"] and
                      (self.mono / "kernel/lines/t/series").read_text())
        tree = self.tmp / "tree-reval"
        self.kpm("apply", str(tree))
        self.assertTrue((tree / "s.c").exists())
        self.assertTrue(self.kpm("verify", "--strict")["ok"])

    def test_rebase_does_not_drop_a_patch_that_we_changed(self):
        """We added a line to the vendor's s.c patch. v1.0.2 takes the vendor's original, not our line."""
        self.kpm("fetch"); self.kpm("harvest"); self.kpm("resolve")
        d = self.rows()["UBUNTU: SAUCE: d: add d"]
        self.kpm("decide", "dropped", d["change"], "--reason", "test: only s in this test")
        self.kpm("import")
        wt = self.tmp / "work"
        (wt / "s.c").write_text("sauce\nour safety check\n")
        self.g(wt, "commit", "-q", "-a", "--amend", "--no-edit")
        # A patch that we changed needs a note that says how. Without one, export
        # stops and writes nothing.
        series_file = self.mono / "kernel/lines/t/series"
        self.kpm("export", ok=(2,))
        self.assertFalse(series_file.exists() and series_file.read_text().strip())
        self.assertFalse(list((self.mono / "kernel/lines/t/patches").glob("*/*.patch")))   # no file written
        change = self.rows()["UBUNTU: SAUCE: s: add s"]["change"]
        self.assertEqual(self.kpm("export", "--conflicts", f"{change}=s.c: added our safety check")["adapted"], 1)
        row = self.rows()["UBUNTU: SAUCE: s: add s"]
        self.assertTrue(row["adapted"])
        self.assertIn("Conflicts:\n\ts.c: added our safety check",
                      (self.mono / "kernel/lines/t/patches" / row["patch"]).read_text())

        # A later amendment with no new explanation: the note in the ledger covers
        # the patch, and a rejected export would leave the stored file as it is.
        stored = (self.mono / "kernel/lines/t/patches" / row["patch"]).read_text()
        self.g(wt, "update-index", "--chmod=+x", "s.c")
        self.g(wt, "commit", "-q", "--amend", "--no-edit")
        self.kpm("export")
        after = (self.mono / "kernel/lines/t/patches" / row["patch"]).read_text()
        self.assertIn("new file mode 100755", after)
        self.assertNotEqual(stored, after)
        self.assertTrue(self.rows()["UBUNTU: SAUCE: s: add s"]["adapted"])

        rb = self.kpm("rebase", "v1.0.2", ok=(3,))                 # stops for a person
        self.assertEqual(rb["landed"], [])
        self.assertIn("adapted by hand", rb["conflict"]["hint"])
        row = self.rows()["UBUNTU: SAUCE: s: add s"]
        self.assertEqual(row["disposition"], "carry")              # not in-base
        self.assertIn("our safety check",
                      (self.mono / "kernel/lines/t/patches" / row["patch"]).read_text())

    def test_rebase_does_not_drop_a_patch_of_which_we_changed_the_file_mode(self):
        """We made the vendor's s.c executable. v1.0.2 has s.c with the same content, not executable."""
        self.kpm("fetch"); self.kpm("harvest"); self.kpm("resolve")
        d = self.rows()["UBUNTU: SAUCE: d: add d"]
        self.kpm("decide", "dropped", d["change"], "--reason", "test: only s in this test")
        self.kpm("import")
        wt = self.tmp / "work"
        self.g(wt, "update-index", "--chmod=+x", "s.c")
        self.g(wt, "commit", "-q", "--amend", "--no-edit")
        change = self.rows()["UBUNTU: SAUCE: s: add s"]["change"]
        self.assertEqual(self.kpm("export", "--conflicts", f"{change}=s.c: made executable")["adapted"], 1)

        rb = self.kpm("rebase", "v1.0.2", ok=(3,))                 # stops for a person
        self.assertEqual(rb["landed"], [])
        row = self.rows()["UBUNTU: SAUCE: s: add s"]
        self.assertEqual(row["disposition"], "carry")              # not in-base
        self.assertIn("new file mode 100755", (self.mono / "kernel/lines/t/patches" / row["patch"]).read_text())

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

        # A vendor commit that says it is the base's "s: add s" commit. That commit is
        # in the base's history, and reverted there.
        added_s = self.g(linux, "rev-parse", "v1.0.2")
        self.g(vendor, "checkout", "-q", "vendor")
        self.commit(vendor, {"s2.c": "sauce two\n"}, f"s: add s, again\n\n(cherry picked from commit {added_s})")
        self.g(vendor, "tag", "-f", "Vendor-1")

        self.kpm("fetch")
        self.kpm("harvest")
        self.kpm("resolve")
        r = self.rows()
        again = r["s: add s, again"]
        self.assertEqual(again["disposition"], "undecided")                          # in the history, not in the tree
        self.assertIn("reverts it there", again["proposed"]["note"])
        self.kpm("decide", "carry", again["change"], "--reason", "test: the base reverted it")
        self.assertEqual(r["UBUNTU: SAUCE: s: add s"]["disposition"], "carry")      # not in-base
        self.assertEqual(r["UBUNTU: SAUCE: c: tweak"]["disposition"], "dropped")     # reverted in the vendor tree

        # The vendor applies the reverted patch again. It is a new commit with the
        # old content, and must not inherit "dropped".
        self.commit(vendor, {"a.c": "l1\nl2 fixed\nl3 tweak\n"}, "UBUNTU: SAUCE: c: tweak")
        self.g(vendor, "tag", "Vendor-2")
        self.kpm("fetch")
        h = self.kpm("harvest")[0]
        self.assertEqual((h["commits"], h["new"], h["moved"]), (8, 1, 0))
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
        # A commit named Revert "..." that makes the opposite change of "fix f8", but
        # in f3: the tree keeps both changes, so neither row may be cancelled.
        both = body.replace("f8() {\n\tx = 0;", "f8() {\n\tx = 1;").replace("f3() {\n\tx = 0;", "f3() {\n\tx = 1;")
        self.g(vendor, "reset", "-q", "--hard", "Twin-1~1")
        self.commit(vendor, {"twin.c": body.replace("f3() {\n\tx = 0;", "f3() {\n\tx = 1;")}, "UBUNTU: SAUCE: twin: set f3")
        self.commit(vendor, {"twin.c": both}, "UBUNTU: SAUCE: twin: fix f8")
        self.commit(vendor, {"twin.c": body.replace("f8() {\n\tx = 0;", "f8() {\n\tx = 1;")},
                    'Revert "UBUNTU: SAUCE: twin: fix f8"')                 # really undoes "set f3"
        self.g(vendor, "tag", "Twin-3")
        self.kpm("fetch"); self.kpm("harvest"); self.kpm("resolve")
        r = self.rows()
        self.assertEqual(r["UBUNTU: SAUCE: twin: fix f8"]["disposition"], "carry")     # not cancelled; policy carries it
        fake = r['Revert "UBUNTU: SAUCE: twin: fix f8"']
        self.assertEqual((fake["disposition"], fake["proposed"]["match"]), ("undecided", "revert-subject"))
        self.assertEqual(fake["inverse_id"], r["UBUNTU: SAUCE: twin: fix f8"]["delta_id"])    # the old rule would cancel them

        # back to the Vendor tags for the rest of the test
        (self.mono / "kernel/sources.toml").write_text(
            (self.mono / "kernel/sources.toml").read_text().replace('tags = "Twin-*"', 'tags = "Vendor-*"'))
        self.g(vendor, "checkout", "-q", "vendor")
        self.kpm("harvest"); self.kpm("resolve")
        # The rows of the Vendor tree are new again after that detour: decide "s again" once more.
        again = self.rows()["s: add s, again"]
        self.assertEqual(again["disposition"], "undecided")
        self.kpm("decide", "carry", again["change"], "--reason", "test: the base reverted it")

        # export without a workbench must fail, not empty the series
        self.kpm("import")
        self.assertEqual(self.kpm("export")["patches"], 3)     # s, "s again" and the re-applied tweak; d.c is in v1.0.3

        # A patch that removes a file that an earlier patch added. Its reverse
        # applies to the base, which has no such file. It must stay in the series.
        self.commit(vendor, {"t.c": "temporary\n"}, "UBUNTU: SAUCE: t: add t")
        self.g(vendor, "rm", "-q", "t.c")
        self.g(vendor, "commit", "-q", "-m", "UBUNTU: SAUCE: t: remove t again, in a different commit")
        self.g(vendor, "tag", "Vendor-3")
        self.kpm("fetch"); self.kpm("harvest"); self.kpm("resolve")
        self.kpm("import")
        self.assertEqual(self.kpm("export")["patches"], 5)
        again = self.kpm("import")                              # now from the patch files
        self.assertEqual((again["from_patch"], again["landed"], again["empty"]), (5, [], []))
        self.assertEqual(self.kpm("export")["patches"], 5)

        # Two different fixes with one title. The base has "a: fix l2"; this one
        # has the same title and changes another part of the same file. It must be added.
        self.g(linux, "checkout", "-q", "master")
        twin = self.commit(linux, {"a.c": "l1\nl2 fixed\nl3 fixed too\n"}, "a: fix l2")
        self.kpm("fetch", "mainline")
        res = self.kpm("add", twin)[0]
        self.assertEqual(res["result"], "added")
        self.assertIn("same title and different content", res["note"])
        shutil.rmtree(self.tmp / "work")
        self.kpm("export", ok=(2,))
        self.assertEqual(len((line / "series").read_text().split()), 5)

        # a short or ambiguous id never selects rows
        with self.assertRaises(SystemExit):
            self.kpm("decide", "dropped", "a", "--reason", "x")

    def test_a_carried_revert_of_a_base_revert_puts_the_fix_back(self):
        from kpm import config, cves
        from kpm.git import Git
        linux, line = self.tmp / "mirrors/linux", self.mono / "kernel/lines/t"
        fix = self.g(linux, "rev-parse", "v1.0.1")
        self.g(linux, "checkout", "-q", "linux-1.0.y")
        self.g(linux, "revert", "--no-edit", fix)                 # the base reverts the fix
        self.g(linux, "tag", "v1.0.4")
        self.g(linux, "revert", "--no-edit", "v1.0.4")            # a later commit puts it back
        again = self.g(linux, "rev-parse", "HEAD")
        self.g(linux, "tag", "v1.0.5")
        self.g(linux, "checkout", "-q", "master")
        (line / "line.toml").write_text((line / "line.toml").read_text().replace('"v1.0.1"', '"v1.0.4"'))
        self.kpm("fetch"); self.kpm("fetch", "mainline")
        vulns = self.tmp / "vulns/cve/published/2026"
        vulns.mkdir(parents=True)
        (vulns / "CVE-2026-0001.dyad").write_text(f"0:0:1.0.1:{fix}\n")
        # A second record names the fix by its upstream commit. The base reverts the
        # stable backport of it, so that name is undone too.
        (vulns / "CVE-2026-0002.dyad").write_text(f"0:0:1.0.1:{self.m1}\n")

        def counts():
            cfg = config.load()
            return cves.check(cfg, Git(cfg.repo), repo=self.tmp / "vulns")["counts"]
        self.assertEqual(counts()["fixed-in-base"], 0)             # reverted in the base
        self.assertEqual(self.kpm("add", again)[0]["result"], "added")
        self.assertEqual(counts()["fixed-in-base"], 2)             # our patch reverts that revert

    def test_a_fix_reverted_before_the_series_starts_is_not_in_the_base(self):
        from kpm import config, cves
        from kpm.git import Git
        linux, k = self.tmp / "mirrors/linux", self.mono / "kernel"
        fix = self.commit(linux, {"f.c": "fixed\n"}, "f: fix f")
        self.g(linux, "tag", "v1.1")                              # the fix is released in v1.1
        kept = self.commit(linux, {"g.c": "fixed\n"}, "g: fix g")
        self.g(linux, "revert", "--no-edit", fix)                 # and reverted before v1.2
        self.g(linux, "tag", "v1.2")
        self.g(linux, "checkout", "-q", "-b", "linux-1.2.y", "v1.2")
        self.commit(linux, {"h.c": "h\n"}, "h: add h")
        self.g(linux, "tag", "v1.2.1")
        self.g(linux, "checkout", "-q", "master")
        (k / "sources.toml").write_text((k / "sources.toml").read_text().replace('"v1.0*"', '"v1.*"'))
        (k / "lines/t/line.toml").write_text(
            (k / "lines/t/line.toml").read_text().replace('"v1.0.1"', '"v1.2.1"').replace('"v1.0"', '"v1.2"'))
        self.kpm("fetch"); self.kpm("fetch", "mainline")
        vulns = self.tmp / "vulns/cve/published/2026"
        vulns.mkdir(parents=True)
        (vulns / "CVE-2026-0001.dyad").write_text(f"0:0:1.1:{fix}\n")
        (vulns / "CVE-2026-0002.dyad").write_text(f"0:0:1.2:{kept}\n")
        cfg = config.load()
        res = cves.check(cfg, Git(cfg.repo), repo=self.tmp / "vulns")
        self.assertEqual(res["counts"]["fixed-in-base"], 1)        # only the fix that is still there
        self.assertEqual([(r["cve"], r["status"]) for r in res["open"]], [("CVE-2026-0001", "backport")])

    def test_a_fix_applied_again_and_reverted_again_by_our_patches_is_gone(self):
        from kpm import config, cves
        from kpm.git import Git
        linux, line = self.tmp / "mirrors/linux", self.mono / "kernel/lines/t"
        fix = self.g(linux, "rev-parse", "v1.0.1")
        self.g(linux, "checkout", "-q", "linux-1.0.y")
        self.g(linux, "revert", "--no-edit", fix)                 # the base reverts the fix
        self.g(linux, "tag", "v1.0.4")
        self.g(linux, "checkout", "-q", "master")
        self.g(linux, "revert", "--no-edit", self.m1)             # mainline reverts it too, later
        rev = self.g(linux, "rev-parse", "HEAD")
        (line / "line.toml").write_text((line / "line.toml").read_text().replace('"v1.0.1"', '"v1.0.4"'))
        self.kpm("fetch"); self.kpm("fetch", "mainline")
        vulns = self.tmp / "vulns/cve/published/2026"
        vulns.mkdir(parents=True)
        (vulns / "CVE-2026-0001.dyad").write_text(f"0:0:1.0.1:{self.m1}\n")

        def counts():
            cfg = config.load()
            return cves.check(cfg, Git(cfg.repo), repo=self.tmp / "vulns")["counts"]
        self.assertEqual(self.kpm("add", self.m1)[0]["result"], "added")
        self.assertEqual(counts()["fixed-by-patch"], 1)            # we carry the fix again
        self.assertEqual(self.kpm("add", rev)[0]["result"], "added")
        c = counts()
        self.assertEqual((c["fixed-by-patch"], c["fixed-in-base"]), (0, 0))     # and then we revert it

    def test_a_carried_revert_at_the_end_of_a_base_chain_removes_the_fix(self):
        from kpm import config, cves
        from kpm.git import Git
        linux, line = self.tmp / "mirrors/linux", self.mono / "kernel/lines/t"
        fix = self.g(linux, "rev-parse", "v1.0.1")
        self.g(linux, "checkout", "-q", "linux-1.0.y")
        self.g(linux, "revert", "--no-edit", fix)                 # B: the fix is gone
        self.g(linux, "tag", "v1.0.4")
        self.g(linux, "revert", "--no-edit", "v1.0.4")            # C: the fix is back
        self.g(linux, "tag", "v1.0.5")
        self.g(linux, "revert", "--no-edit", "v1.0.5")            # D, not in our base
        d = self.g(linux, "rev-parse", "HEAD")
        self.g(linux, "tag", "v1.0.6")
        self.g(linux, "checkout", "-q", "master")
        (line / "line.toml").write_text((line / "line.toml").read_text().replace('"v1.0.1"', '"v1.0.5"'))
        self.kpm("fetch"); self.kpm("fetch", "mainline")
        vulns = self.tmp / "vulns/cve/published/2026"
        vulns.mkdir(parents=True)
        (vulns / "CVE-2026-0001.dyad").write_text(f"0:0:1.0.1:{fix}\n")

        def counts():
            cfg = config.load()
            return cves.check(cfg, Git(cfg.repo), repo=self.tmp / "vulns")["counts"]
        self.assertEqual(counts()["fixed-in-base"], 1)             # the base ends with the fix in place
        self.assertEqual(self.kpm("add", d)[0]["result"], "added")
        self.assertEqual(counts()["fixed-in-base"], 0)             # our patch undoes C: B holds, the fix is gone

    def test_a_rewritten_vendor_commit_with_other_whitespace_is_decided_again(self):
        """The vendor rebases. One commit comes back with other indentation: the same
        patch-id, not the same change. Its decision does not carry over; the others do."""
        vendor = self.tmp / "mirrors/vendor"
        self.kpm("fetch"); self.kpm("harvest"); self.kpm("resolve")
        self.assertEqual(self.rows()["UBUNTU: SAUCE: s: add s"]["disposition"], "carry")
        self.g(vendor, "checkout", "-q", "-B", "vendor", "v1.0")
        self.commit(vendor, {"s.c": "\tsauce\n"}, "UBUNTU: SAUCE: s: add s")
        self.g(vendor, "cherry-pick", "Vendor-1~5..Vendor-1")
        self.g(vendor, "tag", "Vendor-2")
        self.kpm("fetch")
        self.assertEqual(self.kpm("harvest")[0]["moved"], 6)
        r = self.rows()
        self.assertEqual((r["UBUNTU: SAUCE: s: add s"]["disposition"], r["UBUNTU: SAUCE: s: add s"]["proposed"]["match"]),
                         ("undecided", "rewritten"))
        self.assertEqual(r["UBUNTU: SAUCE: d: add d"]["disposition"], "carry")      # the same to the character
        self.assertEqual(r["a: fix l2"]["disposition"], "in-base")

    def test_a_change_moved_to_a_twin_function_is_seen(self):
        """a() and b() have the same lines. The vendor changes a(); we put the change in b().
        The changed lines and the lines next to them are the same: only the tree tells."""
        from kpm import core, fingerprint as fp
        from kpm.git import Git
        repo = self.tmp / "twin"
        repo.mkdir()
        self.g(repo, "init", "-q", "-b", "main")
        body = "int a(void)\n{\n\treturn 0;\n}\n\nint b(void)\n{\n\treturn 0;\n}\n"
        base = self.commit(repo, {"f.c": body, "other.c": "x\n"}, "base")
        original = self.commit(repo, {"f.c": body.replace("return 0", "return 1", 1)}, "a: return 1")
        self.g(repo, "checkout", "-q", "-b", "work", base)
        self.commit(repo, {"other.c": "y\n"}, "an earlier patch of the series")
        same = self.commit(repo, {"f.c": body.replace("return 0", "return 1", 1)}, "a: return 1")
        self.g(repo, "reset", "-q", "--hard", "HEAD^")
        moved = self.commit(repo, {"f.c": "return 1".join(body.rsplit("return 0", 1))}, "a: return 1")
        git = Git(repo)
        show = lambda c: self.g(repo, "show", "--format=", "--no-renames", c)
        self.assertEqual(fp.places(show(original)), fp.places(show(moved)))     # the cheap check cannot tell
        self.assertFalse(core.not_what_git_makes(git, same, original))
        self.assertTrue(core.not_what_git_makes(git, moved, original))
        # The same check settles a patch-id match: a commit that changes b() is not
        # "in the base" because the base has a commit that changes a().
        self.assertTrue(core.same_change(git, original, same))
        self.assertFalse(core.same_change(git, original, moved))
        # A delta-id match has no more than the changed lines, with or without the
        # same title: it needs the same confirmation.
        self.assertEqual(fp.delta(show(original))[0], fp.delta(show(moved))[0])
        self.assertTrue(core.delta_confirmed(git, original, same, same))
        self.assertFalse(core.delta_confirmed(git, original, moved, moved))
        # Import asks if a patch is in a tree already. The reverse of the patch
        # for b() applies to a tree that has the change in a() only: that is not
        # proof. The patch must come back at the place it was taken from.
        patch = lambda c: subprocess.run(["git", "-C", str(repo), "format-patch", "-1", "--stdout", c],
                                         capture_output=True, check=True).stdout
        self.assertEqual(subprocess.run(["git", "-C", str(repo), "apply", "--check", "--reverse", "-"],
                                        input=patch(moved), capture_output=True).returncode, 0,
                         "the reverse of the b() patch applies to the tree that has a() changed")
        self.g(repo, "checkout", "-q", same)
        self.assertFalse(core.diff_in(git, patch(moved), same))             # a() is changed there, not b()
        self.assertTrue(core.diff_in(git, patch(moved), moved))
        self.assertTrue(core.diff_in(git, patch(original), same))
        self.assertTrue(core.pick_changes(git, moved, same))                # a pick of the b() commit changes that tree
        self.assertFalse(core.pick_changes(git, original, same))

    def test_a_normal_fetch_completes_a_shallow_repository(self):
        repo = self.tmp / "kernel.git"
        self.kpm("fetch", "--base-only")                       # CI: one commit, shallow
        self.assertEqual(self.g(repo, "rev-parse", "--is-shallow-repository"), "true")
        self.kpm("fetch")
        self.assertEqual(self.g(repo, "rev-parse", "--is-shallow-repository"), "false")
        self.assertEqual(self.g(repo, "rev-list", "--count", "stable/v1.0.1"), "2")   # v1.0 and the fix

    def test_packaging_kind_selects_the_build_path(self):
        from kpm import config
        self.assertEqual(config.load().packaging, "ours")       # a stable line has no packaging in its base
        line = self.mono / "kernel/lines/t/line.toml"
        line.write_text(line.read_text() + 'packaging = "base"\n')
        with self.assertRaises(SystemExit):
            config.load()

    def test_a_revert_of_a_carried_patch_can_be_added(self):
        # m2 adds b.c. Its revert removes b.c, and the base has no b.c: the revert
        # must not be taken for "already in the base".
        linux = self.tmp / "mirrors/linux"
        self.g(linux, "revert", "--no-edit", self.m4)
        self.g(linux, "revert", "--no-edit", self.m2)
        rev = self.g(linux, "rev-parse", "HEAD")
        self.kpm("fetch"); self.kpm("fetch", "mainline")
        self.assertEqual(self.kpm("add", self.m2)[0]["result"], "added")
        self.assertEqual(self.kpm("add", rev)[0]["result"], "added")
        self.assertTrue(self.kpm("add", self.m1)[0]["result"].startswith("already in base"))   # unchanged

    def test_a_revert_of_an_adapted_workbench_commit_can_be_added(self):
        # m2 adds b.c, and we change the patch by hand. A revert made on the
        # workbench names the workbench commit and is not the inverse of m2.
        self.kpm("fetch"); self.kpm("fetch", "mainline"); self.kpm("harvest"); self.kpm("resolve")
        self.assertEqual(self.kpm("add", self.m2)[0]["result"], "added")
        self.kpm("import")
        wt = self.tmp / "work"
        (wt / "b.c").write_text("b\nour extra line\n")
        self.g(wt, "commit", "-q", "-a", "--amend", "--no-edit")
        change = ledger.read(self.mono / "kernel/lines/t/ledger/local.jsonl")[-1]["change"]
        self.kpm("export", "--conflicts", f"{change}=b.c: an extra line of ours")
        self.g(wt, "revert", "--no-edit", "HEAD")
        rev = self.g(wt, "rev-parse", "HEAD")
        self.g(wt, "reset", "-q", "--hard", "HEAD^")
        self.assertEqual(self.kpm("add", rev)[0]["result"], "added")

    def test_an_added_revert_keeps_what_it_reverts(self):
        # kpm cves reads `reverts` to see that the series removes a fix.
        linux = self.tmp / "mirrors/linux"
        self.g(linux, "revert", "--no-edit", self.m1)
        rev = self.g(linux, "rev-parse", "HEAD")
        self.kpm("fetch"); self.kpm("fetch", "mainline")
        self.assertEqual(self.kpm("add", rev)[0]["result"], "added")
        row = ledger.read(self.mono / "kernel/lines/t/ledger/local.jsonl")[-1]
        self.assertEqual((row["sha"], row["reverts"]), (rev, self.m1))
        self.assertTrue(row["inverse_patch_id"])


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
