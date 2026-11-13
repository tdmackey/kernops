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


class Bands(unittest.TestCase):
    """A band never runs into the next one: the numbers get more digits."""
    BANDS = {"bos": 2500, "n70": 2700, "gcp": 3500, "local": 6000}

    def test_room_enough_keeps_four_digits(self):
        self.assertEqual(series.band_scale(self.BANDS, {"bos": 12, "n70": 1, "gcp": 26, "local": 1}), 1)
        self.assertEqual(series.band_scale(self.BANDS, {"bos": 199}), 1)

    def test_a_full_band_gets_one_more_digit(self):
        self.assertEqual(series.band_scale(self.BANDS, {"bos": 200}), 10)       # 2700 would be used twice
        self.assertEqual(series.band_scale(self.BANDS, {"bos": 563, "n70": 1}), 10)
        self.assertEqual(series.band_scale(self.BANDS, {"bos": 2500}), 100)
        self.assertEqual(series.band_scale(self.BANDS, {"local": 5000}), 1)     # the last band has no neighbour

    def test_two_sources_cannot_share_a_band(self):
        with self.assertRaises(Exception):
            series.band_scale({"a": 2000, "b": 2000}, {"a": 1, "b": 1})


class Places(unittest.TestCase):
    """delta_id is the changed lines; places also has the lines around them."""

    def diff(self, at, before, after):
        return (f"diff --git a/f.c b/f.c\n--- a/f.c\n+++ b/f.c\n@@ -{at},3 +{at},3 @@\n"
                f" {before}\n-\treturn 0;\n+\treturn 1;\n {after}\n")

    def test_the_same_change_in_another_function_is_another_place(self):
        in_a, in_b = self.diff(10, "int a(void) {", "}"), self.diff(20, "int b(void) {", "}")
        self.assertEqual(fp.delta(in_a)[0], fp.delta(in_b)[0])
        self.assertNotEqual(fp.places(in_a), fp.places(in_b))

    def test_indentation_is_part_of_the_place(self):
        head = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,3 +1,3 @@\n def f(user):\n"
        plain = head + "-    return False\n+    return True\n \n"
        inside = head + "-    return False\n+        return True\n \n"      # moved into a block above
        self.assertEqual(fp.delta(plain)[0], fp.delta(inside)[0])
        self.assertNotEqual(fp.places(plain), fp.places(inside))

    def test_a_mode_change_is_a_change(self):
        body = "--- a/run.sh\n+++ b/run.sh\n@@ -1,2 +1,2 @@\n #!/bin/sh\n-echo a\n+echo b\n"
        plain = "diff --git a/run.sh b/run.sh\n" + body
        exe = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n" + body
        self.assertEqual(fp.delta(plain)[0], fp.delta(exe)[0])
        self.assertEqual(fp.modes(plain), [])
        self.assertEqual(fp.modes(exe), ["run.sh new mode 100755", "run.sh old mode 100644"])
        self.assertNotEqual(fp.places(plain), fp.places(exe))

    def test_binary_files_are_named_so_that_their_content_can_be_compared(self):
        a = ("diff --git a/fw.bin b/fw.bin\nindex 1111111..2222222 100644\nGIT binary patch\nliteral 4\nLc${x}\n\n"
             "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1 +1 @@\n-x\n+y\n")
        b = a.replace("2222222", "3333333").replace("Lc${x}", "Lc${y}")          # other bytes in the blob
        self.assertEqual(fp.binary_files(a), ["fw.bin"])
        self.assertEqual(fp.delta(a)[0], fp.delta(b)[0])                          # delta_id cannot tell them apart
        self.assertEqual(fp.binary_files("diff --git a/x b/x\nBinary files a/x and b/x differ\n"), ["x"])

    def test_a_change_that_only_moved_down_the_file_is_the_same_place(self):
        self.assertEqual(fp.places(self.diff(10, "int a(void) {", "}")), fp.places(self.diff(40, "int a(void) {", "}")))


class Reverts(unittest.TestCase):
    REV = DIFF.replace("-\treturn old(a);\n+\treturn new(a);", "-\treturn new(a);\n+\treturn old(a);")

    def row(self, sha, subject, diff, **kw):
        # In these rows the patch-ids follow the delta ids: the change is at one place.
        d, _, inv = fp.delta(diff)
        return {"sha": sha * 40, "subject": subject, "disposition": "undecided",
                "delta_id": d, "inverse_id": inv, "patch_id": "P" + d, "inverse_patch_id": "P" + inv, **kw}

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

    def test_same_lines_at_another_place_is_only_proposed(self):
        # "Revert" makes the opposite change, but in a different function: the
        # delta ids agree, the patch-ids (which cover the context) do not.
        rows = [self.row("a", "x: fix", DIFF),
                self.row("b", 'Revert "x: fix"', self.REV, inverse_patch_id="P-of-another-place")]
        self.assertEqual(rows[1]["inverse_id"], rows[0]["delta_id"])
        self.assertEqual(core.pair_reverts(rows), (0, 1))
        self.assertEqual([r["disposition"] for r in rows], ["undecided", "undecided"])
        self.assertIn("not provably at the same place", rows[1]["proposed"]["note"])
        # a row from an old ledger, with no reversed patch-id, is not cancelled either
        rows = [self.row("a", "x: fix", DIFF), self.row("b", 'Revert "x: fix"', self.REV, inverse_patch_id="")]
        self.assertEqual(core.pair_reverts(rows), (0, 1))

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
            self.assertEqual(res["version"], "7.3.0-3.4~24.04.2+7.3.4")      # the base is part of the version
            self.assertEqual(sorted(p.name for p in (d / "rules.d").iterdir()), ["amd64.mk", "arm64.mk"])
            self.assertIn("flavours\t= acme acme-64k", (d / "rules.d" / "arm64.mk").read_text())
            self.assertIn("flavours\t= acme\n", (d / "rules.d" / "amd64.mk").read_text())
            self.assertIn("do_zfs\t= false", (d / "rules.d" / "amd64.mk").read_text())
            self.assertIn("do_lib_rust\t= false", (d / "rules.d" / "amd64.mk").read_text())
            ann = (d / "config" / "annotations").read_text()
            self.assertIn("# ARCH: amd64 arm64\n", ann)
            self.assertIn("# FLAVOUR: amd64-acme arm64-acme arm64-acme-64k\n", ann)
            self.assertIn("'arm64-acme-64k': 'arm64-generic-64k'", ann)
            self.assertTrue((d / "changelog").read_text().startswith("linux-acme (7.3.0-3.4~24.04.2+7.3.4) noble;"))
            self.assertEqual((tree / "debian" / "debian.env").read_text(), "DEBIAN=debian.acme\n")
            self.assertEqual((d / "control.d" / "vars.acme-64k").read_text().splitlines()[0], 'arch="arm64"')


class Conflicts(unittest.TestCase):
    PATCH = ("From 0000 Mon Sep 17 00:00:00 2001\nFrom: A <a@example.invalid>\nSubject: [PATCH] x: change\n\n"
             "Body.\n\nSigned-off-by: A <a@example.invalid>\nLedger-Change: C-00001\nLedger-Origin: v@abc\n"
             "---\n x.c | 2 +-\n\ndiff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-a\n+b\n")

    def test_note_goes_before_our_trailers_and_only_once(self):
        out = series.with_conflicts(self.PATCH, "x.c: the base has a new argument; pass it.")
        head = out.split("\n---\n")[0]
        self.assertIn("Signed-off-by: A <a@example.invalid>\n\nConflicts:\n\tx.c: the base has a new argument; pass it.\n\n"
                      "Ledger-Change: C-00001\nLedger-Origin: v@abc", head)
        self.assertEqual(out.split("\n---\n", 1)[1], self.PATCH.split("\n---\n", 1)[1])     # the diff is untouched
        self.assertEqual(series.with_conflicts(out, "something else"), out)
        self.assertEqual(series.with_conflicts(self.PATCH, ""), self.PATCH)

    def test_what_a_row_says_we_changed(self):
        self.assertEqual(series.conflicts_note({}), "")
        self.assertEqual(series.conflicts_note({"conflicts": " a.c: adjusted "}), "a.c: adjusted")
        both = series.conflicts_note({"conflicts": "a.c: adjusted", "touches_packaging": True})
        self.assertTrue(both.startswith("a.c: adjusted\n") and "debian*/" in both)

    def test_a_carry_decision_needs_a_reason(self):
        self.assertTrue(ledger.problems("s", [{"sha": "a" * 40, "disposition": "carry"}]))
        self.assertEqual(ledger.problems("s", [{"sha": "a" * 40, "disposition": "carry", "reason": "needed"}]), [])


class HookDirs(unittest.TestCase):
    def test_package_scripts_use_one_hook_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "debian" / "templates"
            t.mkdir(parents=True)
            (t / "image.preinst.in").write_text(
                'if [ -d /etc/kernel/preinst.d ] || [ -d /usr/share/kernel/preinst.d ]; then\n'
                '    DEB_MAINT_PARAMS="$*" run-parts --report --exit-on-error --arg=$version \\\n'
                '        --arg=$image_path /etc/kernel/preinst.d /usr/share/kernel/preinst.d\n'
                'fi\n')
            (t / "other.in").write_text("nothing to change\n")
            self.assertEqual(packaging.single_hook_dir(Path(tmp)), 1)
            out = (t / "image.preinst.in").read_text()
            self.assertNotIn("/usr/share/kernel", out)
            self.assertIn("if [ -d /etc/kernel/preinst.d ]; then", out)
            self.assertIn("--arg=$image_path /etc/kernel/preinst.d\n", out)
            self.assertEqual(packaging.single_hook_dir(Path(tmp)), 0)     # nothing left to change


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
        self.assertEqual(b["reason"], "all sauce")                 # a carry decision keeps its reason too
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


class UbuntuBasePackaging(unittest.TestCase):
    def test_the_version_names_the_base(self):
        self.assertEqual(packaging.base_suffix("v7.3.4"), "7.3.4")
        self.assertEqual(packaging.base_suffix("Ubuntu-7.0.0-44.44"), "44.44")
        self.assertEqual(packaging.base_suffix("Ubuntu-hwe-7.0-7.0.0-44.44_24.04.1"), "44.44")

    def test_a_line_merges_the_config_of_sources_on_its_own_kernel(self):
        from kpm import config

        def line(kind, upstream):
            src = lambda **kw: config.Source(name="x", **kw)
            return config.Config(
                mono=Path("."), line="l", line_dir=Path("."), repo=Path("."), cache=Path("."), kind=kind,
                base_source_name="base", base_tag="t", series_root="", upstream=upstream,
                line_sources=["ubuntu", "aws", "aws-7.0", "bos", "local"],
                sources={"ubuntu": src(packaging_dir="debian.master"), "aws": src(packaging_dir="debian.aws"),
                         "aws-7.0": src(packaging_dir="debian.aws", upstream="v7.0"),
                         "bos": src(upstream="v7.0"), "local": src(kind="local")})
        self.assertEqual(line("stable", "").config_sources(), ["ubuntu", "aws"])
        self.assertEqual(line("ubuntu", "v7.0").config_sources(), ["aws-7.0"])


class UpstreamAnchor(unittest.TestCase):
    """The mainline tag that anchors a fetch is fetched whole, and a shallow repository is completed."""

    def fetches(self, has_tag, shallow):
        calls = []

        class Src:
            @staticmethod
            def remote_location():
                return "mainline-url"

        class Cfg:
            sources = {"mainline": Src}

        class Git:
            def ok(self, *args):
                return has_tag

            def run(self, *args, **kw):
                return "true\n" if shallow else "false\n"

            def stream(self, *args):
                calls.append(args)
                return 0
        core._ensure_upstream_tag(Cfg, Git(), "v7.0")
        return calls

    def test_never_shallow(self):
        calls = self.fetches(has_tag=False, shallow=False)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--depth", calls[0])
        self.assertEqual(self.fetches(has_tag=True, shallow=False), [])

    def test_a_shallow_repository_is_completed(self):
        calls = self.fetches(has_tag=True, shallow=True)
        self.assertEqual(len(calls), 1)
        self.assertIn("--unshallow", calls[0])


class UbuntuBaseIndex(unittest.TestCase):
    """An Ubuntu line has no series root. Its base is indexed from the mainline release."""

    class Cfg:
        kind, series_root, upstream, base_tag = "ubuntu", "", "v7.0", "Ubuntu-hwe-7.0-1"

    class Git:
        def __init__(self, has):
            self.has = has

        def ok(self, *args):
            return self.has

    def test_the_range_starts_at_the_mainline_release(self):
        seen = []
        real, core.index_range = core.index_range, lambda cfg, git, since, upto, since_ref="": seen.append((since, upto, since_ref)) or {"x": 1}
        try:
            self.assertEqual(core.base_index(self.Cfg, self.Git(True)), {"x": 1})
            self.assertEqual(seen, [("v7.0", "Ubuntu-hwe-7.0-1", "mainline/v7.0")])
            # without that release in the repo there is nothing to index
            self.assertEqual(core.base_index(self.Cfg, self.Git(False))["patch_id"], {})
            self.assertEqual(len(seen), 1)
        finally:
            core.index_range = real


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
        rows = [{"sha": "aaaa1111", "subject": "x: change", "disposition": "undecided", "delta_id": "D1", "patch_id": "P1"},
                {"sha": "bbbb2222", "subject": "x: change", "disposition": "undecided", "delta_id": "D2", "patch_id": "P2"},
                {"sha": "cccc3333", "subject": 'Revert "x: change"', "disposition": "undecided",
                 "reverts": "aaaa1111", "inverse_id": "D1", "inverse_patch_id": "P1"}]
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

    def test_each_flaw_of_a_cve_needs_its_own_fix(self):
        # Two introducing commits. We carry the fix of the first only: the second is still open.
        have = lambda sha: sha == "f1"
        self.assertEqual(self.status("5.0:a:7.4:f1", "6.0:b:7.5:f2", carried=have), "backport")
        self.assertEqual(self.status("5.0:a:7.4:f1", "6.0:b:7.5:f2", carried=lambda s: s in ("f1", "f2")),
                         "fixed-by-patch")
        # The second flaw has no fix anywhere; the first one's fix must not hide that.
        self.assertEqual(self.status("5.0:a:7.4:f1", "6.0:b:0:0", carried=have), "no-fix")
        self.assertEqual(self.status("5.0:a:7.4:f1", "6.0:b:0:0"), "no-fix")

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
                return args[2] in ("good", "undone")   # merge-base --is-ancestor <sha> <base>

            def rev(self, sha):
                return {"good": "1" * 40, "undone": "2" * 40}.get(sha, sha)

            def run(self, *args, **kw):          # git log --grep="This reverts commit ..." <sha>..<base>
                if any("2222222" in str(a) for a in args):
                    return "9" * 40 + "\x00This reverts commit " + "2" * 40 + ".\n\x01"
                return ""

        with tempfile.TemporaryDirectory() as tmp:
            Cfg.line_dir = Path(tmp)
            (Path(tmp) / "cves.toml").write_text(
                '["CVE-1"]\nstatus = "not-affected"\nevidence = ["good"]\nreason = "code was removed"\n'
                '["CVE-2"]\nstatus = "not-affected"\nevidence = ["absent"]\nreason = "x"\n'
                '["CVE-3"]\nstatus = "not-affected"\n')
            good, problems = self.cves.assessments(Cfg, FakeGit())
            self.assertEqual(list(good), ["CVE-1"])
            self.assertEqual(len(problems), 2)
            # Evidence that is in the history but reverted there does not hold either.
            (Path(tmp) / "cves.toml").write_text(
                '["CVE-1"]\nstatus = "not-affected"\nevidence = ["good"]\nreason = "code was removed"\n'
                '["CVE-4"]\nstatus = "not-affected"\nevidence = ["undone"]\nreason = "code was removed"\n')
            good, problems = self.cves.assessments(Cfg, FakeGit())
            self.assertEqual(list(good), ["CVE-1"])
            self.assertEqual(len(problems), 1)
            self.assertIn("reverted by 999999999999", problems[0])
            # Nor does evidence that a carried patch reverts: the tree we ship does not have it.
            good, problems = self.cves.assessments(Cfg, FakeGit(), ["111111111111"])
            self.assertEqual(good, {})
            self.assertIn("good (reverted by a carried patch)", " ".join(problems))
            good, problems = self.cves.assessments(Cfg, FakeGit(), ["3" * 12])
            self.assertEqual(list(good), ["CVE-1"])
            # Evidence that the base reverts holds again when a carried patch reverts that revert.
            (Path(tmp) / "cves.toml").write_text(
                '["CVE-4"]\nstatus = "not-affected"\nevidence = ["undone"]\nreason = "code was removed"\n')
            good, problems = self.cves.assessments(Cfg, FakeGit(), [], {"2" * 40})
            self.assertEqual((list(good), problems), (["CVE-4"], []))
            good, problems = self.cves.assessments(Cfg, None)     # cannot check the evidence: do not trust it
            self.assertEqual(good, {})

    def test_a_fix_the_ubuntu_base_reverted_does_not_count(self):
        up1, up2 = "1" * 40, "2" * 40                     # two upstream fixes
        b1, b2, rv = "a" * 40, "b" * 40, "c" * 40         # their backports in the base, and a revert of the first
        up3 = "3" * 40                                    # upstream fixed a third flaw with a revert
        log = (f"{rv}\x00This reverts commit {b1}.\n\n(cherry picked from commit {up3})\n\x01"
               f"{b2}\x00(cherry picked from commit {up2})\n\x01"
               f"{b1}\x00(cherry picked from commit {up1})\n\x01")

        class FakeGit:
            def ok(self, *args):
                return True

            def run(self, *args, **kw):
                if any(str(a).startswith("--grep=^This reverts") for a in args):
                    return f"{rv}\x00This reverts commit {b1}.\n\x01"
                return log

        class Cfg:
            upstream, base_ref, base_tag = "v7.0", "ubuntu/Ubuntu-x", "Ubuntu-x"

        with tempfile.TemporaryDirectory() as tmp:
            Cfg.cache = Path(tmp)
            have = self.cves.base_commits(Cfg, FakeGit())
        self.assertIn(up2, have)
        self.assertIn(b2, have)
        self.assertNotIn(up1, have)                        # backported, then reverted: not fixed
        self.assertNotIn(b1, have)
        self.assertIn(rv, have)                            # the revert is in the tree,
        self.assertIn(up3, have)                           # and it can be the fix of another CVE
        self.assertEqual(self.cves.assess([("5.0", "x", "7.2", up3)], self.cves.Base((7, 0), None), self.none,
                                          lambda s: s in have)["status"], "fixed-in-base")
        ubuntu = self.cves.Base((7, 0), None)
        self.assertEqual(self.cves.assess([("5.0", "x", "7.2", up1)], ubuntu, self.none,
                                          lambda s: s in have)["status"], "backport")
        self.assertEqual(self.cves.assess([("5.0", "x", "7.2", up2)], ubuntu, self.none,
                                          lambda s: s in have)["status"], "fixed-in-base")

    def test_a_release_number_is_not_proof_when_the_base_reverts_the_fix(self):
        gone = lambda sha: sha == "f1"
        # The fix is in mainline 7.1, below our 7.3.4 base, and the base reverts it.
        res = self.cves.assess([("5.0", "a", "7.1", "f1")], self.base, self.none, undone=gone)
        self.assertEqual((res["status"], res["fix"], res["reverted_in_base"]), ("backport", "f1", True))
        # The same for a fix that came in with a release of our own series.
        self.assertEqual(self.cves.assess([("5.0", "a", "7.3.2", "f1")], self.base, self.none, undone=gone)["status"], "backport")
        # A patch of ours that is the fix closes it again.
        self.assertEqual(self.cves.assess([("5.0", "a", "7.1", "f1")], self.base, lambda s: s == "f1", undone=gone)["status"],
                         "fixed-by-patch")
        # Reverted in mainline form, fixed again by a later release of our series that the base has.
        self.assertEqual(self.cves.assess([("5.0", "a", "7.1", "f1"), ("5.0", "a", "7.3.3", "f2")],
                                          self.base, self.none, undone=gone)["status"], "fixed-in-base")
        # An Ubuntu base on 7.0 that reverts a fix that was in mainline 7.0.
        ubuntu = self.cves.Base((7, 0), None)
        self.assertEqual(self.cves.assess([("5.0", "a", "7.0", "f1")], ubuntu, self.none, undone=gone)["status"], "backport")
        self.assertEqual(self.cves.assess([("5.0", "a", "7.0", "f1")], ubuntu, self.none)["status"], "fixed-in-base")

    def test_a_carried_revert_of_a_fix_reopens_the_cve(self):
        fix = "f" * 40
        with tempfile.TemporaryDirectory() as tmp:
            class Cfg:
                line_sources = ["vendor"]

                @staticmethod
                def ledger_path(name):
                    return Path(tmp) / f"{name}.jsonl"
            ledger.write(Cfg.ledger_path("vendor"), [
                {"sha": "a" * 40, "subject": 'Revert "x: fix"', "disposition": "carry", "reason": "r", "reverts": fix[:12]},
                {"sha": "b" * 40, "subject": 'Revert "y"', "disposition": "dropped", "reason": "r", "reverts": "e" * 12},
                {"sha": "c" * 40, "subject": "z", "disposition": "carry", "reason": "r"}])
            self.assertEqual(self.cves.carried_reverts(Cfg), [fix[:12]])      # only what we carry

            # The patch names a stable backport; the CVE record names the upstream fix.
            class FakeGit:
                def run(self, *args, **kw):
                    return "commit " + "9" * 40 + " upstream.\n"
            self.assertEqual(self.cves.carried_reverts(Cfg, FakeGit()), [fix[:12], "9" * 40])

            # A later patch reverts our revert: the fix is back, and nothing is removed.
            ledger.write(Cfg.ledger_path("vendor"), ledger.read(Cfg.ledger_path("vendor")) + [
                {"sha": "d" * 40, "subject": 'Revert "Revert "x: fix""', "disposition": "carry", "reason": "r",
                 "reverts": "a" * 12}])
            class NoRefs:
                def run(self, *args, **kw):
                    return ""
            # The fix is back. The first revert patch is the one that is removed now:
            # if it was itself the fix of another CVE, that fix is gone.
            self.assertEqual(self.cves.carried_reverts(Cfg, NoRefs()), ["a" * 40])
            # A third patch reverts the second: the first revert holds again.
            ledger.write(Cfg.ledger_path("vendor"), ledger.read(Cfg.ledger_path("vendor")) + [
                {"sha": "e" * 40, "subject": "Revert x3", "disposition": "carry", "reason": "r", "reverts": "d" * 40}])
            self.assertEqual(self.cves.carried_reverts(Cfg, NoRefs()), [fix[:12], "d" * 40])
        gone = lambda sha: sha.startswith(fix[:12])
        # the fix is in the 7.1 release, below our base, and our series reverts it
        res = self.cves.assess([("5.0", "a", "7.1", fix)], self.base, self.none, undone=gone)
        self.assertEqual((res["status"], res["reverted_in_base"]), ("backport", True))

    def test_a_backported_introducing_commit_makes_us_affected(self):
        intro, fix = "1" * 40, "2" * 40
        pairs = [("7.4", intro, "7.5", fix)]                 # introduced after our 7.3.4 base
        self.assertEqual(self.cves.assess(pairs, self.base, self.none)["status"], "not-affected")
        # ... but a patch of ours is the introducing commit, and we do not have the fix
        has_intro = lambda sha: sha == intro
        res = self.cves.assess(pairs, self.base, has_intro)
        self.assertEqual((res["status"], res["fix"]), ("backport", fix))
        # with the fix carried too, it is closed
        self.assertEqual(self.cves.assess(pairs, self.base, lambda s: s in (intro, fix))["status"], "fixed-by-patch")
        # the same when an Ubuntu base's own history has the introducing commit
        ubuntu = self.cves.Base((7, 0), None)
        self.assertEqual(self.cves.assess(pairs, ubuntu, self.none, in_base=has_intro)["status"], "backport")
        self.assertEqual(self.cves.assess(pairs, ubuntu, self.none)["status"], "not-affected")

    def test_a_reverted_introducing_commit_leaves_nothing_to_fix(self):
        intro, fix = "1" * 40, "2" * 40
        pairs = [("7.2", intro, "7.5", fix)]                 # in our 7.3.4 base, fixed later
        self.assertEqual(self.cves.assess(pairs, self.base, self.none)["status"], "backport")
        gone = lambda sha: sha == intro                       # the base, or a patch of ours, reverts it
        self.assertEqual(self.cves.assess(pairs, self.base, self.none, undone=gone)["status"], "not-affected")
        # A second flaw of the same CVE, from another commit, still counts.
        pairs.append(("7.1", "3" * 40, "7.6", "4" * 40))
        self.assertEqual(self.cves.assess(pairs, self.base, self.none, undone=gone)["status"], "backport")

    def test_ubuntu_base_has_no_release_number(self):
        ubuntu = self.cves.Base((7, 0), None)
        self.assertEqual(self.status("5.0:a:7.0.4:f1", base=ubuntu), "rebase")                 # not provable by version
        self.assertEqual(self.cves.assess([("5.0", "a", "7.0.4", "f1")], ubuntu, self.none,
                                          lambda sha: sha == "f1")["status"], "fixed-in-base")


class RevertChains(unittest.TestCase):
    A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40

    def reverts(self, *pairs):
        log = "".join(f"{rev}\x00This reverts commit {target}.\n\x01" for rev, target in pairs)

        class FakeGit:
            def run(self, *args, **kw):
                return log
        return core.reverts_in(FakeGit(), "old", "new")

    def test_one_revert(self):
        self.assertEqual(self.reverts((self.B, self.A)), {self.A: self.B})

    def test_a_reverted_revert_puts_the_commit_back_and_is_itself_undone(self):
        # A; B reverts A; C reverts B. A stands, B does not.
        self.assertEqual(self.reverts((self.B, self.A), (self.C, self.B)), {self.B: self.C})

    def test_three_in_a_row(self):
        # D reverts C: B is back, so A is undone again; C is undone.
        got = self.reverts((self.B, self.A), (self.C, self.B), (self.D, self.C))
        self.assertEqual(got, {self.A: self.B, self.C: self.D})

    def test_short_names(self):
        got = self.reverts((self.B, self.A[:12]), (self.C, self.B[:12]))
        self.assertEqual(got, {self.B[:12]: self.C})

    def test_the_cve_index_leaves_out_a_fix_that_was_undone(self):
        # A introduces a flaw. B fixes it by reverting A. C reverts B: the flaw is back.
        from kpm import cves
        A, B, C = self.A, self.B, self.C
        log = (f"{C}\x00This reverts commit {B}.\n\x01{B}\x00This reverts commit {A}.\n\x01{A}\x00add feature\n\x01")

        class FakeGit:
            def ok(self, *args):
                return True

            def run(self, *args, **kw):
                if "--reverse" in args:              # reverts_in reads oldest first
                    return f"{B}\x00This reverts commit {A}.\n\x01{C}\x00This reverts commit {B}.\n\x01"
                return log

        class Cfg:
            upstream, base_ref, base_tag = "v7.0", "ubuntu/Ubuntu-x", "Ubuntu-x"

        with tempfile.TemporaryDirectory() as tmp:
            Cfg.cache = Path(tmp)
            have = cves.base_commits(Cfg, FakeGit())
        self.assertNotIn(B, have)                    # the fix is not in effect
        self.assertIn(A, have)
        status = cves.assess([("7.0", A, "7.0.5", B)], cves.Base((7, 0), None), lambda s: False, lambda s: s in have)
        self.assertEqual(status["status"], "rebase")


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
