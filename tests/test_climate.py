"""Independent climate archive acceptance tests; all fixture values are invented."""

from copy import deepcopy
import csv
from datetime import datetime, timezone
from email.message import Message
from http.client import HTTPResponse, IncompleteRead
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, HTTPSHandler
from urllib.response import addinfourl

import eco_evidence.climate as climate_module
from eco_evidence.climate import ClimateQuery, ClimateSnapshot, SOURCE_URL, export_climate
from eco_evidence.core import EvidenceError, digest


STAMP = "2026-11-26T12:34:56Z"
CREATION = "Synthetic archive creation label 2026-11-25"
UNITS = "CO2 expressed as a mole fraction in dry air, micromol/mol, abbreviated as ppm"
# The source's reviewed method text is used solely to exercise its format contract.
# Every number and the remaining source context below are explicitly synthetic.
UNCERTAINTY = (
    "The uncertainty in the global annual mean is estimated using a monte carlo\n"
    "technique that computes 200 global annual averages, each time using a\n"
    "slightly different set of measurement records from the NOAA GML cooperative\n"
    "air sampling network.  The reported uncertainty is the mean of the standard\n"
    "deviations for each annual average using this technique. Please see\n"
    "Conway et al., 1994, JGR, vol. 99, no. D11. for a complete discussion."
)
REVISION = "NOTE: Synthetic fixture values may be revised; these are invented test values."
PRECISE = "401.123456789012345678901234567890123456789"
ROWS = f"1979 {PRECISE} 0.00\n1981 402.000 0.1250\n1982 403.50 0.20\n"


def synthetic_body(rows=ROWS, *, uncertainty=UNCERTAINTY, revision=REVISION):
    """A NOAA-shaped format fixture, never an atmospheric measurement."""
    comments = [
        "# Explicitly synthetic fixture. All observations are invented for testing.",
        "# Global annual mean CO2 for the synthetic global marine surface example.",
        "# " + UNITS,
        "# File Creation: " + CREATION,
        "#",
        *["# " + line for line in uncertainty.splitlines()],
        "#",
        *["# " + line for line in revision.splitlines()],
        "#",
        "# year mean unc",
    ]
    return ("\n".join(comments) + "\n" + rows).encode("utf-8")


def read_json(path):
    return json.loads(Path(path).read_bytes())


def file_snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in Path(root).rglob("*")}


def read_rows(directory):
    return list(csv.DictReader(io.StringIO((directory / "data.csv").read_text(encoding="utf-8"))))


def response(url, body, *, status=200, location=None):
    """A transport response consumed by urllib's genuine handler chain."""
    headers = Message()
    headers["Content-Length"] = str(len(body))
    if location is not None:
        headers["Location"] = location
    result = addinfourl(io.BytesIO(body), headers, url, status)
    result.msg = "Synthetic transport response"
    return result


def wire_response(body, headers):
    """Parse actual HTTP wire bytes with the standard library's body decoder."""
    wire = (b"HTTP/1.1 200 OK\r\n"
            + b"".join(f"{name}: {value}\r\n".encode("latin-1") for name, value in headers)
            + b"\r\n" + body)

    class FakeSocket:
        def makefile(self, mode):
            if mode != "rb":
                raise AssertionError("HTTPResponse must read the synthetic wire as bytes")
            return io.BytesIO(wire)

    result = HTTPResponse(FakeSocket(), method="GET")
    result.begin()
    result.url = SOURCE_URL
    return result


def chunked_body(parts, *, terminated=True):
    encoded = b"".join(f"{len(part):x}\r\n".encode("ascii") + part + b"\r\n" for part in parts)
    return encoded + (b"0\r\n\r\n" if terminated else b"")


class ClimateAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eco-climate-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.query = ClimateQuery(1979, 1981)

    def build(self, name="bundle", *, body=None, query=None, timeout=15):
        output = self.root / name
        snapshot = ClimateSnapshot(SOURCE_URL, body if body is not None else synthetic_body(),
                                   STAMP, "synthetic_fixture")
        with patch.object(climate_module, "_fetch_snapshot", return_value=snapshot) as fetch:
            provenance = export_climate(query or self.query, output, timeout=timeout)
        fetch.assert_called_once_with(timeout)
        return output, provenance

    def replay(self, source, name="replay", *, query=None, timeout=15):
        output = self.root / name
        with patch.object(climate_module, "_fetch_snapshot", side_effect=AssertionError("Network forbidden")), \
                patch.object(climate_module, "open_noaa", side_effect=AssertionError("Network forbidden")):
            provenance = export_climate(query or self.query, output, offline=source, timeout=timeout)
        return output, provenance

    def assert_no_output(self, output):
        self.assertFalse(output.exists())
        self.assertFalse(output.is_symlink())
        self.assertEqual(list(output.parent.glob(f".{output.name}-*")), [])

    def assert_body_rejected(self, body, name):
        output = self.root / name
        with self.assertRaises(EvidenceError):
            self.build(name, body=body)
        self.assert_no_output(output)

    def test_exact_values_uncertainty_and_requested_gaps_survive_export(self):
        output, provenance = self.build()
        rows = read_rows(output)
        self.assertEqual(list(rows[0]), ["year", "co2_mean", "co2_uncertainty", "unit", "status",
                                         "spatial_scope", "data_origin"])
        self.assertEqual(rows, [
            {"year": "1979", "co2_mean": PRECISE, "co2_uncertainty": "0.00", "unit": "ppm",
             "status": "available", "spatial_scope": "global_marine_surface", "data_origin": "synthetic_fixture"},
            {"year": "1980", "co2_mean": "", "co2_uncertainty": "", "unit": "ppm",
             "status": "not_returned", "spatial_scope": "global_marine_surface", "data_origin": "synthetic_fixture"},
            {"year": "1981", "co2_mean": "402.000", "co2_uncertainty": "0.1250", "unit": "ppm",
             "status": "available", "spatial_scope": "global_marine_surface", "data_origin": "synthetic_fixture"},
        ])
        self.assertEqual(provenance["query"], {"series": "co2_global_annual", "start_year": 1979, "end_year": 1981})
        self.assertEqual(provenance["counts"], {"available": 2, "not_returned": 1})
        self.assertEqual(provenance["row_count"], 3)
        self.assertEqual(provenance["data_origin"], "synthetic_fixture")
        self.assertEqual(provenance["data_csv_sha256"], digest((output / "data.csv").read_bytes()))
        self.assertEqual(read_json(output / "provenance.json"), provenance)

    def test_complete_source_coverage_metadata_and_raw_lineage_are_retained(self):
        body = synthetic_body()
        output, provenance = self.build(body=body)
        source = provenance["source"]
        self.assertEqual(source["parsed_coverage"], {"start_year": 1979, "end_year": 1982, "row_count": 3})
        self.assertEqual(source["source_file_creation"], CREATION)
        self.assertEqual(source["uncertainty_definition"], UNCERTAINTY)
        self.assertEqual(source["revision_caveat"], REVISION)
        self.assertEqual(source["citation_doi"], "https://doi.org/10.15138/9N0H-ZH07")
        self.assertEqual(source["header_comments"], [line for line in body.decode().splitlines() if line.startswith("#")])
        self.assertEqual(provenance["raw_sources"], [{
            "url": SOURCE_URL, "file": "raw/co2_annmean_gl.txt", "sha256": digest(body), "retrieved_at": STAMP,
        }])
        self.assertEqual((output / "raw/co2_annmean_gl.txt").read_bytes(), body)

    def test_whitespace_and_line_endings_do_not_round_or_normalize_values(self):
        mean = "0." + "0" * 61 + "1"
        self.assertEqual(len(mean), 64)
        body = synthetic_body(f"1979\t{mean}  000.000\n1981 000402.500 0\n")
        body = body.replace(b"# year mean unc", b"#\tyear\tmean   unc").replace(b"\n", b"\r\n")
        output, _ = self.build(body=body)
        rows = read_rows(output)
        self.assertEqual((rows[0]["co2_mean"], rows[0]["co2_uncertainty"]), (mean, "000.000"))
        self.assertEqual((rows[2]["co2_mean"], rows[2]["co2_uncertainty"]), ("000402.500", "0"))
        self.assertEqual((output / "raw/co2_annmean_gl.txt").read_bytes(), body)

    def test_completely_unreported_query_keeps_every_requested_year(self):
        output, provenance = self.build(query=ClimateQuery(1983, 1985))
        rows = read_rows(output)
        self.assertEqual([row["year"] for row in rows], ["1983", "1984", "1985"])
        self.assertTrue(all(row["status"] == "not_returned" and row["co2_mean"] == row["co2_uncertainty"] == ""
                            for row in rows))
        self.assertEqual(provenance["counts"], {"available": 0, "not_returned": 3})
        self.assertEqual(provenance["source"]["parsed_coverage"]["row_count"], 3)

    def test_query_year_types_ranges_and_canonical_series(self):
        current_year = datetime.now(timezone.utc).year
        self.assertEqual(ClimateQuery(1979, current_year).as_dict(),
                         {"series": "co2_global_annual", "start_year": 1979, "end_year": current_year})
        for start, end in ((1978, 1980), (1979, current_year + 1), (1981, 1980),
                           (True, 1980), (1979, False), (1979.0, 1980), (1979, "1980"), (None, 1980)):
            with self.subTest(start=start, end=end), self.assertRaises(EvidenceError):
                ClimateQuery(start, end)

    def test_timeout_is_validated_before_fetch_for_online_and_offline_runs(self):
        bundle, _ = self.build("input")
        invalid = (0, 31, True, False, "15", None, float("nan"), float("inf"), -float("inf"))
        with patch.object(climate_module, "_fetch_snapshot", side_effect=AssertionError("Fetch must not run")):
            for index, timeout in enumerate(invalid):
                for offline in (None, bundle):
                    output = self.root / f"invalid-timeout-{index}-{offline is None}"
                    with self.subTest(timeout=timeout, offline=offline is not None), self.assertRaises(EvidenceError):
                        export_climate(self.query, output, offline=offline, timeout=timeout)
                    self.assert_no_output(output)
        for timeout in (1, 1.5, 30):
            self.build(f"valid-timeout-{timeout}", timeout=timeout)

    def test_malformed_numeric_tokens_and_domain_violations_are_rejected(self):
        cases = (("0", "0.1"), ("-1", "0.1"), ("+1", "0.1"), ("1e2", "0.1"),
                 ("NaN", "0.1"), ("Infinity", "0.1"), ("４０１", "0.1"),
                 ("1" * 65, "0.1"), ("401", "-0.1"), ("401", "+0.1"),
                 ("401", "1E-2"), ("401", "NaN"), ("401", "0" * 65))
        for index, (mean, uncertainty) in enumerate(cases):
            with self.subTest(mean=mean, uncertainty=uncertainty):
                self.assert_body_rejected(synthetic_body(f"1979 {mean} {uncertainty}\n"), f"number-{index}")

    def test_every_row_is_validated_even_outside_the_requested_window(self):
        current_year = datetime.now(timezone.utc).year
        cases = [
            "1979 401 0.1\n1979 402 0.1\n", "1980 401 0.1\n1979 402 0.1\n",
            "1978 401 0.1\n", f"{current_year + 1} 401 0.1\n", "1979.0 401 0.1\n",
            "１９７９ 401 0.1\n", "1979 401\n", "1979 401 0.1 extra\n", "",
            "1979 401 0.1\n1985 -402 0.2\n", "1979 401 0.1\n# year mean unc\n1981 402 0.1\n",
        ]
        for index, rows in enumerate(cases):
            with self.subTest(rows=rows):
                self.assert_body_rejected(synthetic_body(rows), f"row-{index}")

    def test_every_data_row_needs_lf_or_crlf_in_online_and_checksum_valid_offline_input(self):
        source, _ = self.build("source")
        manifest_path = source / "provenance.json"
        manifest = read_json(manifest_path)
        cases = {
            "no-final-newline": ROWS.rstrip("\n"),
            "bare-carriage-return": ROWS.replace("\n", "\r"),
            "unicode-line-separator": ROWS.replace("\n", "\u2028"),
            "partial-final-uncertainty": ROWS[:-2],
        }
        for name, rows in cases.items():
            body = synthetic_body(rows)
            with self.subTest(case=name):
                self.assert_body_rejected(body, f"online-{name}")
                (source / "raw/co2_annmean_gl.txt").write_bytes(body)
                manifest["raw_sources"][0]["sha256"] = digest(body)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                before = file_snapshot(source)
                with self.assertRaises(EvidenceError):
                    self.replay(source, f"offline-{name}")
                self.assert_no_output(self.root / f"offline-{name}")
                self.assertEqual(file_snapshot(source), before)
        # A complete CRLF-terminated final row may be followed by blank spaces.
        body = synthetic_body().replace(b"\n", b"\r\n") + b" \t  "
        output, _ = self.build("terminated-plus-blank-space", body=body)
        self.assertEqual((output / "raw/co2_annmean_gl.txt").read_bytes(), body)

    def test_real_content_length_and_chunked_downloads_preserve_complete_source_bytes(self):
        body = synthetic_body()
        cases = (
            ("content-length", body, [("Content-Length", str(len(body)))]),
            ("leading-zero-length", body, [("Content-Length", f"00{len(body)}")]),
            ("chunked", chunked_body((body[:67], body[67:-9], body[-9:])), [("Transfer-Encoding", "chunked")]),
            ("uppercase-chunked", chunked_body((body,)), [("Transfer-Encoding", "CHUNKED")]),
        )
        for name, encoded, headers in cases:
            with self.subTest(framing=name):
                result = wire_response(encoded, headers)
                output = self.root / name
                with patch.object(climate_module, "open_noaa", return_value=result) as open_request:
                    provenance = export_climate(self.query, output)
                open_request.assert_called_once()
                self.assertTrue(result.closed)
                self.assertEqual((output / "raw/co2_annmean_gl.txt").read_bytes(), body)
                self.assertEqual(provenance["raw_sources"][0]["sha256"], digest(body))
                self.assertEqual(read_rows(output)[0]["co2_mean"], PRECISE)
                self.assertEqual(provenance["counts"], {"available": 2, "not_returned": 1})

    def test_real_content_length_truncation_rejects_whole_and_partial_last_rows(self):
        retained, _ = self.build("retained-input")
        before = file_snapshot(retained)
        body = synthetic_body("2024 422.1234 0.09\n2025 425.6000 0.05\n")
        prefix = b"# Synthetic transport-length padding: "
        body = prefix + b"." * (2914 - len(body) - len(prefix) - 1) + b"\n" + body
        self.assertEqual(len(body), 2914)
        cut_after_2024 = body[:body.index(b"2025 425.6000")]
        cut_inside_2025 = body[:-2]
        self.assertTrue(cut_after_2024.endswith(b"2024 422.1234 0.09\n"))
        self.assertTrue(cut_inside_2025.endswith(b"2025 425.6000 0.0"))
        for name, partial in (("after-2024-row", cut_after_2024), ("inside-2025-uncertainty", cut_inside_2025)):
            with self.subTest(cut=name):
                # Demonstrate the exact stdlib behavior behind B1: bounded
                # reads silently return EOF before the declared body length.
                probe = wire_response(partial, [("Content-Length", "2914")])
                try:
                    self.assertEqual(probe.read(1_000_001), partial)
                finally:
                    probe.close()
                result = wire_response(partial, [("Content-Length", "2914")])
                output = self.root / name
                with patch.object(climate_module, "open_noaa", return_value=result) as open_request:
                    with self.assertRaises(EvidenceError):
                        export_climate(ClimateQuery(2024, 2025), output)
                open_request.assert_called_once()
                self.assertTrue(result.closed)
                self.assert_no_output(output)
                self.assertEqual(file_snapshot(retained), before)

    def test_real_chunk_decoder_requires_terminal_chunk_and_complete_chunk_data(self):
        retained, _ = self.build("retained-input")
        before = file_snapshot(retained)
        body = synthetic_body()
        cases = {
            "missing-terminal-chunk": chunked_body((body,), terminated=False),
            "incomplete-chunk-data": f"{len(body):x}\r\n".encode("ascii") + body[:-2],
        }
        for name, encoded in cases.items():
            with self.subTest(case=name):
                result = wire_response(encoded, [("Transfer-Encoding", "chunked")])
                self.assertIs(result.chunked, True)
                output = self.root / name
                with patch.object(climate_module, "open_noaa", return_value=result) as open_request:
                    with self.assertRaises(EvidenceError) as failure:
                        export_climate(self.query, output)
                # The exception originates in the real decoder, not a mock.
                self.assertIsInstance(failure.exception.__cause__, IncompleteRead)
                open_request.assert_called_once()
                self.assertTrue(result.closed)
                self.assert_no_output(output)
                self.assertEqual(file_snapshot(retained), before)

    def test_ambiguous_invalid_or_absent_http_framing_is_rejected(self):
        retained, _ = self.build("retained-input")
        before = file_snapshot(retained)
        body = synthetic_body()
        length = str(len(body))
        cases = [
            ("no-framing", []),
            ("duplicate-length", [("Content-Length", length), ("Content-Length", length)]),
            ("conflicting-length", [("Content-Length", length), ("Content-Length", "12")]),
            ("length-and-encoding", [("Content-Length", length), ("Transfer-Encoding", "chunked")]),
            ("unknown-encoding", [("Transfer-Encoding", "gzip")]),
            ("multiple-encodings", [("Transfer-Encoding", "gzip, chunked")]),
            ("repeated-encoding", [("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")]),
        ]
        cases += [(f"invalid-length-{index}", [("Content-Length", value)])
                  for index, value in enumerate(("-1", "+12", "12.0", "1 2", "²", "", "1000001",
                                                   "9" * 5000, f"{length}, {length}"))]
        for name, headers in cases:
            with self.subTest(case=name):
                result = wire_response(body, headers)
                output = self.root / name
                with patch.object(climate_module, "open_noaa", return_value=result) as open_request:
                    with self.assertRaises(EvidenceError):
                        export_climate(self.query, output)
                open_request.assert_called_once()
                self.assertTrue(result.closed)
                self.assert_no_output(output)
                self.assertEqual(file_snapshot(retained), before)
        # A header or boolean on a generic response does not activate urllib's
        # native chunk decoder and cannot establish chunk completion.
        result = response(SOURCE_URL, body)
        del result.headers["Content-Length"]
        result.headers["Transfer-Encoding"] = "chunked"
        result.chunked = True
        with patch.object(climate_module, "open_noaa", return_value=result):
            with self.assertRaises(EvidenceError):
                export_climate(self.query, self.root / "inactive-chunk-decoder")
        self.assertTrue(result.closed)
        self.assert_no_output(self.root / "inactive-chunk-decoder")

    def test_required_header_units_creation_method_and_revision_cannot_drift_silently(self):
        body = synthetic_body()
        cases = {
            "units": body.replace(UNITS.encode(), b"CO2 measured as mass emissions in tonnes"),
            "creation-absent": body.replace(("# File Creation: " + CREATION + "\n").encode(), b""),
            "creation-blank": body.replace(CREATION.encode(), b"   "),
            "creation-duplicate": body.replace(b"# File Creation:", b"# File Creation: second label\n# File Creation:"),
            "columns": body.replace(b"# year mean unc", b"# year unc mean"),
            "header-absent": body.replace(b"# year mean unc\n", b""),
            "uncertainty-absent": synthetic_body(uncertainty="Synthetic context only."),
            "revision-absent": synthetic_body(revision="Synthetic context only."),
            "invalid-encoding": b"\xff" + body,
        }
        self.assertIn("200", UNCERTAINTY)
        cases["uncertainty-method-changed"] = synthetic_body(uncertainty=UNCERTAINTY.replace("200", "201", 1))
        for name, changed in cases.items():
            with self.subTest(case=name):
                self.assert_body_rejected(changed, name)

    def test_offline_replay_preserves_bytes_hashes_timestamps_and_input_tree(self):
        source, original = self.build("source")
        before = file_snapshot(source)
        output, replayed = self.replay(source)
        self.assertEqual((source / "data.csv").read_bytes(), (output / "data.csv").read_bytes())
        self.assertEqual((source / "raw/co2_annmean_gl.txt").read_bytes(), (output / "raw/co2_annmean_gl.txt").read_bytes())
        for key in ("query", "data_origin", "row_count", "counts", "source", "raw_sources", "data_csv_sha256"):
            self.assertEqual(replayed[key], original[key], key)
        self.assertEqual(replayed["original_manifest_sha256"], digest((source / "provenance.json").read_bytes()))
        self.assertEqual(file_snapshot(source), before)
        # This deliberate label change illustrates what replay cannot authenticate.
        manifest = read_json(source / "provenance.json")
        manifest["data_origin"] = "noaa_gml"
        (source / "provenance.json").write_text(json.dumps(manifest), encoding="utf-8")
        _, relabeled = self.replay(source, "supplied-origin-label")
        self.assertEqual(relabeled["data_origin"], "noaa_gml")
        self.assertIn("without authenticating historical retrieval", relabeled["source"]["origin_context"])

    def test_offline_values_and_metadata_are_reconstructed_not_trusted_from_derived_files(self):
        source, original = self.build("source")
        expected_csv = (source / "data.csv").read_bytes()
        manifest = read_json(source / "provenance.json")
        manifest["row_count"] = 999
        manifest["counts"] = {"available": 999}
        manifest["source"] = {"source_file_creation": "Untrusted derived summary"}
        manifest["data_csv_sha256"] = "0" * 64
        (source / "provenance.json").write_text(json.dumps(manifest), encoding="utf-8")
        (source / "data.csv").write_text("year,co2_mean\n1979,999999\n", encoding="utf-8")
        for name, remove_csv in (("tampered-derived", False), ("deleted-derived", True)):
            if remove_csv:
                (source / "data.csv").unlink()
            before = file_snapshot(source)
            output, replayed = self.replay(source, name)
            self.assertEqual((output / "data.csv").read_bytes(), expected_csv)
            self.assertEqual(replayed["source"], original["source"])
            self.assertEqual(replayed["counts"], original["counts"])
            self.assertEqual(replayed["row_count"], original["row_count"])
            self.assertEqual(file_snapshot(source), before)

    def test_offline_manifest_identity_query_and_raw_catalog_are_strict(self):
        source, _ = self.build("source")
        manifest_path = source / "provenance.json"
        original = read_json(manifest_path)
        cases = []
        for field, value in (("schema_version", 99), ("schema_version", True),
                             ("artifact_type", "indicator_panel"), ("data_origin", "worldbank_api"),
                             ("data_origin", "synthetic_fixture\n"), ("data_origin", None),
                             ("raw_sources", []), ("raw_sources", original["raw_sources"] * 2)):
            changed = deepcopy(original)
            changed[field] = value
            cases.append((field + repr(value), changed))
        for field, value in (("series", "co2_monthly"), ("start_year", True), ("end_year", "1981"), ("unexpected", 1)):
            changed = deepcopy(original)
            changed["query"][field] = value
            cases.append(("query." + field, changed))
        for field, value in (("url", "https://outside.example/data"), ("sha256", "bad hash"),
                             ("retrieved_at", "2026-11-26")):
            changed = deepcopy(original)
            changed["raw_sources"][0][field] = value
            cases.append(("raw." + field, changed))
        for index, (name, changed) in enumerate(cases):
            manifest_path.write_text(json.dumps(changed), encoding="utf-8")
            before = file_snapshot(source)
            with self.subTest(case=name), self.assertRaises(EvidenceError):
                self.replay(source, f"bad-manifest-{index}")
            self.assert_no_output(self.root / f"bad-manifest-{index}")
            self.assertEqual(file_snapshot(source), before)
        manifest_path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaises(EvidenceError):
            self.replay(source, "wrong-query", query=ClimateQuery(1980, 1981))
        self.assert_no_output(self.root / "wrong-query")

    def test_duplicate_json_fields_and_raw_tampering_are_rejected(self):
        source, _ = self.build("source")
        manifest_path = source / "provenance.json"
        original = manifest_path.read_bytes()
        changed = original.replace(b'"data_origin": "synthetic_fixture"',
                                   b'"data_origin": "synthetic_fixture", "data_origin": "synthetic_fixture"', 1)
        self.assertNotEqual(changed, original)
        manifest_path.write_bytes(changed)
        with self.assertRaises(EvidenceError):
            self.replay(source, "duplicate-json")
        self.assert_no_output(self.root / "duplicate-json")
        manifest_path.write_bytes(original)
        raw = source / "raw/co2_annmean_gl.txt"
        raw.write_bytes(raw.read_bytes().replace(b"402.000", b"999.000"))
        before = file_snapshot(source)
        with self.assertRaises(EvidenceError):
            self.replay(source, "raw-tampering")
        self.assert_no_output(self.root / "raw-tampering")
        self.assertEqual(file_snapshot(source), before)

    def test_offline_raw_paths_must_be_relative_contained_and_regular(self):
        source, _ = self.build("source")
        manifest_path = source / "provenance.json"
        original = read_json(manifest_path)
        outside = self.root / "outside.txt"
        outside.write_bytes(synthetic_body())
        directory = source / "directory-not-file"
        directory.mkdir()
        escape = source / "raw/escape.txt"
        escape.symlink_to(outside)
        for index, raw_path in enumerate((str(outside), "../outside.txt", "raw/escape.txt", "directory-not-file")):
            changed = deepcopy(original)
            changed["raw_sources"][0]["file"] = raw_path
            manifest_path.write_text(json.dumps(changed), encoding="utf-8")
            before = file_snapshot(source)
            with self.subTest(path=raw_path), self.assertRaises(EvidenceError):
                self.replay(source, f"unsafe-path-{index}")
            self.assert_no_output(self.root / f"unsafe-path-{index}")
            self.assertEqual(file_snapshot(source), before)
        self.assertEqual(outside.read_bytes(), synthetic_body())

    def test_raw_and_manifest_limits_accept_exact_boundary_and_reject_one_extra_byte(self):
        for kind in ("raw", "manifest"):
            with self.subTest(kind=kind):
                source, _ = self.build(f"{kind}-source")
                manifest_path = source / "provenance.json"
                target = source / "raw/co2_annmean_gl.txt" if kind == "raw" else manifest_path
                original = target.read_bytes()
                # Trailing whitespace changes no source tokens or JSON fields.
                target.write_bytes(original + b" " * (1_000_000 - len(original)))
                self.assertEqual(target.stat().st_size, 1_000_000)
                if kind == "raw":
                    manifest = read_json(manifest_path)
                    manifest["raw_sources"][0]["sha256"] = digest(target.read_bytes())
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                before = file_snapshot(source)
                output, _ = self.replay(source, f"{kind}-exact-boundary")
                self.assertEqual(read_rows(output)[0]["co2_mean"], PRECISE)
                self.assertEqual(file_snapshot(source), before)
                with target.open("ab") as stream:
                    stream.write(b" ")
                before = file_snapshot(source)
                with self.assertRaises(EvidenceError):
                    self.replay(source, f"{kind}-oversized")
                self.assert_no_output(self.root / f"{kind}-oversized")
                self.assertEqual(file_snapshot(source), before)

    def test_valid_raw_unicode_comments_cannot_expand_into_an_oversized_manifest(self):
        source, _ = self.build("source")
        padding = "# Synthetic metadata padding: " + "é" * 170_000 + "\n"
        body = padding.encode("utf-8") + synthetic_body()
        self.assertLess(len(body), 1_000_000)
        self.assertGreater(170_000 * len(r"\u00e9"), 1_000_000)
        (source / "raw/co2_annmean_gl.txt").write_bytes(body)
        manifest = read_json(source / "provenance.json")
        manifest["raw_sources"][0]["sha256"] = digest(body)
        (source / "provenance.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertLess((source / "provenance.json").stat().st_size, 1_000_000)
        before = file_snapshot(source)
        with self.assertRaisesRegex(EvidenceError, "provenance.*1 MB"):
            self.replay(source, "oversized-generated-manifest")
        self.assert_no_output(self.root / "oversized-generated-manifest")
        self.assertEqual(file_snapshot(source), before)

    def test_csv_limit_preserves_exact_boundary_and_never_truncates_requested_rows(self):
        source, _ = self.build("source")
        csv_bytes = (source / "data.csv").read_bytes()
        before = file_snapshot(source)
        # The normal bounded year window cannot approach 5 MB; reduce only the
        # defensive CSV cap to exercise the real complete export at its boundary.
        with patch.object(climate_module, "MAX_CSV_BYTES", len(csv_bytes)):
            output, _ = self.replay(source, "csv-exact-boundary")
        self.assertEqual((output / "data.csv").read_bytes(), csv_bytes)
        with patch.object(climate_module, "MAX_CSV_BYTES", len(csv_bytes) - 1):
            with self.assertRaisesRegex(EvidenceError, "CSV"):
                self.replay(source, "csv-oversized")
        self.assert_no_output(self.root / "csv-oversized")
        self.assertEqual(file_snapshot(source), before)

    def test_existing_outputs_and_input_aliases_are_preserved(self):
        source, _ = self.build("SourceArchive")
        existing_file = self.root / "existing-file"
        existing_file.write_bytes(b"Keep this result")
        existing_directory = self.root / "existing-directory"
        existing_directory.mkdir()
        (existing_directory / "keep.txt").write_bytes(b"Keep prior contents")
        alias = self.root / "source-alias"
        alias.symlink_to(source, target_is_directory=True)
        outputs = [existing_file, existing_directory, source / "nested", alias / "missing/ancestors/nested"]
        case_alias = source.with_name("sOURCEaRCHIVE")
        if case_alias.exists() and case_alias.samefile(source):
            outputs.append(case_alias / "more/missing/ancestors/nested")
        before = file_snapshot(self.root)
        for output in outputs:
            with self.subTest(output=output), patch.object(climate_module, "_fetch_snapshot", side_effect=AssertionError("Network forbidden")):
                with self.assertRaises(EvidenceError):
                    export_climate(self.query, output, offline=source)
            self.assertEqual(file_snapshot(self.root), before)

    def test_late_manifest_write_failure_cleans_populated_stage_and_preserves_input(self):
        source, _ = self.build("source")
        before = file_snapshot(source)
        output = self.root / "failed-write"
        write_bytes = Path.write_bytes
        observed = []

        def fail_final_manifest(path, data):
            if path.name == "provenance.json" and path.parent != source:
                observed.append(path.parent)
                self.assertTrue((path.parent / "data.csv").is_file())
                self.assertEqual((path.parent / "raw/co2_annmean_gl.txt").read_bytes(), synthetic_body())
                raise OSError("Synthetic disk-full failure at final manifest")
            return write_bytes(path, data)

        with patch.object(Path, "write_bytes", fail_final_manifest):
            with self.assertRaises((EvidenceError, OSError)):
                self.replay(source, output.name)
        self.assertEqual(len(observed), 1)
        self.assert_no_output(output)
        self.assertEqual(file_snapshot(source), before)


class ClimateTransportTests(unittest.TestCase):
    def test_single_fixed_https_get_uses_requested_timeout_and_retains_exact_body(self):
        calls = []
        responses = []
        body = synthetic_body()

        def transport(_handler, request):
            self.assertTrue(_handler._context.check_hostname)
            self.assertEqual(_handler._context.verify_mode, ssl.CERT_REQUIRED)
            calls.append((request.full_url, request.get_method(), request.timeout, request.data))
            result = response(request.full_url, body)
            responses.append(result)
            return result

        with patch.object(HTTPSHandler, "https_open", transport), \
                patch.object(HTTPHandler, "http_open", side_effect=AssertionError("HTTP forbidden")):
            snapshot = climate_module._fetch_snapshot(7.5)
        self.assertEqual(SOURCE_URL, "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_gl.txt")
        self.assertEqual(calls, [(SOURCE_URL, "GET", 7.5, None)])
        self.assertEqual(snapshot.url, SOURCE_URL)
        self.assertEqual(snapshot.body, body)
        self.assertEqual(snapshot.origin, "noaa_gml")
        self.assertTrue(snapshot.retrieved_at.endswith("Z"))
        datetime.fromisoformat(snapshot.retrieved_at.replace("Z", "+00:00"))
        self.assertTrue(all(item.closed for item in responses))

    def test_network_read_obeys_one_mb_cap_and_snapshot_identity_validation(self):
        for size in (1_000_000, 1_000_001):
            body = b" " * size
            result = response(SOURCE_URL, body)
            with self.subTest(size=size), patch.object(climate_module, "open_noaa", return_value=result) as open_request:
                if size == 1_000_000:
                    snapshot = climate_module._fetch_snapshot(15)
                    self.assertEqual(snapshot.body, body)
                else:
                    with self.assertRaises(EvidenceError):
                        climate_module._fetch_snapshot(15)
            open_request.assert_called_once()
            self.assertTrue(result.closed)
        invalid = (
            (SOURCE_URL + "?extra=1", b"body", STAMP, "synthetic_fixture"),
            (SOURCE_URL, b" " * 1_000_001, STAMP, "synthetic_fixture"),
            (SOURCE_URL, "text instead of bytes", STAMP, "synthetic_fixture"),
            (SOURCE_URL, b"body", "2026-11-26", "synthetic_fixture"),
            (SOURCE_URL, b"body", STAMP, "unverified_fallback"),
        )
        for index, args in enumerate(invalid):
            with self.subTest(invalid_snapshot=index), self.assertRaises(EvidenceError):
                ClimateSnapshot(*args)

    def test_all_redirect_statuses_and_destinations_are_rejected_before_dispatch(self):
        cases = [(status, SOURCE_URL + "?revised=1") for status in (301, 302, 303, 307, 308)]
        cases += [(302, destination) for destination in (
            SOURCE_URL, "/webdata/ccgg/trends/co2/other.txt", "https://outside.example/data",
            SOURCE_URL.replace("https:", "http:"), SOURCE_URL.replace("gml.noaa.gov", "person@gml.noaa.gov"),
            SOURCE_URL.replace("gml.noaa.gov", "gml.noaa.gov:444"),
        )]
        for status, destination in cases:
            calls, responses = [], []

            def transport(_handler, request):
                calls.append(request.full_url)
                result = response(request.full_url, b"", status=status, location=destination)
                responses.append(result)
                return result

            with self.subTest(status=status, destination=destination), \
                    patch.object(HTTPSHandler, "https_open", transport), patch.object(HTTPHandler, "http_open", transport):
                with self.assertRaises(EvidenceError):
                    climate_module._fetch_snapshot(15)
            self.assertEqual(calls, [SOURCE_URL], "A rejected redirect must never receive a request.")
            self.assertTrue(all(item.closed for item in responses))

    def test_unexpected_final_url_and_network_failures_fail_without_retry_or_output(self):
        with tempfile.TemporaryDirectory(prefix="eco-climate-transport-") as tmp:
            root = Path(tmp)
            for index, final_url in enumerate((SOURCE_URL + "?changed=1", SOURCE_URL + "#fragment", "https://outside.example/data")):
                result = response(final_url, synthetic_body())
                with patch.object(climate_module, "open_noaa", return_value=result) as open_request:
                    with self.assertRaises(EvidenceError):
                        export_climate(ClimateQuery(1979, 1981), root / f"redirect-{index}")
                open_request.assert_called_once()
                self.assertEqual(open_request.call_args.args[0].get_method(), "GET")
                self.assertTrue(result.closed)
                self.assertEqual(list(root.iterdir()), [])
            for status in (204, 206):
                result = response(SOURCE_URL, synthetic_body(), status=status)
                with patch.object(climate_module, "open_noaa", return_value=result) as open_request:
                    with self.assertRaises(EvidenceError):
                        export_climate(ClimateQuery(1979, 1981), root / f"status-{status}")
                open_request.assert_called_once()
                self.assertEqual(open_request.call_args.args[0].get_method(), "GET")
                self.assertTrue(result.closed)
                self.assertEqual(list(root.iterdir()), [])
            errors = (HTTPError(SOURCE_URL, 429, "Synthetic rate limit", None, None), URLError("Synthetic offline"),
                      TimeoutError("Synthetic timeout"), IncompleteRead(b"Synthetic partial body", 100))
            for index, error in enumerate(errors):
                with self.subTest(error=type(error).__name__), patch.object(climate_module, "open_noaa", side_effect=error) as open_request:
                    with self.assertRaises(EvidenceError):
                        export_climate(ClimateQuery(1979, 1981), root / f"failure-{index}")
                open_request.assert_called_once()
                self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
