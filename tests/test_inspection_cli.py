"""Exercise archive inspection through the normal command-line entry point."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/synthetic-population"


class InspectionCLITests(unittest.TestCase):
    def run_cli(self, *arguments, environment=None):
        return subprocess.run([sys.executable, "-m", "eco_evidence", "inspect", *arguments],
                              cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30)

    def test_json_inspection_reports_verified_metadata_and_origin(self):
        result = self.run_cli("--bundle", str(FIXTURE), "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        bundle = report["bundle"]
        self.assertEqual(bundle["indicator"]["code"], "SP.POP.TOTL")
        self.assertEqual(bundle["indicator"]["unit"], "people")
        self.assertEqual(bundle["data_origin"], "synthetic_fixture")
        self.assertEqual(bundle["counts"], {"available": 1, "missing": 1, "not_returned": 1})
        self.assertIsNone(report["comparison"])

    def test_default_text_and_same_archive_comparison(self):
        result = self.run_cli("--bundle", str(FIXTURE), "--baseline", str(FIXTURE))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SP.POP.TOTL", result.stdout)
        self.assertIn("synthetic_fixture", result.stdout)
        self.assertIn("people", result.stdout)
        structured = self.run_cli("--bundle", str(FIXTURE), "--baseline", str(FIXTURE),
                                  "--format", "json")
        self.assertEqual(structured.returncode, 0, structured.stderr)
        comparison = json.loads(structured.stdout)["comparison"]
        self.assertEqual(comparison["changed_row_count"], 0)
        self.assertEqual(comparison["metadata_changes"], [])

    def test_text_report_works_with_ascii_only_stdout(self):
        environment = dict(os.environ, PYTHONIOENCODING="ascii")
        for baseline in ((), ("--baseline", str(FIXTURE))):
            with self.subTest(baseline=bool(baseline)):
                result = self.run_cli("--bundle", str(FIXTURE), *baseline, environment=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertTrue(result.stdout.isascii())
                self.assertIn("SP.POP.TOTL", result.stdout)

    def test_revised_example_reports_data_changes_despite_unchanged_update_label(self):
        result = self.run_cli("--bundle", str(ROOT / "examples/synthetic-population-revised"),
                              "--baseline", str(FIXTURE), "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["bundle"]["data_origin"], "synthetic_fixture")
        comparison = report["comparison"]
        self.assertEqual(comparison["changed_row_count"], 3)
        self.assertEqual(comparison["numeric_value_change_count"], 1)
        self.assertEqual(comparison["row_field_change_counts"]["status"], 2)
        self.assertFalse(comparison["source_update_changed"])

    def test_incompatible_baseline_fails_without_partial_report(self):
        result = self.run_cli("--bundle", str(FIXTURE), "--baseline",
                              str(ROOT / "examples/synthetic-gdp"), "--format", "json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("eco-evidence:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_checksum_failure_preserves_input_and_emits_no_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            shutil.copytree(FIXTURE, bundle)
            raw = next((bundle / "raw").glob("*.json"))
            raw.write_bytes(raw.read_bytes() + b" ")
            before = {path.relative_to(bundle): path.read_bytes()
                      for path in bundle.rglob("*") if path.is_file()}
            result = self.run_cli("--bundle", str(bundle), "--format", "json")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(before, {path.relative_to(bundle): path.read_bytes()
                                     for path in bundle.rglob("*") if path.is_file()})


if __name__ == "__main__":
    unittest.main()
