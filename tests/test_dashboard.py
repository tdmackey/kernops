#!/usr/bin/env python3
import importlib.util
import os
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN_PATH = os.path.join(ROOT, "tools", "dashboard", "generate.py")
SPEC = importlib.util.spec_from_file_location("dashboard_generate", GEN_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


class DashboardTests(unittest.TestCase):
    def test_tag_to_version_preserves_hwe_debian_revision(self):
        self.assertEqual(
            dashboard.tag_to_version("Ubuntu-hwe-6.17-6.17.0-38.38_24.04.1"),
            "6.17.0-38.38~24.04.1",
        )
        self.assertEqual(
            dashboard.tag_to_version("Ubuntu-6.8.0-130.130"),
            "6.8.0-130.130",
        )

    def test_debian_compare_handles_epoch_tilde_and_local_suffix(self):
        self.assertLess(
            dashboard.debian_compare("6.17.0-38.38~24.04.1", "6.17.0-38.38"),
            0,
        )
        self.assertGreater(
            dashboard.debian_compare("1:6.8.0-1", "6.8.0-999"),
            0,
        )
        self.assertGreater(
            dashboard.debian_compare("6.8.0-130.130+gb200.1", "6.8.0-130.130"),
            0,
        )

    def test_artifact_analysis_matches_kernel_pinned_modules_by_arch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = os.path.join(tmp, "out", "noble-6.8")
            arm_dir = os.path.join(base_dir, "arm64", "run-1")
            amd_dir = os.path.join(base_dir, "amd64", "run-1")
            for run_dir in (arm_dir, amd_dir):
                os.makedirs(os.path.join(run_dir, "modules"))
            arm_names = [
                "linux-image-6.8.0-124-generic_6.8.0-124.124_arm64.deb",
                "linux-headers-6.8.0-124-generic_6.8.0-124.124_arm64.deb",
                "gb200-modules-doca-6.8.0-124-generic_3.4.0+kernel.6.8.0-124.124_arm64.deb",
                "gb200-modules-nvidia-open-6.8.0-124-generic_580.159.04+kernel.6.8.0-123.123_arm64.deb",
            ]
            amd_names = [
                "linux-image-6.8.0-124-generic_6.8.0-124.124_amd64.deb",
                "linux-headers-6.8.0-124-generic_6.8.0-124.124_amd64.deb",
                "gb200-modules-doca-6.8.0-124-generic_3.4.0+kernel.6.8.0-124.124_amd64.deb",
            ]
            for name in arm_names[:2]:
                open(os.path.join(arm_dir, name), "w").close()
            for name in arm_names[2:]:
                open(os.path.join(arm_dir, "modules", name), "w").close()
            for name in amd_names[:2]:
                open(os.path.join(amd_dir, name), "w").close()
            for name in amd_names[2:]:
                open(os.path.join(amd_dir, "modules", name), "w").close()

            artifacts = dashboard.list_artifacts([tmp])
            provenance = [{
                "base": "noble-6.8",
                "arch": "arm64",
                "_mtime": 1,
                "module_abi": [{
                    "name": "doca",
                    "modules": [{
                        "signature_appended": True,
                        "undefined_symbols": ["ib_register_client"],
                        "modinfo": {
                            "vermagic": "6.8.0-124-generic SMP mod_unload",
                            "signer": "gb200",
                        },
                    }],
                }],
            }]
            rows = [
                {"name": "doca", "version": "3.4.0", "source": "repo"},
                {"name": "nvidia-open", "version": "580.159.04", "source": "git"},
            ]
            arm = dashboard.analyze_artifacts("noble-6.8", "arm64", artifacts, rows, provenance)
            amd = dashboard.analyze_artifacts("noble-6.8", "amd64", artifacts, rows)
            self.assertEqual(arm["kernel"]["arch"], "arm64")
            self.assertEqual(amd["kernel"]["arch"], "amd64")
            self.assertTrue(arm["header_ok"])
            self.assertTrue(amd["header_ok"])
            self.assertIsNotNone(arm["modules"][0]["current"])
            self.assertIsNone(arm["modules"][1]["current"])
            self.assertIsNotNone(amd["modules"][0]["current"])
            self.assertEqual(arm["modules"][0]["abi"]["modules"][0]["modinfo"]["signer"], "gb200")

    def test_missing_clone_does_not_mark_upstream_sha_invalid(self):
        patches = [{
            "kind": "cherry picked",
            "upstream": "0123456789abcdef",
            "subject": "test patch",
        }]
        dashboard.analyze_patch_stack("/does/not/exist", "pin", "latest", patches)
        self.assertEqual(patches[0]["pin_presence"]["status"], "unknown")
        self.assertEqual(patches[0]["pin_presence"]["label"], "clone missing")
        self.assertEqual(patches[0]["warnings"], [])

    def test_render_contains_operator_sections(self):
        base = {
            "name": "noble-6.8",
            "pin": "Ubuntu-6.8.0-124.124",
            "version": "6.8.0-124.124",
            "package": "linux",
            "git": {"latest_tag": "Ubuntu-6.8.0-130.130", "behind": 1, "pin_exists": True},
            "treadmill": {"status": "offline"},
            "patches": [],
            "artifacts_by_arch": {
                "arm64": {"kernel": None, "header_ok": False, "matches_pin": False,
                          "modules": [{"name": "doca", "version": "3.4.0",
                                       "source": "repo", "current": None,
                                       "latest": None, "abi": None}]},
                "amd64": {"kernel": None, "header_ok": False, "modules": [], "matches_pin": False},
            },
            "range_diff": None,
            "cve_source": "offline",
            "cves": ([], []),
        }
        html = dashboard.render(ROOT, [base], [], "offline")
        for heading in ("Action queue", "Build health", "Module matrix", "ABI", "Recent build artifacts"):
            self.assertIn(heading, html)


if __name__ == "__main__":
    unittest.main()
