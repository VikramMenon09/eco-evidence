"""Offline CLI acceptance using the attributed, archived worked example."""

import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/output-energy-study"


class StudyCLITests(unittest.TestCase):
    def test_committed_example_manifests_match_all_derived_files(self):
        def check_hash(path, expected):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected, str(path))

        manifest = json.loads((EXAMPLE / "provenance.json").read_text())
        check_hash(EXAMPLE / manifest["study_csv_file"], manifest["study_csv_sha256"])
        check_hash(EXAMPLE / manifest["summary_file"], manifest["summary_sha256"])
        self.assertEqual([item["alias"] for item in manifest["inputs"]],
                         ["gdp", "population", "renewables"])
        for item in manifest["inputs"]:
            bundle = EXAMPLE / item["bundle_path"]
            check_hash(bundle / "provenance.json", item["embedded_manifest_sha256"])
            embedded = json.loads((bundle / "provenance.json").read_text())
            check_hash(bundle / "data.csv", embedded["csv_sha256"])
            for source in embedded["raw_sources"]:
                check_hash(bundle / source["file"], source["sha256"])

    def arguments(self, inputs, output):
        return ["study", "--gdp-bundle", str(inputs / "gdp"), "--population-bundle",
                str(inputs / "population"), "--renewables-bundle", str(inputs / "renewables"),
                "--output", str(output)]

    def run_cli(self, arguments):
        return subprocess.run([sys.executable, "-m", "eco_evidence", *arguments], cwd=ROOT,
                              text=True, capture_output=True, timeout=30)

    def test_real_example_keeps_missing_years_and_replays_from_embedded_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "study"
            result = self.run_cli(self.arguments(EXAMPLE / "inputs", output))
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual((summary["rows"], summary["derived"], summary["complete_pairs"]), (30, 30, 18))
            self.assertEqual(summary["data_origin"], "worldbank_api")
            for name in ("study.csv", "summary.json"):
                self.assertEqual((output / name).read_bytes(), (EXAMPLE / name).read_bytes())
            with (output / "study.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            later = [row for row in rows if row["year"] in ("2022", "2023")]
            self.assertEqual(len(later), 12)
            self.assertTrue(all(row["gdp_per_person_status"] == "derived" and row["gdp_per_person"]
                                and row["renewables_status"] == "missing"
                                and row["pair_exclusion_reasons"] == "renewables_missing" for row in later))
            replay = Path(temporary) / "replay"
            second = self.run_cli(self.arguments(output / "inputs", replay))
            self.assertEqual(second.returncode, 0, second.stderr)
            for name in ("study.csv", "summary.json"):
                self.assertEqual((replay / name).read_bytes(), (output / name).read_bytes())

    def test_existing_destination_and_research_notes_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "research"
            output.mkdir()
            note = output / "notes.txt"
            note.write_text("Unrelated research must be preserved.\n")
            result = self.run_cli(self.arguments(EXAMPLE / "inputs", output))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("Output already exists", result.stderr)
            self.assertEqual(note.read_text(), "Unrelated research must be preserved.\n")
            self.assertEqual(list(output.iterdir()), [note])

    def test_changed_source_bytes_fail_before_creating_any_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            inputs = Path(temporary) / "inputs"
            shutil.copytree(EXAMPLE / "inputs", inputs)
            raw = next((inputs / "population/raw").glob("*.json"))
            raw.write_bytes(raw.read_bytes() + b" ")
            before = {path.relative_to(inputs): path.read_bytes() for path in inputs.rglob("*") if path.is_file()}
            output = Path(temporary) / "new-parent/study"
            result = self.run_cli(self.arguments(inputs, output))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.parent.exists())
            self.assertEqual(before, {path.relative_to(inputs): path.read_bytes()
                                      for path in inputs.rglob("*") if path.is_file()})

    def test_wrong_indicator_role_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "study"
            arguments = self.arguments(EXAMPLE / "inputs", output)
            arguments[arguments.index("--gdp-bundle") + 1] = str(EXAMPLE / "inputs/population")
            result = self.run_cli(arguments)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("NY.GDP.MKTP.KD", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
