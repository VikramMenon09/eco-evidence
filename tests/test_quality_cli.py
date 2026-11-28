"""Exercise quality reports through the public CLI and real output streams."""

import hashlib
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
REAL = ROOT / "examples/output-energy-study/inputs/renewables"
GUARDED_MAIN = r'''
import os, runpy, sys
sys.dont_write_bytecode = True
def guard(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "os.mkdir", "os.remove", "os.rename", "os.rmdir"):
        raise AssertionError("Unexpected network or filesystem mutation: " + event)
    if event == "open":
        path, mode, flags = args
        if isinstance(path, (str, bytes)) and os.fsdecode(path).endswith("/data.csv"):
            raise AssertionError("Derived input CSV must not be read")
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError("Unexpected writable file")
sys.addaudithook(guard)
sys.argv = ["eco-evidence", "quality", *sys.argv[1:]]
runpy.run_module("eco_evidence", run_name="__main__")
'''


def snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes() if path.is_file() else None
            for path in directory.rglob("*")}


class QualityCLITests(unittest.TestCase):
    def run_cli(self, *arguments, guarded=False):
        command = ([sys.executable, "-c", GUARDED_MAIN] if guarded else
                   [sys.executable, "-m", "eco_evidence", "quality"])
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="ascii")
        return subprocess.run([*command, *map(str, arguments)], cwd=ROOT, env=environment,
                              capture_output=True, timeout=30)

    def test_default_text_and_explicit_formats_are_deterministic(self):
        before = snapshot(FIXTURE)
        default = self.run_cli("--bundle", FIXTURE, guarded=True)
        explicit = self.run_cli("--bundle", FIXTURE, "--format", "text", guarded=True)
        first_json = self.run_cli("--bundle", FIXTURE, "--format", "json", guarded=True)
        second_json = self.run_cli("--bundle", FIXTURE, "--format", "json", guarded=True)
        for result in (default, explicit, first_json, second_json):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, b"")
            result.stdout.decode("ascii")
        self.assertEqual(default.stdout, explicit.stdout)
        self.assertEqual(first_json.stdout, second_json.stdout)
        self.assertIn(b"synthetic_fixture", default.stdout)
        self.assertIn(b"SP.POP.TOTL", default.stdout)
        self.assertIn(b"leading", default.stdout)
        report = json.loads(first_json.stdout)
        self.assertEqual(report["tool_version"], "0.6.0")
        self.assertEqual(report["summary"]["counts"], {"available": 1, "missing": 1, "not_returned": 1})
        self.assertEqual(report["summary"]["zero_value_count"], 1)
        self.assertEqual(report["summary"]["flagged_row_count"], 0)
        self.assertEqual(report["countries"][0]["gaps"], [{
            "start_year": 2020, "end_year": 2021, "length": 2, "position": "leading",
            "counts": {"missing": 1, "not_returned": 1},
        }])
        self.assertEqual([(r["year"], r["status"], r["value"]) for r in report["rows"]],
                         [(2020, "not_returned", ""), (2021, "missing", ""), (2022, "available", "0")])
        self.assertEqual(snapshot(FIXTURE), before)

    def test_retained_real_archive_keeps_all_six_countries_and_late_missing_years(self):
        before = snapshot(REAL)
        result = self.run_cli("--bundle", REAL, "--format", "json", guarded=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["bundle"]["data_origin"], "worldbank_api")
        self.assertEqual(report["summary"]["row_count"], 30)
        self.assertEqual(report["summary"]["counts"], {"available": 18, "missing": 12, "not_returned": 0})
        self.assertEqual(report["summary"]["gap_position_counts"],
                         {"leading": 0, "internal": 0, "trailing": 6, "all_unavailable": 0})
        self.assertEqual(report["rule"]["maximum"], "100")
        self.assertEqual([r["country_code"] for r in report["countries"]],
                         ["BRA", "CAN", "DEU", "IND", "USA", "ZAF"])
        for country in report["countries"]:
            self.assertEqual(country["gaps"], [{
                "start_year": 2022, "end_year": 2023, "length": 2, "position": "trailing",
                "counts": {"missing": 2, "not_returned": 0},
            }])
        self.assertEqual(snapshot(REAL), before)

    def test_derived_csv_and_manifest_summaries_are_not_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle"
            shutil.copytree(FIXTURE, bundle)
            (bundle / "data.csv").write_text("Invented replacement CSV that must never be read.\n")
            manifest_path = bundle / "provenance.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["counts"] = {"available": 9999}
            manifest["indicator"]["unit"] = "misleading derived unit"
            manifest_path.write_text(json.dumps(manifest))
            before = snapshot(bundle)
            result = self.run_cli("--bundle", bundle, "--format", "json", guarded=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["row_count"], 3)
            self.assertEqual(report["bundle"]["indicator"]["unit"], "people")
            self.assertEqual(report["bundle"]["original_manifest_sha256"],
                             hashlib.sha256(manifest_path.read_bytes()).hexdigest())
            self.assertEqual(snapshot(bundle), before)

    def test_invalid_archives_and_formats_fail_without_partial_report_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "corrupt"
            shutil.copytree(FIXTURE, corrupt)
            raw = next((corrupt / "raw").glob("*.json"))
            raw.write_bytes(raw.read_bytes() + b" ")
            before = snapshot(corrupt)
            for args in (("--bundle", corrupt),
                         ("--bundle", ROOT / "examples/noaa-global-co2"),
                         ("--bundle", FIXTURE, "--format", "csv")):
                with self.subTest(arguments=args):
                    result = self.run_cli(*args)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, b"")
                    self.assertNotIn(b"Traceback", result.stderr)
            self.assertEqual(snapshot(corrupt), before)

    def test_report_bound_failure_emits_no_partial_stdout(self):
        script = (
            "import sys; import eco_evidence.quality as quality; "
            "quality.MAX_REPORT_BYTES = 1; "
            "from eco_evidence.cli import main; raise SystemExit(main(sys.argv[1:]))"
        )
        for output_format in ("text", "json"):
            result = subprocess.run(
                [sys.executable, "-c", script, "quality", "--bundle", str(FIXTURE),
                 "--format", output_format], cwd=ROOT, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"eco-evidence:", result.stderr)

    def test_preclosed_stdout_pipe_exits_cleanly_for_both_formats(self):
        for output_format in ("text", "json"):
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "eco_evidence", "quality", "--bundle", str(FIXTURE),
                     "--format", output_format], cwd=ROOT, stdout=write_fd,
                    stderr=subprocess.PIPE, timeout=30,
                )
            finally:
                os.close(write_fd)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(b"eco-evidence:", result.stderr)
            self.assertNotIn(b"Traceback", result.stderr)
            self.assertNotIn(b"Exception ignored", result.stderr)

    def test_stdout_closed_before_python_startup_reports_an_error(self):
        script = ('import os, sys; os.close(1); '
                  'os.execv(sys.executable, [sys.executable, "-m", "eco_evidence", *sys.argv[1:]])')
        result = subprocess.run(
            [sys.executable, "-c", script, "quality", "--bundle", str(FIXTURE)],
            cwd=ROOT, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"eco-evidence:", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertNotIn(b"Exception ignored", result.stderr)


if __name__ == "__main__":
    unittest.main()
