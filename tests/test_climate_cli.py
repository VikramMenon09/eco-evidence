"""Normal CLI acceptance against the attributed NOAA archive, without network."""

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
EXAMPLE = ROOT / "examples/noaa-global-co2"


class ClimateCLITests(unittest.TestCase):
    def arguments(self, archive, output):
        return ["climate", "--start-year", "2020", "--end-year", "2026",
                "--offline", str(archive), "--output", str(output)]

    def run_cli(self, arguments):
        return subprocess.run([sys.executable, "-m", "eco_evidence", *arguments],
                              cwd=ROOT, text=True, capture_output=True, timeout=30)

    def test_real_archive_replays_exact_values_uncertainties_and_absent_year(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "climate"
            result = self.run_cli(self.arguments(EXAMPLE, output))
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["rows"], 7)
            self.assertEqual(summary["counts"], {"available": 6, "not_returned": 1})
            self.assertEqual(summary["data_origin"], "noaa_gml")
            content = (output / "data.csv").read_bytes()
            self.assertEqual(content, (EXAMPLE / "data.csv").read_bytes())
            with (output / "data.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([(row["year"], row["co2_mean"], row["co2_uncertainty"]) for row in rows],
                             [("2020", "412.44", "0.07"), ("2021", "414.70", "0.06"),
                              ("2022", "417.08", "0.09"), ("2023", "419.35", "0.10"),
                              ("2024", "422.79", "0.09"), ("2025", "425.63", "0.05"),
                              ("2026", "", "")])
            self.assertTrue(all(row["status"] == "available" for row in rows[:-1]))
            self.assertEqual(rows[-1]["status"], "not_returned")
            self.assertTrue(all(row["unit"] == "ppm" and row["spatial_scope"] == "global_marine_surface"
                                and row["data_origin"] == "noaa_gml" for row in rows))
            replay = Path(temporary) / "detached-replay"
            copied = Path(temporary) / "copied-archive"
            shutil.copytree(output, copied)
            shutil.rmtree(output)
            second = self.run_cli(self.arguments(copied, replay))
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual((replay / "data.csv").read_bytes(), content)
            original = json.loads((EXAMPLE / "provenance.json").read_text())
            final = json.loads((replay / "provenance.json").read_text())
            self.assertEqual(final["raw_sources"], original["raw_sources"])

    def test_committed_noaa_archive_checksums_match_every_recorded_file(self):
        manifest = json.loads((EXAMPLE / "provenance.json").read_text())
        self.assertEqual(hashlib.sha256((EXAMPLE / "data.csv").read_bytes()).hexdigest(),
                         manifest["data_csv_sha256"])
        sources = manifest["raw_sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual({source["file"] for source in sources},
                         {str(path.relative_to(EXAMPLE)) for path in (EXAMPLE / "raw").iterdir()})
        for source in sources:
            self.assertEqual(hashlib.sha256((EXAMPLE / source["file"]).read_bytes()).hexdigest(), source["sha256"])

    def test_existing_research_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "notes"
            output.mkdir()
            (output / "research.txt").write_text("Earlier research must survive.\n")
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            result = self.run_cli(self.arguments(EXAMPLE, output))
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stdout)
            self.assertIn("Output already exists", result.stderr)
            self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, before)

    def test_modified_raw_data_fails_without_output_or_archive_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive"
            shutil.copytree(EXAMPLE, archive)
            source = next((archive / "raw").iterdir())
            source.write_bytes(source.read_bytes() + b"\n")
            before = {path.relative_to(archive): path.read_bytes()
                      for path in archive.rglob("*") if path.is_file()}
            output = Path(temporary) / "new-parent/replay"
            result = self.run_cli(self.arguments(archive, output))
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stdout)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.parent.exists())
            self.assertEqual(before, {path.relative_to(archive): path.read_bytes()
                                      for path in archive.rglob("*") if path.is_file()})

    def test_archive_query_and_world_bank_bundles_cannot_be_substituted(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "rejected"
            mismatched = self.arguments(EXAMPLE, output)
            mismatched[mismatched.index("--start-year") + 1] = "2021"
            cases = [mismatched,
                     self.arguments(ROOT / "examples/synthetic-population", output),
                     ["panel", "--bundle", str(EXAMPLE), "--bundle",
                      str(ROOT / "examples/synthetic-population"), "--output", str(output)]]
            for arguments in cases:
                with self.subTest(command=arguments[0]):
                    result = self.run_cli(arguments)
                    self.assertEqual(result.returncode, 2)
                    self.assertFalse(result.stdout)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
