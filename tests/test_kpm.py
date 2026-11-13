import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "kpm"))

from kpm import core, fingerprint as fp, kconfig, ledger, packaging, series  # noqa: E402

DIFF = """diff --git a/drivers/x.c b/drivers/x.c
index 111..222 100644
--- a/drivers/x.c
+++ b/drivers/x.c
@@ -10,7 +10,7 @@ static int f(void)
 	int a;
-	return old(a);
+	return new(a);
 }
"""


class Fingerprints(unittest.TestCase):
    def test_delta_ignores_context_and_line_numbers(self):
        drifted = DIFF.replace("@@ -10,7 +10,7 @@", "@@ -90,7 +94,7 @@").replace("\tint a;", "\tlong a;")
        self.assertEqual(fp.delta(DIFF)[0], fp.delta(drifted)[0])

    def test_delta_ignores_whitespace_but_not_content(self):
        respaced = DIFF.replace("\treturn new(a);", "    return  new(a);")
        self.assertEqual(fp.delta(DIFF)[0], fp.delta(respaced)[0])
        self.assertNotEqual(fp.delta(DIFF)[0], fp.delta(DIFF.replace("new(a)", "newer(a)"))[0])

    def test_delta_is_per_path(self):
        self.assertNotEqual(fp.delta(DIFF)[0], fp.delta(DIFF.replace("drivers/x.c", "drivers/y.c"))[0])
        self.assertEqual(fp.delta(DIFF)[1], ["drivers/x.c"])

    def test_removed_line_starting_with_dashes_is_content(self):
        d = DIFF.replace("-\treturn old(a);", "--- a comment")
        self.assertNotEqual(fp.delta(d)[0], fp.delta(DIFF)[0])

    def test_upstream_refs(self):
        body = ("x\n(cherry picked from commit 0123456789abcdef0123456789abcdef01234567)\n"
                "(backported from commit aaaaaaaaaaaa linux-next)\n"
                "[ Upstream commit bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ]\n"
                "commit cccccccccccccccccccccccccccccccccccccccc upstream.\n")
        self.assertEqual(fp.upstream_refs(body), [
            "0123456789abcdef0123456789abcdef01234567", "aaaaaaaaaaaa linux-next",
            "cccccccccccccccccccccccccccccccccccccccc", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"])

    def test_subject_key_strips_vendor_prefixes(self):
        self.assertEqual(fp.subject_key("NVIDIA: VR: SAUCE: sched/fair:  Fix X"), "sched/fair: fix x")
        self.assertEqual(fp.subject_key("UBUNTU: SAUCE: (no-up) linux/ena: Add Y"), "linux/ena: add y")


class Reverts(unittest.TestCase):
    REV = DIFF.replace("-\treturn old(a);\n+\treturn new(a);", "-\treturn new(a);\n+\treturn old(a);")

    def row(self, sha, subject, diff, **kw):
        d, _, inv = fp.delta(diff)
        return {"sha": sha * 40, "subject": subject, "disposition": "undecided",
                "delta_id": d, "inverse_id": inv, **kw}

    def test_inverse_id_matches_the_real_revert(self):
        self.assertEqual(fp.delta(self.REV)[0], fp.delta(DIFF)[2])
        self.assertNotEqual(fp.delta(DIFF)[0], fp.delta(DIFF)[2])

    def test_exact_pair_cancels_and_reapply_stands(self):
        rows = [self.row("a", "x: fix", DIFF), self.row("b", 'Revert "x: fix"', self.REV),
                self.row("c", "x: fix", DIFF)]
        self.assertEqual(core.pair_reverts(rows), (1, 0))
        self.assertEqual([r["disposition"] for r in rows], ["dropped", "dropped", "undecided"])
        self.assertIn("bbbbbbbbbbbb", rows[0]["reason"])

    def test_stale_sha_falls_back_to_subject_and_prefixed_revert(self):
        rows = [self.row("a", "UBUNTU: SAUCE: x: fix", DIFF),
                self.row("b", 'UBUNTU: SAUCE: Revert "UBUNTU: SAUCE: x: fix"', self.REV, reverts="f" * 12)]
        self.assertEqual(core.pair_reverts(rows), (1, 0))

    def test_inexact_revert_is_only_proposed(self):
        partial = self.REV.replace("old(a)", "older(a)")
        rows = [self.row("a", "x: fix", DIFF), self.row("b", 'Revert "x: fix"', partial)]
        self.assertEqual(core.pair_reverts(rows), (0, 1))
        self.assertEqual([r["disposition"] for r in rows], ["undecided", "undecided"])
        self.assertEqual(rows[1]["proposed"]["match"], "revert-subject")

    def test_revert_of_commit_outside_delta_is_a_real_patch(self):
        rows = [self.row("b", 'UBUNTU: SAUCE: Revert "netfilter: something upstream"', self.REV)]
        self.assertEqual(core.pair_reverts(rows), (0, 0))
        self.assertNotIn("proposed", rows[0])

    def test_decided_rows_are_not_touched(self):
        rows = [dict(self.row("a", "x: fix", DIFF), disposition="carry", patch="p"),
                self.row("b", 'Revert "x: fix"', self.REV)]
        self.assertEqual(core.pair_reverts(rows), (0, 0))


class Packaging(unittest.TestCase):
    KEEP = ["amd64", "arm64"]

    def test_filter_arches(self):
        text = ("Build-Depends:\n bindgen:native [amd64 arm64 armhf ppc64el riscv64 s390x],\n"
                " makedumpfile:native [amd64] <!stage1>,\n libfoo [s390x],\n rsync [!i386] <!stage1>,\n"
                " pahole [amd64 arm64 armhf] | dwarves (>= 1.21) [amd64 arm64 armhf] <!stage1>,\n"
                "Architecture: amd64 armhf arm64 ppc64el s390x\nArchitecture: all\n")
        out = packaging.filter_arches(text, self.KEEP)
        self.assertIn(" bindgen:native [amd64 arm64],", out)
        self.assertIn(" makedumpfile:native [amd64] <!stage1>,", out)
        self.assertNotIn("libfoo", out)
        self.assertIn(" rsync [!i386] <!stage1>,", out)
        self.assertIn(" pahole [amd64 arm64] | dwarves (>= 1.21) [amd64 arm64] <!stage1>,", out)
        self.assertIn("Architecture: amd64 arm64\n", out)
        self.assertIn("Architecture: all", out)
        for gone in ("armhf", "ppc64el", "riscv64", "s390x"):
            self.assertNotIn(gone, out)

    def test_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            mono, tree = Path(tmp) / "mono", Path(tmp) / "tree"
            (mono / "kernel").mkdir(parents=True)
            (mono / "kernel" / "identity.toml").write_text(
                'vendor = "acme"\narches = ["amd64", "arm64"]\nkver = "ubuntu"\nabi = 3\nupload = 4\n'
                'respin = 2\nmaintainer = "M <m@example.invalid>"\nsupported = "S"\ntarget = "T"\n'
                '[series]\nnoble = "24.04"\n[rules.noble]\ndo_lib_rust = "false"\n')
            m = tree / "debian.master"
            for d in ("control.d", "rules.d", "config"):
                (m / d).mkdir(parents=True)
            (m / "changelog").write_text("linux (7.3.0-11.12) stonking; urgency=medium\n")
            (m / "modprobe.d").mkdir()
            (m / "modprobe.d" / "common.conf").write_text("# base\noptions a b=1\n")
            (tree / "debian" / "rules.d").mkdir(parents=True)
            for rel, old, _new in packaging._RULE_EDITS:
                with open(tree / rel, "a") as f:
                    f.write(old)
            (m / "control.stub.in").write_text("Architecture: amd64 armhf arm64 ppc64el s390x\n")
            for stub in ("flavour-control.stub", "flavour-signed-control.stub"):
                (m / "control.d" / stub).write_text("x\n")
            for a in ("amd64", "arm64", "s390x"):
                (m / "rules.d" / f"{a}.mk").write_text("flavours\t= generic\ndo_tools_perf = true\n")
            res = packaging.render(mono, tree, "noble", "v7.3.4", modprobe={
                "azure": {"common.conf": "# base\noptions a b=1\n# why\nsoftdep x post: y\n"},
                "aws": {"common.conf": "# base\noptions a b=1\n"}})
            mp = (tree / "debian.acme" / "modprobe.d" / "common.conf").read_text()
            self.assertEqual(mp, "# base\noptions a b=1\n\n# --- from azure ---\n# why\nsoftdep x post: y\n")
            self.assertEqual(res["modprobe_from"], ["aws", "azure"])
            rules = (tree / "debian" / "rules").read_text()
            self.assertNotIn("do_tools_perf = false", rules)
            arch_mk = (tree / "debian" / "rules.d" / "2-binary-arch.mk").read_text()
            self.assertIn("CORESIGHT=1", arch_mk)
            self.assertNotIn("perfpkgdir", arch_mk)
            self.assertIn("$(DEB_SOURCE)-tools-$(abi_release)/bpftool", arch_mk)
            for a in ("amd64", "arm64"):
                mk = (tree / "debian.acme" / "rules.d" / f"{a}.mk").read_text()
                for tool in ("perf", "bpftool", "hyperv"):
                    self.assertIn(f"do_tools_{tool}\t= true", mk)
            dropped = packaging.merge_modprobe({"c": "a\n"}, {"nv": {"c": "a\n# why\nblacklist x\n"}},
                                               drop=["blacklist x"])
            self.assertEqual(dropped, {"c": "a\n"})
            with self.assertRaises(SystemExit):          # rules changed shape -> loud failure
                packaging.enable_tools(tree)
            d = tree / "debian.acme"
            self.assertEqual(res["version"], "7.3.0-3.4~24.04.2")
            self.assertEqual(sorted(p.name for p in (d / "rules.d").iterdir()), ["amd64.mk", "arm64.mk"])
            self.assertIn("flavours\t= acme acme-64k", (d / "rules.d" / "arm64.mk").read_text())
            self.assertIn("flavours\t= acme\n", (d / "rules.d" / "amd64.mk").read_text())
            self.assertIn("do_zfs\t= false", (d / "rules.d" / "amd64.mk").read_text())
            self.assertIn("do_lib_rust\t= false", (d / "rules.d" / "amd64.mk").read_text())
            ann = (d / "config" / "annotations").read_text()
            self.assertIn("# ARCH: amd64 arm64\n", ann)
            self.assertIn("# FLAVOUR: amd64-acme arm64-acme arm64-acme-64k\n", ann)
            self.assertIn("'arm64-acme-64k': 'arm64-generic-64k'", ann)
            self.assertTrue((d / "changelog").read_text().startswith("linux-acme (7.3.0-3.4~24.04.2) noble;"))
            self.assertEqual((tree / "debian" / "debian.env").read_text(), "DEBIAN=debian.acme\n")
            self.assertEqual((d / "control.d" / "vars.acme-64k").read_text().splitlines()[0], 'arch="arm64"')


class Policy(unittest.TestCase):
    RULES = [{"source": ["azure"], "_rx": __import__("re").compile("TDX"), "disposition": "deferred", "reason": "later"},
             {"source": ["ubuntu", "azure"], "_rx": None, "disposition": "carry", "reason": "all sauce"}]

    def test_first_match_wins_and_only_undecided(self):
        a = {"subject": "x86/tdx: TDX thing", "disposition": "undecided"}
        b = {"subject": "hv: other", "disposition": "undecided"}
        c = {"subject": "x", "disposition": "dropped", "reason": "by hand"}
        d = {"subject": "x", "disposition": "undecided"}
        self.assertTrue(core.apply_policy("azure", a, self.RULES))
        self.assertTrue(core.apply_policy("azure", b, self.RULES))
        self.assertFalse(core.apply_policy("azure", c, self.RULES))
        self.assertFalse(core.apply_policy("nvidia", d, self.RULES))
        self.assertEqual((a["disposition"], a["reason"]), ("deferred", "later"))
        self.assertEqual(b["disposition"], "carry")
        self.assertNotIn("reason", b)
        self.assertEqual(c["reason"], "by hand")
        e = {"subject": "x", "disposition": "undecided", "proposed": {"disposition": "duplicate"}}
        self.assertFalse(core.apply_policy("azure", e, self.RULES))          # proposals wait for a person
        self.assertEqual(e["disposition"], "undecided")

    def test_pending_and_slug(self):
        rows = [{"disposition": "carry"}, {"disposition": "carry", "patch": "p"}, {"disposition": "dropped"}]
        self.assertEqual(ledger.pending(rows), 1)
        self.assertEqual(series.slug("UBUNTU: SAUCE: (no-up) x86/tdx: Fix  the_thing!"), "x86-tdx-fix-the-thing")


class Kconfig(unittest.TestCase):
    def test_parse_and_effective(self):
        p = kconfig.parse("CONFIG_A      policy<{'amd64': 'y', 'arm64': 'm', 'arm64-aws-64k': '-'}>\n"
                          "CONFIG_A      note<'x'>\n# c\n")
        self.assertEqual(kconfig.effective(p["CONFIG_A"], "arm64", "aws"), "m")
        self.assertEqual(kconfig.effective(p["CONFIG_A"], "arm64", "aws-64k"), "-")
        self.assertEqual(kconfig.effective(p["CONFIG_A"], "amd64", "aws"), "y")
        self.assertEqual(kconfig.effective(None, "amd64", "aws"), "-")

    def test_annotation_lines_use_vendor_flavour_only_when_64k_differs(self):
        union = {"CONFIG_A": {"value": {"amd64": "m", "arm64": "m", "arm64-64k": "m"}, "wanted_by": ["azure"]},
                 "CONFIG_B": {"value": {"arm64": "y", "arm64-64k": "m"}, "wanted_by": ["aws", "gcp"]}}
        out = kconfig.annotation_lines(union, "acme", ["amd64", "arm64"])
        self.assertIn("policy<{'amd64': 'm', 'arm64': 'm'}>", out)
        self.assertIn("policy<{'arm64': 'y', 'arm64-acme-64k': 'm'}>", out)
        self.assertIn("note<'kpm union: wanted by aws gcp'>", out)
        local = {"CONFIG_A": {"arm64": "n", "note": "ours"}}
        out = kconfig.annotation_lines(kconfig.apply_local(union, local), "acme", ["amd64", "arm64"])
        self.assertIn("CONFIG_A                                        policy<{'arm64': 'n'}>", out)
        self.assertIn("note<'ours'>", out)


class Classify(unittest.TestCase):
    def c(self, subject, files):
        return fp.Commit("s", "a", "e", "d", subject, files=files)

    def test_paths_decide_not_subjects(self):
        self.assertEqual(core.classify(self.c("UBUNTU: [Config] x", ["debian.master/config/annotations"]), []), "config")
        self.assertEqual(core.classify(self.c("anything", ["debian/rules", "debian.aws/changelog"]), []), "packaging")
        self.assertEqual(core.classify(self.c("UBUNTU: [Config] x", ["drivers/Kconfig"]), []), "undecided")
        self.assertEqual(core.classify(self.c("UBUNTU: SAUCE: y", ["fs/a.c", "debian/rules"]), []), "undecided")


class Matching(unittest.TestCase):
    IDX = {"patch_id": {"p1": ["s1", "v7.3.1"]}, "delta_id": {"d1": ["s2", "v7.3.2"]},
           "upstream": {"a" * 40: ["s3", "v7.3.3"]}, "subject": {}}

    def test_strongest_evidence_wins(self):
        row = {"upstream": ["a" * 12], "patch_id": "p1", "delta_id": "d1"}
        self.assertEqual(core.match_base(row, self.IDX), ("s3", "v7.3.3", "upstream-ref"))
        self.assertEqual(core.match_base({"patch_id": "p1", "delta_id": "d1"}, self.IDX)[2], "patch-id")
        self.assertEqual(core.match_base({"patch_id": "zz", "delta_id": "d1"}, self.IDX)[2], "delta-id")
        self.assertIsNone(core.match_base({"patch_id": "zz", "delta_id": ""}, self.IDX))


class Ledger(unittest.TestCase):
    def test_round_trip_and_problems(self):
        rows = [{"sha": "a" * 40, "subject": "s", "disposition": "dropped"},
                {"sha": "b" * 40, "subject": "t", "disposition": "in-base", "base_sha": "c" * 40, "change": "C-00007"}]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.jsonl"
            ledger.write(p, rows)
            self.assertEqual(ledger.read(p), rows)
            self.assertEqual(ledger.next_change({"x": ledger.read(p)}), 8)
        self.assertEqual(len(ledger.problems("x", rows)), 1)   # dropped without a reason


class Safety(unittest.TestCase):
    def test_revert_prefers_the_commit_it_names(self):
        rows = [{"sha": "aaaa1111", "subject": "x: change", "disposition": "undecided", "delta_id": "D1"},
                {"sha": "bbbb2222", "subject": "x: change", "disposition": "undecided", "delta_id": "D2"},
                {"sha": "cccc3333", "subject": 'Revert "x: change"', "disposition": "undecided",
                 "reverts": "aaaa1111", "inverse_id": "D1"}]
        self.assertEqual(core.pair_reverts(rows), (1, 0))
        self.assertEqual([r["disposition"] for r in rows], ["dropped", "undecided", "dropped"])

    def test_orphaned_duplicate_is_reported(self):
        ledgers = {"a": [{"sha": "a" * 40, "disposition": "dropped", "reason": "x"}],
                   "b": [{"sha": "b" * 40, "disposition": "duplicate", "of": "a:" + "a" * 12},
                         {"sha": "c" * 40, "disposition": "duplicate", "of": "b:" + "b" * 12}]}
        self.assertEqual(len(ledger.orphaned_duplicates(ledgers)), 1)
        ledgers["a"][0]["disposition"] = "carry"
        self.assertEqual(ledger.orphaned_duplicates(ledgers), [])


class Cves(unittest.TestCase):
    def setUp(self):
        from kpm import cves
        self.cves = cves
        self.base = cves.Base((7, 3), 4)                  # a line on v7.3.4
        self.none = lambda sha: False

    def status(self, *pairs, carried=None, base=None):
        rows = [tuple(p.split(":")) for p in pairs]
        return self.cves.assess(rows, base or self.base, carried or self.none)["status"]

    def test_versions(self):
        b = self.base
        self.assertTrue(b.has("7.3") and b.has("7.3-rc5") and b.has("6.12") and b.has("7.3.4") and b.has("2.6.12"))
        self.assertFalse(b.has("7.4") or b.has("7.3.5") or b.has("7.4-rc1"))
        self.assertIsNone(b.has("6.12.50"))               # another series' release says nothing about ours

    def test_fixed_or_not_affected(self):
        self.assertEqual(self.status("5.0:a:7.1:f1", "5.0:a:6.12.9:f2"), "fixed-in-base")      # mainline fix before 7.3
        self.assertEqual(self.status("0:0:7.3.3:f1", "0:0:6.12.9:f2"), "fixed-in-base")        # fixed in 7.3.3
        self.assertEqual(self.status("7.4:a:7.5:f1"), "not-affected")                          # introduced later
        self.assertEqual(self.status("6.12.40:a:6.12.50:f1"), "not-affected")                  # another series only

    def test_open(self):
        self.assertEqual(self.status("5.0:a:7.3.9:f1", "5.0:a:7.4:f2"), "rebase")
        self.assertEqual(self.status("5.0:a:7.4:f2", "5.0:a:6.12.9:f3"), "backport")
        self.assertEqual(self.status("5.0:a:7.1.4:f1"), "backport")                            # fixed in another series only
        self.assertEqual(self.status("5.0:a:0:0"), "no-fix")
        self.assertEqual(self.status("7.3.2:a:7.3.9:f1"), "rebase")                            # a stable backport introduced it
        # the worst of several introducing commits decides
        self.assertEqual(self.status("5.0:a:7.1:f1", "6.0:b:7.4:f2"), "backport")

    def test_a_carried_fix_closes_it(self):
        have = lambda sha: sha == "f2"
        self.assertEqual(self.status("5.0:a:7.3.9:f1", "5.0:a:7.4:f2", carried=have), "fixed-by-patch")

    def test_severity_comes_from_the_record(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            rated, plain = Path(tmp) / "a.json", Path(tmp) / "b.json"
            rated.write_text(json.dumps({"containers": {"cna": {"title": "x: fix", "metrics": [
                {"cvssV3_1": {"baseScore": 9.8, "baseSeverity": "CRITICAL", "vectorString": "CVSS:3.1/AV:N"}}],
                "affected": [{"programFiles": ["net/x.c"]}]}}}))
            plain.write_text(json.dumps({"containers": {"cna": {"title": "y: fix"}}}))
            a, b = self.cves.record(rated), self.cves.record(plain)
        self.assertEqual((a["severity"], a["score"], a["files"]), ("critical", 9.8, ["net/x.c"]))
        self.assertEqual(b["severity"], "unscored")
        rows = [a, b, {"severity": "high"}]
        self.assertEqual(len(self.cves.at_least(rows, "critical")), 1)
        self.assertEqual(len(self.cves.at_least(rows, "high")), 2)
        self.assertEqual(len(self.cves.at_least(rows, "unscored")), 3)      # no rating is not "low": it is kept only here

    def test_our_assessment_needs_a_reason_and_evidence_in_the_base(self):
        class Cfg:
            base_ref, base_tag = "stable/v7.3.4", "v7.3.4"

        class FakeGit:
            def ok(self, *args):
                return args[2] == "good"          # merge-base --is-ancestor <sha> <base>

        with tempfile.TemporaryDirectory() as tmp:
            Cfg.line_dir = Path(tmp)
            (Path(tmp) / "cves.toml").write_text(
                '["CVE-1"]\nstatus = "not-affected"\nevidence = ["good"]\nreason = "code was removed"\n'
                '["CVE-2"]\nstatus = "not-affected"\nevidence = ["absent"]\nreason = "x"\n'
                '["CVE-3"]\nstatus = "not-affected"\n')
            good, problems = self.cves.assessments(Cfg, FakeGit())
            self.assertEqual(list(good), ["CVE-1"])
            self.assertEqual(len(problems), 2)
            good, problems = self.cves.assessments(Cfg, None)     # cannot check the evidence: do not trust it
            self.assertEqual(good, {})

    def test_ubuntu_base_has_no_release_number(self):
        ubuntu = self.cves.Base((7, 0), None)
        self.assertEqual(self.status("5.0:a:7.0.4:f1", base=ubuntu), "rebase")                 # not provable by version
        self.assertEqual(self.cves.assess([("5.0", "a", "7.0.4", "f1")], ubuntu, self.none,
                                          lambda sha: sha == "f1")["status"], "fixed-in-base")


class Updates(unittest.TestCase):
    def test_debian_compare(self):
        from kpm.updates import debian_compare as c
        self.assertEqual(c("6.17.0-42.42~24.04.1", "6.17.0-42.42~24.04.1"), 0)
        self.assertEqual(c("6.17.0-9.9~24.04.1", "6.17.0-42.42~24.04.1"), -1)
        self.assertEqual(c("7.0.0-43.43", "7.0.0-43.43~24.04.1"), 1)      # ~ sorts before nothing
        self.assertEqual(c("1:1.0-1", "2.0-1"), 1)
        self.assertEqual(c("1~bpo1", "1~1"), 1)       # a letter sorts after a digit ...
        self.assertEqual(c("a1", "aa"), -1)           # ... and a digit ends the non-digit part
        self.assertEqual(c("1:2:3-1", "1:2:3-1"), 0)  # only the first colon ends the epoch

    def test_ubuntu_package(self):
        from kpm.updates import ubuntu_package as u
        self.assertEqual(u("Ubuntu-hwe-7.0-7.0.0-43.43_24.04.1"), ("linux-hwe-7.0", "Ubuntu-hwe-7.0-"))
        self.assertEqual(u("Ubuntu-hwe-6.17-6.17.0-42.42"), ("linux-hwe-6.17", "Ubuntu-hwe-6.17-"))
        self.assertEqual(u("Ubuntu-7.3.0-11.12"), ("linux", "Ubuntu-"))
        self.assertEqual(u("v7.3.4"), ("", ""))

    def test_tag_order_and_exit_code(self):
        from kpm.updates import exit_code, natural_key
        self.assertEqual(sorted(["v7.3.10", "v7.3.9", "v7.3"], key=natural_key)[-1], "v7.3.10")
        self.assertEqual(exit_code([{"status": "current"}]), 0)
        self.assertEqual(exit_code([{"status": "current"}, {"status": "MOVED"}]), 10)
        self.assertEqual(exit_code([{"status": "STALE"}, {"status": "error: x"}]), 2)


if __name__ == "__main__":
    unittest.main()
