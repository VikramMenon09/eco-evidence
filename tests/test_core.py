"""Offline regression suite. Every numeric fixture here is synthetic."""

from copy import deepcopy
import csv
from datetime import datetime, timezone
from decimal import Decimal
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from eco_evidence.core import (BundleReader, EvidenceError, MAX_BYTES, NetworkReader, Query,
                               Snapshot, collect, decode_json, digest, export_bundle, request_url)
from eco_evidence.registry import get_indicator

STAMP = "2026-11-22T12:00:00Z"
ROOT = Path(__file__).resolve().parents[1]


def synthetic_sources():
    """Four pages: a reported zero, a null, and an omitted year. Not real data."""
    def page(items, number=1, pages=1, total=None, per_page=1000, data=False):
        meta = {"page": number, "pages": pages, "total": len(items) if total is None else total, "per_page": per_page}
        if data:
            meta.update({"sourceid": "2", "lastupdated": "2026-11-22"})
        return [meta, items]
    country = {"id": "USA", "name": "Synthetic example, economy", "region": {"id": "NAC"}}
    indicator = get_indicator("population")
    definition = {"id": indicator.code, "name": indicator.name, "source": {"id": "2"}, "unit": "",
                  "sourceNote": "Synthetic fixture metadata; not an official source response.",
                  "sourceOrganization": "EcoEvidence test fixture authors"}
    def observation(year, value):
        return {"countryiso3code": "USA", "date": str(year), "indicator": {"id": indicator.code, "value": indicator.name},
                "value": value, "obs_status": "", "unit": ""}
    params = {"source": 2, "date": "2020:2022"}
    return {
        request_url("country/USA", {}, 1): page([country]),
        request_url(f"indicator/{indicator.code}", {"source": 2}, 1): page([definition]),
        request_url(f"country/USA/indicator/{indicator.code}", params, 1): page([observation(2022, 0)], 1, 2, 2, 1, True),
        request_url(f"country/USA/indicator/{indicator.code}", params, 2): page([observation(2021, None)], 2, 2, 2, 1, True),
    }


class MemoryReader:
    def __init__(self, sources=None):
        self.sources = deepcopy(sources if sources is not None else synthetic_sources())
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return Snapshot(url, json.dumps(self.sources[url]).encode(), STAMP, "synthetic_fixture")


def data_url(page=1):
    return request_url("country/USA/indicator/SP.POP.TOTL", {"source": 2, "date": "2020:2022"}, page)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.query = Query("population", ("USA",), 2020, 2022)

    def test_query_validation_and_normalization(self):
        query = Query("gdp", ("USA", "IND"), 2020, 2022)
        self.assertEqual(query.countries, ("IND", "USA"))
        self.assertEqual(query.indicator, "NY.GDP.MKTP.KD")
        invalid = [("unknown", ("USA",), 2020, 2022), ("gdp", ("usa",), 2020, 2022),
                   ("gdp", ("USA;IND",), 2020, 2022), ("gdp", ("USA", "USA"), 2020, 2022),
                   ("gdp", (), 2020, 2022), ("gdp", ("USA",), 1959, 2022),
                   ("gdp", ("USA",), 2022, 2020), ("gdp", ("USA",), True, 2022),
                   ("gdp", ("USA",), 2020, datetime.now(timezone.utc).year + 1)]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(EvidenceError):
                Query(*args)

    def test_pagination_and_zero_missing_omitted_distinction(self):
        reader = MemoryReader()
        rows, provenance, sources = collect(self.query, reader)
        self.assertEqual([(r["year"], r["value"], r["status"]) for r in rows],
                         [(2020, "", "not_returned"), (2021, "", "missing"), (2022, "0", "available")])
        self.assertEqual(len(reader.calls), 4)
        self.assertEqual(provenance["counts"], {"available": 1, "missing": 1, "not_returned": 1})
        self.assertTrue(all(r["unit"] == "people" and r["data_origin"] == "synthetic_fixture" for r in rows))
        self.assertTrue(all(source.retrieved_at == STAMP for source in sources))

    def test_decimal_preserved_without_binary_float_conversion(self):
        raw = b'[{"page":1},[{"value":1234567890.1234567890123456789}]]'
        self.assertEqual(str(decode_json(raw)[1][0]["value"]), "1234567890.1234567890123456789")
        for invalid in (b'{invalid', b'[NaN]', b'[Infinity]'):
            with self.assertRaises(EvidenceError):
                decode_json(invalid)
        with patch("eco_evidence.core.json.loads", side_effect=RecursionError("nested input")):
            with self.assertRaises(EvidenceError): decode_json(b"[]")

    def test_pagination_count_revision_and_schema_failures(self):
        for kind in ("total", "page", "revision", "null", "too_many", "schema"):
            reader = MemoryReader()
            if kind == "total": reader.sources[data_url(2)][0]["total"] = 3
            if kind == "page": reader.sources[data_url(2)][0]["page"] = 1
            if kind == "revision": reader.sources[data_url(2)][0]["lastupdated"] = "2026-11-23"
            if kind == "null": reader.sources[data_url(2)][1] = None
            if kind == "too_many": reader.sources[data_url()][0]["pages"] = 11
            if kind == "schema": reader.sources[data_url()] = [{"message": [{"key": "Invalid value"}]}]
            with self.subTest(kind=kind), self.assertRaises(EvidenceError):
                collect(self.query, reader)

    def test_invalid_observation_fields_and_duplicate_rows_fail(self):
        for key, value in (("countryiso3code", "IND"), ("date", "2023"), ("value", True),
                           ("value", "123"), ("indicator", {"id": "WRONG"}),
                           ("indicator", {"id": "SP.POP.TOTL", "value": "Population (thousands)"})):
            reader = MemoryReader()
            reader.sources[data_url()][1][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(EvidenceError):
                collect(self.query, reader)
        reader = MemoryReader()
        reader.sources[data_url(2)][1][0]["date"] = "2022"
        with self.assertRaises(EvidenceError): collect(self.query, reader)
        reader = MemoryReader()
        del reader.sources[data_url()][1][0]["value"]
        with self.assertRaises(EvidenceError): collect(self.query, reader)

    def test_country_aggregate_unknown_and_indicator_unit_drift_fail(self):
        for kind in ("aggregate", "unknown", "indicator"):
            reader = MemoryReader()
            if kind == "aggregate": reader.sources[request_url("country/USA", {}, 1)][1][0]["region"]["id"] = "NA"
            if kind == "unknown": reader.sources[request_url("country/USA", {}, 1)][1][0]["id"] = "ZZZ"
            if kind == "indicator": reader.sources[request_url("indicator/SP.POP.TOTL", {"source": 2}, 1)][1][0]["name"] = "Population (thousands)"
            with self.subTest(kind=kind), self.assertRaises(EvidenceError):
                collect(self.query, reader)

    def test_empty_observation_response_exports_explicit_not_returned_rows(self):
        reader = MemoryReader()
        reader.sources[data_url()] = [{"page": 1, "pages": 0, "per_page": 1000, "total": 0,
                                       "sourceid": "2", "lastupdated": "2026-11-22"}, None]
        rows, _, _ = collect(self.query, reader)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["status"] == "not_returned" for row in rows))

    def test_bundle_csv_hash_provenance_and_offline_replay_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "first", Path(tmp) / "second"
            provenance = export_bundle(self.query, MemoryReader(), first)
            csv_bytes = (first / "data.csv").read_bytes()
            self.assertEqual(digest(csv_bytes), provenance["csv_sha256"])
            parsed = list(csv.DictReader(io.StringIO(csv_bytes.decode())))
            self.assertEqual(len(parsed), provenance["row_count"])
            self.assertEqual(parsed[-1]["value"], "0")
            self.assertEqual(parsed[0]["country_name"], "Synthetic example, economy")
            for raw in provenance["raw_sources"]:
                self.assertEqual(digest((first / raw["file"]).read_bytes()), raw["sha256"])
            with patch("eco_evidence.core.urlopen", side_effect=AssertionError("Network forbidden")):
                replay = export_bundle(self.query, BundleReader(first), second)
            self.assertEqual(csv_bytes, (second / "data.csv").read_bytes())
            self.assertEqual(replay["raw_sources"], provenance["raw_sources"])
            with self.assertRaises(EvidenceError): export_bundle(self.query, MemoryReader(), first)

    def test_corrupt_offline_source_and_escape_path_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            export_bundle(self.query, MemoryReader(), bundle)
            (bundle / "raw/001.json").write_text("[]")
            with self.assertRaises(EvidenceError): collect(self.query, BundleReader(bundle))
            manifest = json.loads((bundle / "provenance.json").read_text())
            outside = Path(tmp) / "outside.json"
            original = MemoryReader().get(request_url("country/USA", {}, 1)).body
            outside.write_bytes(original)
            manifest["raw_sources"][0]["file"] = "../outside.json"
            manifest["raw_sources"][0]["sha256"] = digest(original)
            (bundle / "provenance.json").write_text(json.dumps(manifest))
            with self.assertRaises(EvidenceError): collect(self.query, BundleReader(bundle))

    def test_failed_fetch_does_not_leave_partial_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result"
            reader = MemoryReader()
            reader.sources[data_url(2)][0]["total"] = 999
            with self.assertRaises(EvidenceError): export_bundle(self.query, reader, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])
            with patch("eco_evidence.core.Path.write_bytes", side_effect=OSError("disk full")):
                with self.assertRaises(OSError): export_bundle(self.query, MemoryReader(), output)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])


class NetworkTests(unittest.TestCase):
    def test_http_errors_timeouts_and_request_budget(self):
        url = request_url("country/USA", {}, 1)
        for error in (HTTPError(url, 429, "rate limit", None, None), URLError("offline"),
                      TimeoutError("timeout"), IncompleteRead(b"truncated", 100)):
            with tempfile.TemporaryDirectory() as tmp, patch("eco_evidence.core.urlopen", side_effect=error):
                with self.assertRaises(EvidenceError): NetworkReader(tmp).get(url)
                self.assertEqual(list(Path(tmp).iterdir()), [])
        with tempfile.TemporaryDirectory() as tmp:
            reader = NetworkReader(tmp)
            reader.requests = 30
            with self.assertRaises(EvidenceError): reader.get(url)
            with self.assertRaises(EvidenceError): reader.get("http://api.worldbank.org/v2/country/USA")

    def test_verified_cache_reuses_original_timestamp_without_network(self):
        url = data_url()
        body = json.dumps(synthetic_sources()[url]).encode()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def geturl(self): return url
            def read(self, _): return body
        with tempfile.TemporaryDirectory() as tmp:
            with patch("eco_evidence.core.urlopen", return_value=Response()):
                first = NetworkReader(tmp).get(url)
            with patch("eco_evidence.core.urlopen", side_effect=AssertionError("Network forbidden")):
                second = NetworkReader(tmp).get(url)
            self.assertEqual(first, second)
            cache_file = next(Path(tmp).iterdir())
            entry = json.loads(cache_file.read_text())
            entry["sha256"] = "0" * 64
            cache_file.write_text(json.dumps(entry))
            with self.assertRaises(EvidenceError): NetworkReader(tmp).get(url)

    def test_online_metadata_is_refreshed_even_when_cached(self):
        url = request_url("indicator/SP.POP.TOTL", {"source": 2}, 1)
        payload = synthetic_sources()[url]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def geturl(self): return url
            def read(self, _): return json.dumps(payload).encode()
        with tempfile.TemporaryDirectory() as tmp, patch("eco_evidence.core.urlopen", return_value=Response()) as network:
            first = NetworkReader(tmp).get(url)
            payload[1][0]["name"] = "Population (thousands)"
            second = NetworkReader(tmp).get(url)
            self.assertNotEqual(first.body, second.body)
            self.assertEqual(network.call_count, 2)

    def test_deep_cache_and_offline_manifest_fail_without_recursion_traceback(self):
        nested = '[' * 10000 + ']' * 10000
        with tempfile.TemporaryDirectory() as tmp:
            url = data_url()
            (Path(tmp) / f"{digest(url.encode())}.json").write_text(nested)
            with self.assertRaises(EvidenceError): NetworkReader(tmp).get(url)
            (Path(tmp) / "provenance.json").write_text(nested)
            with self.assertRaises(EvidenceError): BundleReader(tmp)

    def test_redirected_response_is_rejected(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def geturl(self): return "https://example.org/unexpected"
        with tempfile.TemporaryDirectory() as tmp, patch("eco_evidence.core.urlopen", return_value=Response()):
            with self.assertRaises(EvidenceError): NetworkReader(tmp).get(data_url())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_response_limits_timestamp_and_timeout_validation(self):
        with self.assertRaises(EvidenceError): Snapshot("u", b"x" * (MAX_BYTES + 1), STAMP)
        with self.assertRaises(EvidenceError): Snapshot("u", b"[]", "2026-11-22")
        for timeout in (0, 31, float("nan"), float("inf"), True):
            with self.assertRaises(EvidenceError): NetworkReader("unused", timeout=timeout)


class CLITests(unittest.TestCase):
    def test_real_entrypoint_indicators_offline_success_and_invalid_year(self):
        listed = subprocess.run([sys.executable, "-m", "eco_evidence", "indicators"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("NY.GDP.MKTP.KD", listed.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "fixture"
            export_bundle(Query("population", ("USA",), 2020, 2022), MemoryReader(), fixture)
            args = [sys.executable, "-m", "eco_evidence", "fetch", "--indicator", "population", "--countries", "usa",
                    "--start-year", "2020", "--end-year", "2022", "--offline", str(fixture), "--output", str(Path(tmp) / "run")]
            success = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(json.loads(success.stdout)["data_origin"], "synthetic_fixture")
            args[args.index("2020")] = "1800"
            failure = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(failure.returncode, 2)
            self.assertIn("Years must", failure.stderr)
            self.assertNotIn("Traceback", failure.stderr)


if __name__ == "__main__":
    unittest.main()
