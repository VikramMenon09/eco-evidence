"""Independent replay/producer checks; all data comes from archived fixtures."""

import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from eco_evidence import __version__
from eco_evidence.core import BundleReader, EvidenceError, Query, digest, export_bundle, request_url
from test_core import MemoryReader, ROOT, data_url


def cli_fetch(bundle, output, *, indicator="population", countries="USA", start="2020", end="2022"):
    return subprocess.run([
        sys.executable, "-m", "eco_evidence", "fetch", "--indicator", indicator,
        "--countries", countries, "--start-year", start, "--end-year", end,
        "--offline", str(bundle), "--output", str(output),
    ], cwd=ROOT, capture_output=True, text=True, timeout=10)


def file_fingerprints(directory):
    return {str(path.relative_to(directory)): digest(path.read_bytes())
            for path in directory.rglob("*") if path.is_file()}


class ProducerConsumerTests(unittest.TestCase):
    def test_export_never_publishes_a_manifest_that_its_reader_cannot_accept(self):
        reader = MemoryReader()
        definition_url = request_url("indicator/SP.POP.TOTL", {"source": 2}, 1)
        reader.sources[definition_url][1][0]["sourceNote"] = "Synthetic oversized definition. " + "x" * 1_100_000
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bundle"
            with self.assertRaises(EvidenceError):
                export_bundle(Query("population", ("USA",), 2020, 2022), reader, output)
            self.assertFalse(output.exists(), "Rejected provenance must not become a final bundle.")
            self.assertEqual(list(Path(tmp).iterdir()), [], "Failed export must remove its staging directory.")

    def test_repeated_csv_text_is_bounded_without_truncating_source_values(self):
        for field in ("country_name", "observation_status"):
            reader = MemoryReader()
            if field == "country_name":
                reader.sources[request_url("country/USA", {}, 1)][1][0]["name"] = "Synthetic " + "x" * 32_000
            else:
                reader.sources[data_url()][1][0]["obs_status"] = "x" * 32_000
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "bundle"
                with self.assertRaises(EvidenceError):
                    export_bundle(Query("population", ("USA",), 2020, 2022), reader, output)
                self.assertFalse(output.exists())

    def test_large_nonrepeated_definition_and_international_csv_text_round_trip(self):
        reader = MemoryReader()
        name = 'Synthetic economy, "Δ"\nsecond line'
        definition = "Synthetic detailed definition. " + "x" * 32_000
        reader.sources[request_url("country/USA", {}, 1)][1][0]["name"] = name
        reader.sources[request_url("indicator/SP.POP.TOTL", {"source": 2}, 1)][1][0]["sourceNote"] = definition
        query = Query("population", ("USA",), 2020, 2022)
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "first", Path(tmp) / "second"
            export_bundle(query, reader, first)
            export_bundle(query, BundleReader(first), second)
            self.assertEqual((first / "data.csv").read_bytes(), (second / "data.csv").read_bytes())
            rows = list(csv.DictReader(io.StringIO((second / "data.csv").read_text())))
            self.assertTrue(all(row["country_name"] == name for row in rows))
            self.assertEqual([row["status"] for row in rows], ["not_returned", "missing", "available"])
            self.assertEqual(rows[-1]["value"], "0")
            self.assertEqual(json.loads((second / "provenance.json").read_text())["indicator"]["source_note"], definition)


class ArchivedCLITests(unittest.TestCase):
    def test_raw_checksum_rejects_changed_valid_data_before_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "input"
            shutil.copytree(ROOT / "examples/synthetic-population", bundle)
            manifest_path = bundle / "provenance.json"
            manifest = json.loads(manifest_path.read_text())
            source = next(item for item in manifest["raw_sources"]
                          if "/country/" in item["url"] and "/indicator/" in item["url"])
            raw = bundle / source["file"]
            self.assertEqual(digest(raw.read_bytes()), source["sha256"])
            payload = json.loads(raw.read_bytes())
            self.assertEqual(payload[1][0]["value"], 0)
            payload[1][0]["value"] = 17
            raw.write_text(json.dumps(payload))
            self.assertNotEqual(digest(raw.read_bytes()), source["sha256"])

            rejected = Path(tmp) / "rejected-output"
            failure = cli_fetch(bundle, rejected)
            self.assertEqual(failure.returncode, 2, failure.stderr)
            self.assertNotIn("Traceback", failure.stderr)
            self.assertFalse(rejected.exists())

            # Positive control: the changed value is valid source data. Only its
            # mismatch with the preserved checksum must prevent the first run.
            source["sha256"] = digest(raw.read_bytes())
            manifest_path.write_text(json.dumps(manifest))
            accepted = Path(tmp) / "accepted-output"
            success = cli_fetch(bundle, accepted)
            self.assertEqual(success.returncode, 0, success.stderr)
            with (accepted / "data.csv").open(newline="") as stream:
                row = next(row for row in csv.DictReader(stream) if row["year"] == "2022")
            self.assertEqual((row["value"], row["status"]), ("17", "available"))

    def test_historical_bundle_replays_identical_csv_with_current_tool_version(self):
        original = ROOT / "examples/population-2020-2022"
        before = file_fingerprints(original)
        source_manifest = json.loads((original / "provenance.json").read_text())
        self.assertEqual(source_manifest["tool_version"], "0.1.0", "This fixture is retained historical evidence.")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "replay"
            result = cli_fetch(original, output, countries="usa,ind")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["rows"], 6)
            self.assertEqual((output / "data.csv").read_bytes(), (original / "data.csv").read_bytes())
            replay_manifest = json.loads((output / "provenance.json").read_text())
            self.assertEqual(replay_manifest["tool_version"], __version__)
            self.assertEqual(replay_manifest["query"], source_manifest["query"])
            self.assertEqual(replay_manifest["raw_sources"], source_manifest["raw_sources"])
        self.assertEqual(file_fingerprints(original), before)

    def test_unarchived_query_fails_cleanly_and_preserves_every_input_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "input"
            shutil.copytree(ROOT / "examples/synthetic-population", bundle)
            before = file_fingerprints(bundle)
            for number, options in enumerate(({"countries": "IND"}, {"start": "2021"}, {"indicator": "gdp"})):
                output = Path(tmp) / f"output-{number}"
                result = cli_fetch(bundle, output, **options)
                with self.subTest(options=options):
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse(output.exists())
            self.assertEqual(file_fingerprints(bundle), before)

    def test_existing_output_is_preserved_including_unrelated_research_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "existing-output"
            output.mkdir()
            (output / "research-notes.txt").write_text("Do not replace this earlier research.\n")
            (output / "data.csv").write_text("existing,observations\n")
            before = file_fingerprints(output)
            result = cli_fetch(ROOT / "examples/synthetic-population", output)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(file_fingerprints(output), before)

    @unittest.skipUnless(hasattr(os, "symlink") and sys.platform != "win32", "POSIX symlink behavior")
    def test_correct_hash_cannot_make_an_escaping_symlink_an_allowed_archive_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "input"
            shutil.copytree(ROOT / "examples/synthetic-population", bundle)
            manifest = json.loads((bundle / "provenance.json").read_text())
            source = manifest["raw_sources"][0]
            original = bundle / source["file"]
            body = original.read_bytes()
            self.assertEqual(digest(body), source["sha256"])
            outside = Path(tmp) / "outside.json"
            outside.write_bytes(body)
            original.unlink()
            original.symlink_to(outside)
            output = Path(tmp) / "output"
            result = cli_fetch(bundle, output)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.exists())
            self.assertEqual(outside.read_bytes(), body)


if __name__ == "__main__":
    unittest.main()
