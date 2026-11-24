"""Public panel command checks using explicitly invented, archived examples."""

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POPULATION = ROOT / "examples/synthetic-population"
GDP = ROOT / "examples/synthetic-gdp"


def panel_cli(output, *inputs):
    arguments = [sys.executable, "-m", "eco_evidence", "panel", "--output", str(output)]
    for bundle in inputs:
        arguments.extend(("--bundle", str(bundle)))
    return subprocess.run(arguments, cwd=ROOT, text=True, capture_output=True, timeout=20)


def fingerprints(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


class PanelCLITests(unittest.TestCase):
    def test_cli_reports_coverage_and_replays_from_embedded_inputs(self):
        originals = [fingerprints(path) for path in (POPULATION, GDP)]
        with tempfile.TemporaryDirectory() as temporary:
            output, replay = Path(temporary) / "panel", Path(temporary) / "replay"
            result = panel_cli(output, POPULATION, GDP)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary, {"output": str(output.resolve()), "rows": 7,
                                      "complete_cases": 1, "indicators": ["gdp", "population"],
                                      "data_origin": "synthetic_fixture"})
            with (output / "panel.csv").open(encoding="utf-8", newline="") as handle:
                rows = {(row["country_code"], row["year"]): row for row in csv.DictReader(handle)}
            self.assertEqual(rows[("USA", "2022")]["gdp_value"], "12345.67890123456789")
            self.assertEqual(rows[("USA", "2022")]["population_value"], "0")
            self.assertEqual(rows[("USA", "2020")]["gdp_status"], "not_requested")
            self.assertEqual(rows[("IND", "2023")]["gdp_status"], "not_returned")
            self.assertEqual(rows[("USA", "2021")]["gdp_status"], "missing")
            self.assertEqual(rows[("IND", "2021")]["population_status"], "not_requested")
            rerun = panel_cli(replay, output / "inputs/gdp", output / "inputs/population")
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            for filename in ("panel.csv", "coverage.json"):
                self.assertEqual((replay / filename).read_bytes(), (output / filename).read_bytes())
        self.assertEqual(originals, [fingerprints(path) for path in (POPULATION, GDP)])

    def test_invalid_input_sets_fail_cleanly_without_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            for index, inputs in enumerate(((POPULATION,), (POPULATION, POPULATION),
                                            (POPULATION, Path(temporary) / "absent"))):
                with self.subTest(inputs=inputs):
                    output = Path(temporary) / f"rejected-{index}"
                    result = panel_cli(output, *inputs)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse(result.stdout)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse(output.exists())
                    self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_existing_research_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "earlier-research"
            output.mkdir()
            (output / "notes.txt").write_text("Keep this earlier analysis.\n", encoding="utf-8")
            before = fingerprints(output)
            result = panel_cli(output, POPULATION, GDP)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Output already exists", result.stderr)
            self.assertFalse(result.stdout)
            self.assertEqual(before, fingerprints(output))


if __name__ == "__main__":
    unittest.main()
