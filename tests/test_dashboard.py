"""The status page: data collection from the repo's records, the action list, the HTML."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dashboard_generate", ROOT / "tools/dashboard/generate.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def line(**kw):
    base = {"name": "t", "kind": "stable", "base": "v1.0.1", "patches": 2, "base_status": None, "sources_moved": [],
            "sources": [{"source": "vendor", "band": 0, "band_shown": "0000", "commits": 3, "patches": 2, "proposals": 0, "adapted": 0,
                         "pending": 0, "decisions": {"carry": 2, "dropped": 1}}],
            "problems": [], "undecided": 0, "proposals": 0, "pending": 0, "adapted": 0, "deferred": [],
            "cves": {"available": False}}
    base.update(kw)
    return base


def page(lines, **kw):
    data = {"generated": "now", "offline": True, "updates_error": "", "lines": lines, "builds": [],
            "userspace": {"series": ["noble"], "packages": [], "checked_online": False}, "modules": []}
    data.update(kw)
    data["actions"] = gen.build_actions(data)
    return data


class Actions(unittest.TestCase):
    def test_a_healthy_line_needs_nothing(self):
        self.assertEqual(page([line()])["actions"], [])

    def test_each_problem_is_one_action_with_its_command(self):
        cves = {"available": True, "records": 10, "note": "", "open_total": 3, "open": [], "compiled_from": "",
                "counts": {"not-affected": 1, "fixed-in-base": 6, "fixed-by-patch": 0, "rebase": 2, "backport": 1, "no-fix": 0}}
        l = line(undecided=4, proposals=1, pending=2,
                 base_status={"status": "STALE", "latest": "v1.0.3"},
                 sources_moved=[{"source": "vendor", "status": "MOVED", "pin": "Vendor-1", "latest": "Vendor-2"}],
                 cves=cves)
        acts = page([l])["actions"]
        self.assertEqual([a["level"] for a in acts], ["crit", "crit", "warn", "warn", "warn", "warn"])
        text = " | ".join(a["text"] for a in acts)
        for needle in ("4 commits have no decision", "2 carried commits have no patch file", "is behind v1.0.3",
                       "Source vendor has new commits", "2 open CVEs are fixed in a newer release", "3 open CVEs: 1 need a backport"):
            self.assertIn(needle, text)
        self.assertIn("./scripts/kpm --line t rebase v1.0.3", [a["command"] for a in acts])

    def test_a_critical_open_cve_must_be_fixed(self):
        cves = {"available": True, "records": 10, "note": "", "open_total": 5, "open": [], "compiled_from": "",
                "by_severity": {"critical": 1, "high": 2, "medium": 0, "low": 0, "unscored": 2},
                "counts": {"not-affected": 0, "fixed-in-base": 5, "fixed-by-patch": 0, "rebase": 0, "backport": 5, "no-fix": 0}}
        act = page([line(cves=cves)])["actions"][0]
        self.assertEqual(act["level"], "crit")
        self.assertIn("5 open CVEs (1 critical, 2 high)", act["text"])
        self.assertIn("--severity high", act["command"])

    def test_userspace_actions(self):
        us = {"series": ["noble"], "checked_online": True, "packages": [
            {"package": "pahole", "why": "", "series": {"noble": {"version": "1.31", "from": "stonking", "overlay": False,
                                                                 "built": False, "newer": "1.32"}}}]}
        acts = page([line()], userspace=us)["actions"]
        self.assertEqual([a["level"] for a in acts], ["warn", "info"])


class Render(unittest.TestCase):
    def test_sections_and_escaping(self):
        l = line(deferred=[{"reason": "waits for <vendor> & rework", "count": 61, "sources": ["vendor"]}])
        html = gen.render(page([l]))
        for heading in ("1. What needs action", "2. Lines", "3. Builds", "4. Userspace packages", "5. Out-of-tree modules"):
            self.assertIn(heading, html)
        self.assertIn("waits for &lt;vendor&gt; &amp; rework", html)
        self.assertNotIn("<vendor>", html)
        self.assertIn("No build records found", html)

    def test_open_cves_are_listed_with_what_closes_them(self):
        cves = {"available": True, "records": 5, "note": "", "open_total": 2, "compiled_from": "noble amd64",
                "by_severity": {"critical": 0, "high": 1, "medium": 0, "low": 0, "unscored": 1},
                "counts": {"not-affected": 0, "fixed-in-base": 3, "fixed-by-patch": 0, "rebase": 1, "backport": 1, "no-fix": 0},
                "open": [{"cve": "CVE-2026-0001", "status": "rebase", "fixed_in": "1.0.9", "fix": "a" * 40, "title": "x: fix",
                          "compiled": True, "severity": "high", "score": 7.8, "vector": "CVSS:3.1/AV:L"},
                         {"cve": "CVE-2026-0002", "status": "backport", "fixed_in": "1.2", "fix": "b" * 40, "title": "y: fix",
                          "compiled": False, "severity": "unscored"}]}
        html = gen.render(page([line(cves=cves)]))
        # the severity filter: a rating on each row, and the buttons that use it
        self.assertIn('<tr data-sev="high">', html)
        self.assertIn('<tr data-sev="unscored">', html)
        self.assertIn("high 7.8", html)
        self.assertIn('data-min="high"', html)
        self.assertIn("High or worse (1)", html)
        self.assertIn("Not scored (1)", html)
        self.assertIn("move the base to <code>v1.0.9</code>", html)
        self.assertIn("https://git.kernel.org/stable/c/" + "b" * 40, html)
        self.assertIn("CVE-2026-0002", html)


class RealRepo(unittest.TestCase):
    def test_offline_collection_matches_the_records(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"WORK_ROOT": tmp}):
            data = gen.collect(offline=True, records=[], cve_limit=5)
        names = [l["name"] for l in data["lines"]]
        self.assertIn("7.3-lts", names)
        for l in data["lines"]:
            series = (ROOT / "kernel/lines" / l["name"] / "series").read_text().split()
            self.assertEqual(l["patches"], len(series))
            self.assertEqual(sum(s["patches"] for s in l["sources"]), len(series))
            self.assertEqual(l["problems"], [])
            self.assertEqual(l["cves"], {"available": False})          # no CVE records in the empty WORK_ROOT
        self.assertTrue(any(p["package"] == "dwarves" for p in data["userspace"]["packages"]))
        json.dumps(data)                                               # the data must serialise
        self.assertIn("7.3-lts", gen.render(data))

    def test_results_of_an_older_build_do_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "build/7.3-lts/noble"
            d.mkdir(parents=True)
            (d / "build-record.json").write_text(json.dumps({
                "line": "7.3-lts", "series": "noble", "arch": "amd64", "version": "2", "date": "2026-11-14T10:00Z"}))
            # results left from the build before this one
            (d / "install-test.json").write_text(json.dumps({"ok": True, "passed": 69, "date": "2026-11-13T02:00Z"}))
            (d / "boot-kernops.json").write_text('{"flavour": "kernops", "ok": true, "date": "2026-11-13T01:20Z"}')
            with mock.patch.dict(os.environ, {"WORK_ROOT": tmp}):
                b = gen.collect_builds([])[0]
            self.assertNotIn("install_test", b)
            self.assertEqual(b["boot"], [])
            # a result for another build, even a newer one, does not count either
            (d / "install-test.json").write_text(json.dumps({
                "ok": True, "passed": 69, "date": "2026-11-14T11:00Z", "build": {"version": "1", "date": "2026-11-13T00:00Z"}}))
            with mock.patch.dict(os.environ, {"WORK_ROOT": tmp}):
                self.assertNotIn("install_test", gen.collect_builds([])[0])
            # the result of this build does
            (d / "install-test.json").write_text(json.dumps({
                "ok": True, "passed": 69, "date": "2026-11-14T11:00Z", "build": {"version": "2", "date": "2026-11-14T10:00Z"}}))
            with mock.patch.dict(os.environ, {"WORK_ROOT": tmp}):
                self.assertTrue(gen.collect_builds([])[0]["install_test"]["ok"])

    def test_install_test_and_test_key_are_shown(self):
        build = {"line": "7.3-lts", "series": "noble", "arch": "amd64", "version": "1", "date": "2026-11-13T01:10Z",
                 "toolchain": {}, "flavours": [], "boot": [],
                 "trusted_certs": ["subject=CN = acme TEST signing key (not for production) sha256 Fingerprint=AA"],
                 "install_test": {"ok": False, "failed": ["modprobe nvme"], "passed": 10, "date": "2026-11-13T02:00Z"}}
        data = page([line(name="7.3-lts")], builds=[build])
        text = " | ".join(a["text"] for a in data["actions"])
        self.assertIn("Install test failed for the noble amd64 build: modprobe nvme", text)
        self.assertIn("trusts a test signing key", text)
        self.assertEqual(data["actions"][0]["level"], "crit")
        html = gen.render_builds(data)
        self.assertIn("install test failed: 1 checks", html)
        self.assertIn("trusts an extra key: acme TEST signing key", html)
        build["install_test"] = {"ok": True, "failed": [], "passed": 69, "date": "2026-11-13T02:00Z"}
        self.assertIn("installs and works: 69 checks", gen.render_builds(page([line()], builds=[build])))

    def test_build_record_is_picked_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "build/7.3-lts/noble"
            d.mkdir(parents=True)
            (d / "build-record.json").write_text(json.dumps({
                "line": "7.3-lts", "series": "noble", "arch": "amd64", "version": "7.3.0-1.1~24.04.1",
                "date": "2026-11-13T01:10Z", "toolchain": {"gcc": "13.3.0", "pahole": "1.31"},
                "flavours": [{"name": "kernops", "modules": 6919, "patches": {"compiled": 283, "total": 561}}]}))
            (d / "boot-kernops.json").write_text('{"flavour": "kernops", "ok": true, "date": "2026-11-13T01:20Z"}')
            with mock.patch.dict(os.environ, {"WORK_ROOT": tmp}):
                builds = gen.collect_builds([])
        self.assertEqual(len(builds), 1)
        self.assertTrue(builds[0]["boot"][0]["ok"])
        # no install test yet: that is an action
        acts = page([line(name="7.3-lts")], builds=builds)["actions"]
        self.assertIn("has no install test", " | ".join(a["text"] for a in acts))
        html = gen.render_builds({"builds": builds})
        self.assertIn("283 of 561 patches reach built code", html)
        self.assertIn("kernops booted 2026-11-13", html)


if __name__ == "__main__":
    sys.exit(unittest.main())
