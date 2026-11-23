"""Adversarial source/archive cases exercise the real ingestion boundaries."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.request import HTTPHandler, HTTPSHandler
from urllib.response import addinfourl

from eco_evidence.core import (
    BundleReader, EvidenceError, MAX_BYTES, MAX_CACHE_BYTES, MAX_MANIFEST_BYTES,
    NetworkReader, Query, collect, digest, export_bundle, read_bounded_file, request_url,
)
from test_core import MemoryReader, ROOT, data_url, synthetic_sources


def response(url, body, *, location=None):
    headers = Message()
    if location:
        headers["Location"] = location
    result = addinfourl(io.BytesIO(body), headers, url, 302 if location else 200)
    result.msg = "Found" if location else "OK"
    return result


class RedirectTests(unittest.TestCase):
    """Use urllib's real redirect machinery with an in-memory transport."""

    def test_foreign_insecure_and_credential_redirects_are_never_dispatched(self):
        original = data_url()
        destinations = (
            "https://outside.example/data",
            "http://api.worldbank.org/v2/data",
            "https://someone@api.worldbank.org/v2/data",
            "https://api.worldbank.org:444/v2/data",
        )
        for destination in destinations:
            calls = []
            responses = []
            def transport(_handler, request):
                calls.append(request.full_url)
                result = response(request.full_url, b"", location=destination)
                responses.append(result)
                return result
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as tmp:
                with patch.object(HTTPSHandler, "https_open", transport), patch.object(HTTPHandler, "http_open", transport):
                    with self.assertRaises(EvidenceError):
                        NetworkReader(tmp).get(original)
                self.assertEqual(calls, [original], "Rejected destination must receive no request.")
                self.assertTrue(all(item.closed for item in responses))
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_same_origin_redirect_succeeds_and_counts_against_request_budget(self):
        original = data_url()
        destination = "https://api.worldbank.org/v2/same-origin-response"
        body = json.dumps(synthetic_sources()[original]).encode()
        calls = []
        def transport(_handler, request):
            calls.append(request.full_url)
            return response(request.full_url, body, location=destination if request.full_url == original else None)
        with tempfile.TemporaryDirectory() as tmp, patch.object(HTTPSHandler, "https_open", transport):
            reader = NetworkReader(tmp)
            snapshot = reader.get(original)
            self.assertEqual(snapshot.body, body)
            self.assertEqual(snapshot.url, original)
            self.assertEqual(calls, [original, destination])
            self.assertEqual(reader.requests, 2)

    def test_redirect_cannot_send_a_request_beyond_the_total_budget(self):
        original = data_url()
        calls = []
        responses = []
        def transport(_handler, request):
            calls.append(request.full_url)
            result = response(request.full_url, b"", location="https://api.worldbank.org/v2/next")
            responses.append(result)
            return result
        with tempfile.TemporaryDirectory() as tmp, patch.object(HTTPSHandler, "https_open", transport):
            reader = NetworkReader(tmp)
            reader.requests = 29
            with self.assertRaisesRegex(EvidenceError, "budget"):
                reader.get(original)
            self.assertEqual(calls, [original])
            self.assertTrue(all(item.closed for item in responses))
            self.assertEqual(list(Path(tmp).iterdir()), [])


class LocalInputTests(unittest.TestCase):
    def test_duplicate_manifest_and_cache_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            shutil.copytree(ROOT / "examples/synthetic-population", bundle)
            manifest_path = bundle / "provenance.json"
            manifest_path.write_text(manifest_path.read_text().replace(
                '"data_origin": "synthetic_fixture"',
                '"data_origin": "synthetic_fixture", "data_origin": "synthetic_fixture"', 1))
            with self.assertRaises(EvidenceError):
                BundleReader(bundle)
            cache = Path(tmp) / "cache"
            url = data_url()
            body = json.dumps(synthetic_sources()[url]).encode()
            with patch("eco_evidence.core.open_official", lambda request, **_: response(request.full_url, body)):
                NetworkReader(cache).get(url)
            filename = cache / f"{digest(url.encode())}.json"
            field = '"url": ' + json.dumps(url)
            filename.write_text(filename.read_text().replace(field, field + ", " + field, 1))
            with patch("eco_evidence.core.open_official", side_effect=AssertionError("Network forbidden")):
                with self.assertRaises(EvidenceError):
                    NetworkReader(cache).get(url)

    def test_oversized_cache_manifest_and_raw_files_fail_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cache = base / "cache"
            cache.mkdir()
            cached_file = cache / f"{digest(data_url().encode())}.json"
            with cached_file.open("wb") as stream:
                stream.truncate(MAX_CACHE_BYTES + 1)
            with patch("eco_evidence.core.open_official", side_effect=AssertionError("Network forbidden")):
                with self.assertRaises(EvidenceError):
                    NetworkReader(cache).get(data_url())
            bundle = base / "bundle"
            shutil.copytree(ROOT / "examples/synthetic-population", bundle)
            manifest_path = bundle / "provenance.json"
            original_manifest = manifest_path.read_bytes()
            with manifest_path.open("wb") as stream:
                stream.truncate(MAX_MANIFEST_BYTES + 1)
            with self.assertRaises(EvidenceError):
                BundleReader(bundle)
            manifest_path.write_bytes(original_manifest)
            manifest = json.loads(original_manifest)
            raw = manifest["raw_sources"][0]
            with (bundle / raw["file"]).open("wb") as stream:
                stream.truncate(MAX_BYTES + 1)
            with self.assertRaises(EvidenceError):
                BundleReader(bundle).get(raw["url"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO behavior")
    def test_pipe_input_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "input-pipe"
            os.mkfifo(fifo)
            code = ("from eco_evidence.core import read_bounded_file, EvidenceError; import sys\n"
                    "try: read_bounded_file(sys.argv[1], 100)\n"
                    "except EvidenceError: sys.exit(2)\n")
            result = subprocess.run([sys.executable, "-c", code, str(fifo)], cwd=ROOT,
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_regular_file_limit_includes_exact_boundary_and_directory_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            filename = Path(tmp) / "input"
            filename.write_bytes(b"12345")
            self.assertEqual(read_bounded_file(filename, 5), b"12345")
            with self.assertRaises(EvidenceError):
                read_bounded_file(filename, 4)
            with self.assertRaises((EvidenceError, OSError)):
                read_bounded_file(Path(tmp), 5)

    def test_failed_cache_write_or_replacement_preserves_previous_entry_and_cleans_temporary_file(self):
        url = data_url()
        body = json.dumps(synthetic_sources()[url]).encode()
        def transport(request, **_):
            return response(request.full_url, body)
        with tempfile.TemporaryDirectory() as tmp, patch("eco_evidence.core.open_official", transport):
            NetworkReader(tmp).get(url)
            cached_file = Path(tmp) / f"{digest(url.encode())}.json"
            before = cached_file.read_bytes()
            for target in ("eco_evidence.core.Path.replace", "eco_evidence.core.json.dump"):
                with self.subTest(failure=target), patch(target, side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        NetworkReader(tmp, refresh=True).get(url)
                self.assertEqual(cached_file.read_bytes(), before)
                self.assertEqual(list(Path(tmp).iterdir()), [cached_file])

    def test_unknown_bundle_schema_and_incomplete_source_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            shutil.copytree(ROOT / "examples/synthetic-population", bundle)
            filename = bundle / "provenance.json"
            original = json.loads(filename.read_bytes())
            for mutation in ("schema", "missing_hash", "foreign_url", "invalid_hash"):
                manifest = json.loads(json.dumps(original))
                if mutation == "schema": manifest["schema_version"] = 99
                if mutation == "missing_hash": del manifest["raw_sources"][0]["sha256"]
                if mutation == "foreign_url": manifest["raw_sources"][0]["url"] = "https://outside.example/data"
                if mutation == "invalid_hash": manifest["raw_sources"][0]["sha256"] = "hash?"
                filename.write_text(json.dumps(manifest))
                with self.subTest(mutation=mutation), self.assertRaises(EvidenceError):
                    BundleReader(bundle)

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "POSIX symlink behavior")
    def test_symlink_loops_have_clean_errors_on_all_supported_python_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            shutil.copytree(ROOT / "examples/synthetic-population", bundle)
            raw = bundle / "raw/001.json"
            raw.unlink()
            raw.symlink_to(raw.name)
            result = subprocess.run([
                sys.executable, "-m", "eco_evidence", "fetch", "--indicator", "population", "--countries", "USA",
                "--start-year", "2020", "--end-year", "2022", "--offline", str(bundle),
                "--output", str(Path(tmp) / "output"),
            ], cwd=ROOT, capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((Path(tmp) / "output").exists())
            cycle = Path(tmp) / "cycle"
            cycle.symlink_to(cycle.name)
            with self.assertRaises(EvidenceError):
                BundleReader(cycle)


class SourceContractTests(unittest.TestCase):
    def test_exact_unreported_empty_envelope_preserves_unknown_revision_raw_bytes_and_full_grid(self):
        query = Query("population", ("USA", "IND"), 2020, 2022)
        sources = synthetic_sources()
        country = sources.pop(request_url("country/USA", {}, 1))
        country[0]["total"] = 2
        country[1].append({"id": "IND", "name": "Synthetic India", "region": {"id": "SAS"}})
        sources[request_url("country/IND;USA", {}, 1)] = country
        empty_url = request_url("country/IND;USA/indicator/SP.POP.TOTL", {"source": 2, "date": "2020:2022"}, 1)
        sources[empty_url] = [{"page": 0, "pages": 0, "per_page": 0, "total": 0,
                               "sourceid": None, "lastupdated": None}, None]
        reader = MemoryReader(sources)
        rows, provenance, snapshots = collect(query, reader)
        self.assertEqual([(row["country_code"], row["year"]) for row in rows],
                         [(country, year) for country in ("IND", "USA") for year in range(2020, 2023)])
        self.assertTrue(all(row["value"] == "" and row["status"] == "not_returned" for row in rows))
        self.assertEqual(provenance["counts"], {"available": 0, "missing": 0, "not_returned": 6})
        self.assertEqual(provenance["source"]["id"], "2")
        self.assertIsNone(provenance["source"]["last_updated"])
        self.assertEqual(len(snapshots), 3)
        expected_raw = reader.get(empty_url).body
        self.assertEqual(snapshots[-1].body, expected_raw)
        with tempfile.TemporaryDirectory() as tmp:
            first, replay = Path(tmp) / "first", Path(tmp) / "replay"
            manifest = export_bundle(query, MemoryReader(sources), first)
            self.assertIsNone(json.loads((first / "provenance.json").read_bytes())["source"]["last_updated"])
            self.assertEqual((first / manifest["raw_sources"][-1]["file"]).read_bytes(), expected_raw)
            result = subprocess.run([
                sys.executable, "-m", "eco_evidence", "fetch", "--indicator", "population",
                "--countries", "USA,IND", "--start-year", "2020", "--end-year", "2022",
                "--offline", str(first), "--output", str(replay),
            ], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((first / "data.csv").read_bytes(), (replay / "data.csv").read_bytes())
            replay_manifest = json.loads((replay / "provenance.json").read_bytes())
            self.assertIsNone(replay_manifest["source"]["last_updated"])
            self.assertEqual(replay_manifest["raw_sources"], manifest["raw_sources"])

    def test_partial_or_late_empty_markers_are_rejected(self):
        empty = [{"page": 0, "pages": 0, "per_page": 0, "total": 0,
                  "sourceid": None, "lastupdated": None}, None]
        mutations = []
        for field in ("page", "pages", "per_page", "total"):
            for value in (1, False, "0"):
                changed = json.loads(json.dumps(empty))
                changed[0][field] = value
                mutations.append((f"{field}={value!r}", changed))
        for field, value in (("sourceid", "2"), ("lastupdated", "2026-11-22"), ("error", "unavailable")):
            changed = json.loads(json.dumps(empty))
            changed[0][field] = value
            mutations.append((field, changed))
        for field in ("sourceid", "lastupdated"):
            changed = json.loads(json.dumps(empty))
            del changed[0][field]
            mutations.append((f"missing {field}", changed))
        for data in ([], synthetic_sources()[data_url()][1]):
            mutations.append((f"records={data!r}", [empty[0], data]))
        for case, payload in mutations:
            reader = MemoryReader()
            reader.sources[data_url()] = payload
            with self.subTest(case=case), self.assertRaises(EvidenceError):
                collect(Query("population", ("USA",), 2020, 2022), reader)
        reader = MemoryReader()
        reader.sources[data_url(2)] = empty
        with self.assertRaises(EvidenceError):
            collect(Query("population", ("USA",), 2020, 2022), reader)

    def test_empty_observations_still_require_valid_country_and_indicator_metadata(self):
        for case in ("country", "indicator", "source", "unit"):
            reader = MemoryReader()
            reader.sources[data_url()] = [{"page": 0, "pages": 0, "per_page": 0, "total": 0,
                                           "sourceid": None, "lastupdated": None}, None]
            definition = reader.sources[request_url("indicator/SP.POP.TOTL", {"source": 2}, 1)][1][0]
            if case == "country": reader.sources[request_url("country/USA", {}, 1)][1][0]["id"] = "ZZZ"
            if case == "indicator": definition["id"] = "WRONG"
            if case == "source": definition["source"]["id"] = "1"
            if case == "unit": definition["unit"] = "thousands"
            with self.subTest(case=case), self.assertRaises(EvidenceError):
                collect(Query("population", ("USA",), 2020, 2022), reader)

    def test_unit_changes_are_rejected_even_when_indicator_names_stay_the_same(self):
        for location in ("metadata", "observation"):
            for value in ("thousands", None):
                reader = MemoryReader()
                url = request_url("indicator/SP.POP.TOTL", {"source": 2}, 1) if location == "metadata" else data_url()
                reader.sources[url][1][0]["unit"] = value
                with self.subTest(location=location, unit=value), self.assertRaises(EvidenceError):
                    collect(Query("population", ("USA",), 2020, 2022), reader)

    def test_revision_dates_must_be_real_calendar_dates_and_remain_source_values(self):
        for value in (None, {"unexpected": "date"}, 1.5, "2026-02-31", "2026-1-1", "2026-11-22T00:00:00Z"):
            reader = MemoryReader()
            for page in (1, 2):
                reader.sources[data_url(page)][0]["lastupdated"] = value
            with self.subTest(value=value), self.assertRaises(EvidenceError):
                collect(Query("population", ("USA",), 2020, 2022), reader)
        reader = MemoryReader()
        for page in (1, 2):
            reader.sources[data_url(page)][0]["lastupdated"] = "2024-02-29"
        _, provenance, _ = collect(Query("population", ("USA",), 2020, 2022), reader)
        self.assertEqual(provenance["source"]["last_updated"], "2024-02-29")

    def test_region_identity_must_be_text(self):
        reader = MemoryReader()
        reader.sources[request_url("country/USA", {}, 1)][1][0]["region"]["id"] = ["NAC"]
        with self.assertRaises(EvidenceError):
            collect(Query("population", ("USA",), 2020, 2022), reader)


class MalformedSourceCLITests(unittest.TestCase):
    def test_ambiguous_numbers_identity_and_unicode_have_clean_cli_failures(self):
        cases = ("extreme-exponent", "unhashable-country", "duplicate-value", "invalid-unicode", "oversized-integer")
        with tempfile.TemporaryDirectory() as tmp:
            for case in cases:
                bundle = Path(tmp) / case
                shutil.copytree(ROOT / "examples/synthetic-population", bundle)
                manifest = json.loads((bundle / "provenance.json").read_text())
                if case == "invalid-unicode":
                    source = manifest["raw_sources"][0]
                else:
                    source = next(s for s in manifest["raw_sources"] if "/country/" in s["url"] and "/indicator/" in s["url"])
                filename = bundle / source["file"]
                payload = json.loads(filename.read_bytes())
                if case == "unhashable-country": payload[1][0]["countryiso3code"] = []
                if case == "invalid-unicode": payload[1][0]["name"] = "broken\ud800text"
                if case == "oversized-integer": payload[0]["per_page"] = "9" * 5000
                raw = json.dumps(payload)
                if case == "extreme-exponent": raw = raw.replace('"value": 0', '"value": 1e9999999999999999999', 1)
                if case == "duplicate-value": raw = raw.replace('"value": 0', '"value": 0, "value": 99', 1)
                filename.write_text(raw)
                source["sha256"] = digest(filename.read_bytes())
                (bundle / "provenance.json").write_text(json.dumps(manifest))
                output = Path(tmp) / (case + "-out")
                result = subprocess.run([
                    sys.executable, "-m", "eco_evidence", "fetch", "--indicator", "population", "--countries", "USA",
                    "--start-year", "2020", "--end-year", "2022", "--offline", str(bundle), "--output", str(output),
                ], cwd=ROOT, capture_output=True, text=True)
                with self.subTest(case=case):
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("eco-evidence:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
