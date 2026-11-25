"""Read-only inspection acceptance checks using explicitly invented source data."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eco_evidence import __version__
from eco_evidence.core import EvidenceError, MAX_BYTES, MAX_MANIFEST_BYTES, digest, request_url
from eco_evidence.inspection import inspect_bundle, render_text
from test_panel import (STAMP, file_snapshot, mutate_source, read_json, rewrite_manifest,
                        synthetic_bundle)


ROOT = Path(__file__).resolve().parents[1]
SMALLER = "9007199254740992.0000000000000000001"
LARGER = "9007199254740992.0000000000000000002"


def rewrite_retrievals(directory, timestamp):
    manifest = read_json(directory / "provenance.json")
    for entry in manifest["raw_sources"]:
        entry["retrieved_at"] = timestamp
    rewrite_manifest(directory, manifest)


def observation_entry(directory):
    manifest = read_json(directory / "provenance.json")
    return next(item for item in manifest["raw_sources"]
                if "/indicator/" in item["url"] and "/v2/indicator/" not in item["url"])


def split_observation_page(directory):
    """Repage invented four-row fixture without changing any observations."""
    manifest = read_json(directory / "provenance.json")
    entry = observation_entry(directory)
    index = next(index for index, item in enumerate(manifest["raw_sources"])
                 if item["url"] == entry["url"])
    header, observations = read_json(directory / entry["file"])
    assert len(observations) == 4
    header.update({"page": 1, "pages": 2, "per_page": 2, "total": 4})
    first = json.dumps([header, observations[:2]]).encode()
    (directory / entry["file"]).write_bytes(first)
    manifest["raw_sources"][index]["sha256"] = digest(first)
    second_header = {**header, "page": 2}
    second = json.dumps([second_header, observations[2:]]).encode()
    second_path = "raw/004.json"
    (directory / second_path).write_bytes(second)
    query = manifest["query"]
    second_url = request_url("country/" + ";".join(query["countries"]) + "/indicator/" + query["indicator"],
                             {"source": 2, "date": f"{query['start_year']}:{query['end_year']}"}, 2)
    manifest["raw_sources"].append({"url": second_url, "file": second_path,
                                    "retrieved_at": STAMP, "sha256": digest(second)})
    rewrite_manifest(directory, manifest)
    return second_url


class InspectionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eco-inspection-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.baseline = synthetic_bundle(
            self.root / "baseline", "gdp", ("USA",), 2020, 2025,
            {("USA", 2020): 0, ("USA", 2021): None, ("USA", 2023): 1,
             ("USA", 2024): Decimal(SMALLER), ("USA", 2025): 11},
            flags={("USA", 2025): "old synthetic flag"},
        )
        self.bundle = synthetic_bundle(
            self.root / "bundle", "gdp", ("USA",), 2020, 2025,
            {("USA", 2020): None, ("USA", 2021): 0, ("USA", 2022): None,
             ("USA", 2023): Decimal("1.0"), ("USA", 2024): Decimal(LARGER), ("USA", 2025): 11},
            flags={("USA", 2025): "new synthetic flag"},
        )

    def test_overview_reconstructs_metadata_units_coverage_and_explicit_licensing_limits(self):
        report = inspect_bundle(self.baseline)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["artifact_type"], "bundle_inspection")
        self.assertEqual(report["tool_version"], __version__)
        self.assertIsNone(report["comparison"])
        self.assertEqual(json.loads(json.dumps(report)), report)
        overview = report["bundle"]
        self.assertEqual(overview["query"], {"indicator": "NY.GDP.MKTP.KD", "countries": ["USA"],
                                            "start_year": 2020, "end_year": 2025})
        self.assertEqual(overview["data_origin"], "synthetic_fixture")
        self.assertEqual(overview["row_count"], 6)
        self.assertEqual(overview["counts"], {"available": 4, "missing": 1, "not_returned": 1})
        self.assertEqual(overview["original_manifest_sha256"], digest((self.baseline / "provenance.json").read_bytes()))
        self.assertEqual(overview["indicator"]["code"], "NY.GDP.MKTP.KD")
        self.assertEqual(overview["indicator"]["unit"], "constant 2015 US$")
        self.assertEqual(overview["indicator"]["api_unit"], "")
        self.assertIn("curated", overview["indicator"]["unit_basis"].lower())
        self.assertEqual(overview["indicator"]["source_organization"], "EcoEvidence synthetic fixture authors")
        self.assertEqual(overview["source"]["last_updated"], "2026-11-21")
        license_info = overview["licensing"]
        self.assertEqual(license_info["status"], "not_verified_from_archived_api_responses")
        self.assertIn("CC BY 4.0", license_info["catalog_statement"])
        self.assertIn("Catalog-level", license_info["scope"])
        self.assertIn("no indicator-specific reuse authorization", license_info["scope"])
        self.assertTrue(license_info["synthetic_context"])
        self.assertTrue(license_info["catalog_url"].startswith("https://"))
        self.assertTrue(license_info["terms_url"].startswith("https://"))

    def test_raw_endpoint_identity_hashes_and_original_retrieval_dates_are_retained(self):
        expected = {entry["url"]: entry for entry in read_json(self.bundle / "provenance.json")["raw_sources"]}
        overview = inspect_bundle(self.bundle)["bundle"]
        self.assertEqual({entry["url"]: entry["role"] for entry in overview["raw_sources"]}, {
            request_url("country/USA", {}, 1): "country_metadata",
            request_url("indicator/NY.GDP.MKTP.KD", {"source": 2}, 1): "indicator_metadata",
            request_url("country/USA/indicator/NY.GDP.MKTP.KD", {"source": 2, "date": "2020:2025"}, 1): "observations",
        })
        self.assertEqual({entry["url"] for entry in overview["raw_sources"]}, set(expected))
        for entry in overview["raw_sources"]:
            original = expected[entry["url"]]
            self.assertEqual(entry["retrieved_at"], STAMP)
            self.assertEqual(entry["sha256"], original["sha256"])
            self.assertEqual(entry["sha256"], digest((self.bundle / original["file"]).read_bytes()))
        self.assertNotEqual(overview["source"]["last_updated"], STAMP[:10])

    def test_inspection_and_rendering_make_no_network_calls_or_filesystem_writes(self):
        before = file_snapshot(self.root)
        with patch("eco_evidence.core.open_official", side_effect=AssertionError("Network forbidden")), \
             patch("pathlib.Path.write_bytes", side_effect=AssertionError("Writes forbidden")), \
             patch("pathlib.Path.write_text", side_effect=AssertionError("Writes forbidden")), \
             patch("pathlib.Path.mkdir", side_effect=AssertionError("Directory creation forbidden")):
            report = inspect_bundle(self.bundle, baseline_path=self.baseline)
            text = render_text(report)
            self.assertIsInstance(text, str)
            self.assertEqual(report, inspect_bundle(self.bundle, baseline_path=self.baseline))
            self.assertEqual(text, render_text(report))
        self.assertEqual(file_snapshot(self.root), before)

    def test_comparison_preserves_zero_null_omission_flags_and_decimal_precision(self):
        comparison = inspect_bundle(self.bundle, baseline_path=self.baseline)["comparison"]
        self.assertEqual(comparison["direction"], "baseline_to_bundle")
        self.assertEqual(comparison["row_count"], 6)
        self.assertEqual(comparison["changed_row_count"], 6)
        self.assertEqual(comparison["row_field_change_counts"], {
            "value": 4, "status": 3, "source_obs_status": 1, "country_name": 0,
        })
        self.assertEqual(comparison["numeric_value_change_count"], 1)
        self.assertEqual(comparison["value_representation_change_count"], 1)
        self.assertFalse(comparison["source_update_changed"])
        self.assertEqual(comparison["metadata_changes"], [])
        changes = comparison["row_changes"]
        self.assertEqual([(entry["country_code"], entry["year"]) for entry in changes],
                         [("USA", year) for year in range(2020, 2026)])
        indexed = {entry["year"]: entry for entry in changes}
        self.assertEqual(indexed[2020]["fields"], {
            "value": {"baseline": "0", "bundle": ""},
            "status": {"baseline": "available", "bundle": "missing"},
        })
        self.assertEqual(indexed[2021]["fields"]["value"], {"baseline": "", "bundle": "0"})
        self.assertEqual(indexed[2022]["fields"], {
            "status": {"baseline": "not_returned", "bundle": "missing"},
        })
        self.assertEqual(indexed[2023]["fields"], {"value": {"baseline": "1", "bundle": "1.0"}})
        self.assertFalse(indexed[2023]["numeric_value_changed"])
        self.assertTrue(indexed[2023]["value_representation_changed"])
        self.assertEqual(indexed[2024]["fields"], {"value": {"baseline": SMALLER, "bundle": LARGER}})
        self.assertTrue(indexed[2024]["numeric_value_changed"])
        self.assertFalse(indexed[2024]["value_representation_changed"])
        self.assertEqual(float(SMALLER), float(LARGER), "Fixture must detect precision lost by binary float comparison")
        self.assertEqual(indexed[2025]["fields"], {
            "source_obs_status": {"baseline": "old synthetic flag", "bundle": "new synthetic flag"},
        })
        for year in (2020, 2021, 2022, 2025):
            self.assertFalse(indexed[year]["numeric_value_changed"])
            self.assertFalse(indexed[year]["value_representation_changed"])

    def test_reversing_arguments_reverses_direction_without_assuming_chronology(self):
        # The selected bundle may have earlier retrieval timestamps than its baseline.
        rewrite_retrievals(self.baseline, "2026-11-24T20:00:00Z")
        forward = inspect_bundle(self.bundle, baseline_path=self.baseline)["comparison"]
        reverse = inspect_bundle(self.baseline, baseline_path=self.bundle)["comparison"]
        self.assertEqual(forward["direction"], "baseline_to_bundle")
        self.assertEqual(reverse["direction"], "baseline_to_bundle")
        self.assertEqual(forward["changed_row_count"], reverse["changed_row_count"])
        self.assertEqual(forward["numeric_value_change_count"], reverse["numeric_value_change_count"])
        self.assertEqual(forward["value_representation_change_count"], reverse["value_representation_change_count"])
        for left, right in zip(forward["row_changes"], reverse["row_changes"]):
            self.assertEqual((left["country_code"], left["year"]), (right["country_code"], right["year"]))
            for field, values in left["fields"].items():
                self.assertEqual(values["baseline"], right["fields"][field]["bundle"])
                self.assertEqual(values["bundle"], right["fields"][field]["baseline"])

    def test_self_comparison_has_no_metadata_rows_or_source_changes(self):
        comparison = inspect_bundle(self.bundle, baseline_path=self.bundle)["comparison"]
        self.assertEqual(comparison["changed_row_count"], 0)
        self.assertEqual(comparison["row_changes"], [])
        self.assertEqual(comparison["metadata_changes"], [])
        self.assertEqual(comparison["raw_source_changes"], [])
        self.assertEqual(comparison["raw_source_change_count"], 0)
        self.assertEqual(comparison["raw_source_change_counts"], {
            "added": 0, "removed": 0, "body_changed": 0, "retrieval_timestamp_changed": 0,
        })

    def test_definition_provider_and_update_labels_are_reported_separately_from_numeric_changes(self):
        values = {("USA", 2020): 3, ("USA", 2021): 4}
        baseline = synthetic_bundle(self.root / "metadata-before", "population", ("USA",), 2020, 2021, values)
        bundle = synthetic_bundle(self.root / "metadata-after", "population", ("USA",), 2020, 2021, values,
                                  revision="2026-11-24")
        mutate_source(bundle, "metadata", lambda payload: payload[1][0].update({
            "sourceNote": "Changed synthetic definition, not observed evidence.",
            "sourceOrganization": "Different synthetic provider",
        }))
        comparison = inspect_bundle(bundle, baseline_path=baseline)["comparison"]
        fields = {entry["field"]: entry for entry in comparison["metadata_changes"]}
        self.assertEqual(set(fields), {"indicator.source_note", "indicator.source_organization", "source.last_updated"})
        self.assertEqual(fields["source.last_updated"]["baseline"], "2026-11-21")
        self.assertEqual(fields["source.last_updated"]["bundle"], "2026-11-24")
        self.assertEqual(fields["indicator.source_organization"]["bundle"], "Different synthetic provider")
        self.assertEqual(fields["indicator.source_note"]["bundle"], "Changed synthetic definition, not observed evidence.")
        self.assertTrue(comparison["source_update_changed"])
        self.assertEqual(comparison["numeric_value_change_count"], 0)
        self.assertEqual(comparison["changed_row_count"], 0)

    def test_country_name_changes_do_not_masquerade_as_observation_changes(self):
        values = {("CAN", 2020): 1, ("USA", 2020): 2}
        before = synthetic_bundle(self.root / "names-before", "population", ("CAN", "USA"), 2020, 2020, values)
        after = synthetic_bundle(self.root / "names-after", "population", ("CAN", "USA"), 2020, 2020, values,
                                 names={"CAN": "Renamed synthetic Canada"})
        comparison = inspect_bundle(after, baseline_path=before)["comparison"]
        self.assertEqual(comparison["changed_row_count"], 1)
        self.assertEqual(comparison["row_field_change_counts"], {
            "value": 0, "status": 0, "source_obs_status": 0, "country_name": 1,
        })
        self.assertEqual(comparison["row_changes"][0]["country_code"], "CAN")
        self.assertEqual(comparison["row_changes"][0]["fields"], {
            "country_name": {"baseline": "Synthetic CAN", "bundle": "Renamed synthetic Canada"},
        })
        self.assertEqual(comparison["numeric_value_change_count"], 0)

    def test_retrieval_date_only_changes_remain_distinct_from_body_and_source_revision_changes(self):
        values = {("USA", 2020): 1}
        before = synthetic_bundle(self.root / "retrieval-before", "population", ("USA",), 2020, 2020, values)
        after = synthetic_bundle(self.root / "retrieval-after", "population", ("USA",), 2020, 2020, values)
        rewrite_retrievals(after, "2026-11-24T21:30:00Z")
        comparison = inspect_bundle(after, baseline_path=before)["comparison"]
        self.assertEqual(comparison["raw_source_change_count"], 3)
        self.assertEqual(comparison["raw_source_change_counts"], {
            "added": 0, "removed": 0, "body_changed": 0, "retrieval_timestamp_changed": 3,
        })
        changes = comparison["raw_source_changes"]
        self.assertEqual([entry["url"] for entry in changes], sorted(entry["url"] for entry in changes))
        for entry in changes:
            self.assertEqual(entry["change"], "changed")
            self.assertEqual(entry["changed_fields"], ["retrieved_at"])
            self.assertEqual(entry["baseline"]["sha256"], entry["bundle"]["sha256"])
            self.assertEqual(entry["baseline"]["retrieved_at"], STAMP)
            self.assertEqual(entry["bundle"]["retrieved_at"], "2026-11-24T21:30:00Z")
        self.assertEqual(comparison["changed_row_count"], 0)
        self.assertEqual(comparison["metadata_changes"], [])
        self.assertFalse(comparison["source_update_changed"])

    def test_added_and_removed_pages_report_raw_repackaging_without_numeric_revision(self):
        values = {("USA", year): year - 2019 for year in range(2020, 2024)}
        before = synthetic_bundle(self.root / "pages-before", "population", ("USA",), 2020, 2023, values)
        after = synthetic_bundle(self.root / "pages-after", "population", ("USA",), 2020, 2023, values)
        new_url = split_observation_page(after)
        for bundle, baseline, expected_kind in ((after, before, "added"), (before, after, "removed")):
            with self.subTest(direction=expected_kind):
                comparison = inspect_bundle(bundle, baseline_path=baseline)["comparison"]
                counts = {"added": 0, "removed": 0, "body_changed": 1, "retrieval_timestamp_changed": 0}
                counts[expected_kind] = 1
                self.assertEqual(comparison["raw_source_change_counts"], counts)
                self.assertEqual(comparison["raw_source_change_count"], 2)
                self.assertEqual(comparison["changed_row_count"], 0)
                self.assertEqual(comparison["numeric_value_change_count"], 0)
                changed = {entry["url"]: entry for entry in comparison["raw_source_changes"]}
                entry = changed[new_url]
                self.assertEqual(entry["change"], expected_kind)
                self.assertEqual(entry["changed_fields"], [])
                self.assertIsNone(entry["baseline"] if expected_kind == "added" else entry["bundle"])
                self.assertEqual((entry["bundle"] if expected_kind == "added" else entry["baseline"])["role"], "observations")

    def test_raw_numeric_spelling_changes_are_visible_even_when_reconstructed_values_match(self):
        values = {("USA", 2020): 1}
        before = synthetic_bundle(self.root / "spelling-before", "population", ("USA",), 2020, 2020, values)
        after = synthetic_bundle(self.root / "spelling-after", "population", ("USA",), 2020, 2020, values)
        manifest = read_json(after / "provenance.json")
        entry = manifest["raw_sources"][-1]
        old_raw = (after / entry["file"]).read_bytes()
        new_raw = old_raw.replace(b'"value": 1', b'"value": 1e0')
        self.assertNotEqual(old_raw, new_raw)
        (after / entry["file"]).write_bytes(new_raw)
        entry["sha256"] = digest(new_raw)
        rewrite_manifest(after, manifest)
        comparison = inspect_bundle(after, baseline_path=before)["comparison"]
        self.assertEqual(comparison["changed_row_count"], 0)
        self.assertEqual(comparison["numeric_value_change_count"], 0)
        self.assertEqual(comparison["value_representation_change_count"], 0)
        self.assertEqual(comparison["raw_source_change_counts"]["body_changed"], 1)

    def test_empty_response_has_unknown_source_update_and_known_retrieval_timestamp(self):
        empty = synthetic_bundle(self.root / "empty", "gdp", ("USA",), 2020, 2025, {})

        def exact_empty(payload):
            payload[:] = [{"page": 0, "pages": 0, "per_page": 0, "total": 0,
                           "sourceid": None, "lastupdated": None}, None]

        mutate_source(empty, "observation", exact_empty)
        report = inspect_bundle(empty, baseline_path=self.baseline)
        self.assertIsNone(report["bundle"]["source"]["last_updated"])
        self.assertEqual(report["bundle"]["counts"], {"available": 0, "missing": 0, "not_returned": 6})
        self.assertTrue(all(entry["retrieved_at"] == STAMP for entry in report["bundle"]["raw_sources"]))
        comparison = report["comparison"]
        self.assertTrue(comparison["source_update_changed"])
        change = next(entry for entry in comparison["metadata_changes"] if entry["field"] == "source.last_updated")
        self.assertEqual(change["baseline"], "2026-11-21")
        self.assertIsNone(change["bundle"])
        self.assertEqual(comparison["numeric_value_change_count"], 0)

    def test_derived_csv_and_manifest_summaries_cannot_replace_verified_metadata(self):
        (self.bundle / "data.csv").unlink()
        manifest = read_json(self.bundle / "provenance.json")
        manifest["indicator"].update({"unit": "invented unit", "source_organization": "invented derived provider"})
        manifest["source"].update({"last_updated": "1900-01-01", "license": "unrestricted reuse guaranteed"})
        manifest["row_count"] = 999999
        manifest["counts"] = {"available": 999999}
        rewrite_manifest(self.bundle, manifest)
        before = file_snapshot(self.bundle)
        overview = inspect_bundle(self.bundle)["bundle"]
        self.assertEqual(overview["row_count"], 6)
        self.assertEqual(overview["counts"], {"available": 4, "missing": 2, "not_returned": 0})
        self.assertEqual(overview["indicator"]["unit"], "constant 2015 US$")
        self.assertEqual(overview["indicator"]["source_organization"], "EcoEvidence synthetic fixture authors")
        self.assertEqual(overview["source"]["last_updated"], "2026-11-21")
        self.assertEqual(overview["licensing"]["status"], "not_verified_from_archived_api_responses")
        self.assertNotIn("unrestricted reuse guaranteed", json.dumps(overview))
        self.assertEqual(file_snapshot(self.bundle), before)

    def test_mismatched_query_indicator_or_origin_refuses_comparison(self):
        cases = [
            synthetic_bundle(self.root / "different-country", "gdp", ("IND",), 2020, 2025, {}),
            synthetic_bundle(self.root / "different-window", "gdp", ("USA",), 2020, 2024, {}),
            synthetic_bundle(self.root / "different-indicator", "population", ("USA",), 2020, 2025, {}),
        ]
        origin = synthetic_bundle(self.root / "different-origin", "gdp", ("USA",), 2020, 2025, {})
        manifest = read_json(origin / "provenance.json")
        # Falsified label only tests mixed-origin rejection; data remain invented.
        manifest["data_origin"] = "worldbank_api"
        rewrite_manifest(origin, manifest)
        cases.append(origin)
        for directory in cases:
            before = file_snapshot(self.root)
            with self.subTest(directory=directory.name), self.assertRaises(EvidenceError):
                inspect_bundle(self.bundle, baseline_path=directory)
            self.assertEqual(file_snapshot(self.root), before)

    def test_rejects_noncanonical_queries_ambiguous_json_and_size_limit_violations(self):
        original = (self.bundle / "provenance.json").read_bytes()
        manifest = json.loads(original)
        bad_queries = [None, [], {**manifest["query"], "extra": 1},
                       {**manifest["query"], "start_year": True},
                       {**manifest["query"], "countries": "USA"},
                       {**manifest["query"], "countries": ["USA", "USA"]},
                       {**manifest["query"], "indicator": "gdp"},
                       {**manifest["query"], "end_year": "2025"}]
        bad_bytes = [b'[]', b'{"query":null,"query":{}}', b' ' * (MAX_MANIFEST_BYTES + 1)]
        for query in bad_queries:
            invalid = deepcopy(manifest)
            invalid["query"] = query
            bad_bytes.append(json.dumps(invalid).encode())
        for index, raw in enumerate(bad_bytes):
            (self.bundle / "provenance.json").write_bytes(raw)
            before = file_snapshot(self.bundle)
            with self.subTest(index=index), self.assertRaises(EvidenceError):
                inspect_bundle(self.bundle)
            self.assertEqual(file_snapshot(self.bundle), before)
        (self.bundle / "provenance.json").write_bytes(original)
        entry = observation_entry(self.bundle)
        raw_path = self.bundle / entry["file"]
        raw_path.write_bytes(b' ' * (MAX_BYTES + 1))
        with self.assertRaises(EvidenceError):
            inspect_bundle(self.bundle)

    def test_verified_raw_hashes_and_observation_uniqueness_are_required(self):
        entry = observation_entry(self.bundle)
        path = self.bundle / entry["file"]
        original = path.read_bytes()
        changed = original.replace(b'"value": 11', b'"value": 12')
        self.assertNotEqual(changed, original)
        path.write_bytes(changed)
        with self.assertRaises(EvidenceError):
            inspect_bundle(self.bundle)
        path.write_bytes(original)

        def duplicate(payload):
            payload[1].append(deepcopy(payload[1][0]))
            payload[0]["total"] += 1

        mutate_source(self.bundle, "observation", duplicate)
        before = file_snapshot(self.bundle)
        with self.assertRaises(EvidenceError):
            inspect_bundle(self.bundle)
        self.assertEqual(file_snapshot(self.bundle), before)

    def test_text_report_contains_full_changed_values_and_does_not_mutate_json_report(self):
        report = inspect_bundle(self.bundle, baseline_path=self.baseline)
        before = deepcopy(report)
        rendered = render_text(report)
        self.assertIn(SMALLER, rendered)
        self.assertIn(LARGER, rendered)
        self.assertIn("old synthetic flag", rendered)
        self.assertIn("new synthetic flag", rendered)
        self.assertIn("not_returned", rendered)
        self.assertIn("missing", rendered)
        self.assertIn("synthetic_fixture", rendered)
        self.assertIn("constant 2015 US$", rendered)
        for year in range(2020, 2026):
            self.assertIn(str(year), rendered)
        self.assertEqual(report, before)

    def test_text_escapes_terminal_and_directional_controls_but_json_preserves_source_text(self):
        source_note = "Synthetic control fixture: \x1b[2J\nnext line \u009b31m \u202e reversed \u2066 isolate"
        mutate_source(self.bundle, "metadata", lambda payload: payload[1][0].update({"sourceNote": source_note}))
        report = inspect_bundle(self.bundle)
        self.assertEqual(report["bundle"]["indicator"]["source_note"], source_note)
        text = render_text(report)
        for control, escaped in (("\x1b", r"\u001b"), ("\u009b", r"\u009b"),
                                 ("\u202e", r"\u202e"), ("\u2066", r"\u2066")):
            with self.subTest(control=escaped):
                self.assertNotIn(control, text)
                self.assertIn(escaped, text)
        self.assertIn(r"\nnext line", text)

    def test_report_limit_counts_actual_pretty_json_escapes_and_final_newline(self):
        mutate_source(self.bundle, "metadata", lambda payload: payload[1][0].update({
            "sourceNote": "Synthetic Unicode size fixture: " + "\u6e2c" * 200,
        }))
        report = inspect_bundle(self.bundle, baseline_path=self.baseline)
        wire = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        compact = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(compact), len(wire) - 1, "Fixture must expose compact-size undercounting")
        before = file_snapshot(self.root)
        with patch("eco_evidence.inspection.MAX_REPORT_BYTES", len(wire) - 1):
            with self.assertRaises(EvidenceError):
                inspect_bundle(self.bundle, baseline_path=self.baseline)
        with patch("eco_evidence.inspection.MAX_REPORT_BYTES", len(wire)):
            self.assertEqual(inspect_bundle(self.bundle, baseline_path=self.baseline), report)
        self.assertEqual(file_snapshot(self.root), before)

    def test_text_limit_counts_utf8_bytes_and_refuses_partial_rendering(self):
        report = inspect_bundle(self.bundle, baseline_path=self.baseline)
        text = render_text(report)
        byte_count = len(text.encode("utf-8"))
        self.assertTrue(text.isascii(), "Rendered text must be safe for ASCII-only stdout")
        original = deepcopy(report)
        with patch("eco_evidence.inspection.MAX_REPORT_BYTES", byte_count - 1):
            with self.assertRaises(EvidenceError):
                render_text(report)
        with patch("eco_evidence.inspection.MAX_REPORT_BYTES", byte_count):
            self.assertEqual(render_text(report), text)
        self.assertEqual(report, original)

    def test_historical_example_archives_remain_inspectable_without_modification(self):
        for name, expected_origin, row_count in (("synthetic-population", "synthetic_fixture", 3),
                                                 ("population-2020-2022", "worldbank_api", 6)):
            directory = ROOT / "examples" / name
            before = file_snapshot(directory)
            with self.subTest(example=name), patch("eco_evidence.core.open_official", side_effect=AssertionError("Network forbidden")):
                overview = inspect_bundle(directory)["bundle"]
                self.assertEqual(overview["row_count"], row_count)
                self.assertEqual(overview["data_origin"], expected_origin)
                self.assertEqual(overview["indicator"]["code"], "SP.POP.TOTL")
                if expected_origin == "worldbank_api":
                    self.assertIsNone(overview["licensing"]["synthetic_context"])
            self.assertEqual(file_snapshot(directory), before)


if __name__ == "__main__":
    unittest.main()
