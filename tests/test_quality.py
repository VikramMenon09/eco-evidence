"""Independent coverage/flag examples; all constructed source data is invented."""

from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from eco_evidence import __version__
from eco_evidence.archive import read_bundle
from eco_evidence.core import EvidenceError, digest
from eco_evidence.quality import quality_bundle, render_quality_text
from test_panel import (STAMP, file_snapshot, mutate_source, read_json,
                        rewrite_manifest, synthetic_bundle)


ROOT = Path(__file__).resolve().parents[1]
TINY_NEGATIVE = "-1E-31"
JUST_ABOVE_100 = "100.0000000000000000000000000000001"


class QualityAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eco-quality-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = synthetic_bundle(
            self.root / "bundle", "renewables", ("USA", "CAN", "IND"), 2020, 2026,
            {
                ("CAN", 2020): None, ("CAN", 2022): None, ("CAN", 2026): None,
                ("USA", 2020): None, ("USA", 2021): Decimal("-0.00"),
                ("USA", 2022): None, ("USA", 2024): Decimal(JUST_ABOVE_100),
                ("USA", 2025): Decimal(TINY_NEGATIVE),
                **{("IND", year): value for year, value in zip(
                    range(2020, 2027),
                    [0, 100, Decimal("1E+999999"), Decimal("1E-999999"),
                     Decimal("-0E+999999"), 50, Decimal("100.00")],
                )},
            },
            flags={("USA", 2021): " ", ("USA", 2025): "invented flag",
                   ("CAN", 2020): "invented missing flag"},
        )

    def test_hand_worked_summary_reconciles_zero_missing_omission_and_overlapping_flags(self):
        report = quality_bundle(self.bundle)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["artifact_type"], "bundle_quality")
        self.assertEqual(report["tool_version"], __version__)
        self.assertEqual(report["summary"], {
            "row_count": 21, "country_count": 3,
            "counts": {"available": 10, "missing": 5, "not_returned": 6},
            "zero_value_count": 3, "flagged_row_count": 5,
            "range_flag_count": 3, "source_flag_count": 3,
            "gap_run_count": 4, "gap_year_count": 11, "countries_with_gaps": 2,
            "all_unavailable_country_count": 1,
            "gap_position_counts": {"leading": 1, "internal": 1,
                                    "trailing": 1, "all_unavailable": 1},
        })
        self.assertEqual(report["summary"]["counts"], report["bundle"]["counts"])
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertEqual(report, quality_bundle(self.bundle))

    def test_gap_spans_keep_both_missingness_kinds_and_query_relative_positions(self):
        countries = {item["country_code"]: item for item in quality_bundle(self.bundle)["countries"]}
        usa = countries["USA"]
        self.assertEqual(usa["first_available_year"], 2021)
        self.assertEqual(usa["last_available_year"], 2025)
        self.assertEqual(usa["counts"], {"available": 3, "missing": 2, "not_returned": 2})
        self.assertEqual(usa["gaps"], [
            {"start_year": 2020, "end_year": 2020, "length": 1, "position": "leading",
             "counts": {"missing": 1, "not_returned": 0}},
            {"start_year": 2022, "end_year": 2023, "length": 2, "position": "internal",
             "counts": {"missing": 1, "not_returned": 1}},
            {"start_year": 2026, "end_year": 2026, "length": 1, "position": "trailing",
             "counts": {"missing": 0, "not_returned": 1}},
        ])
        can = countries["CAN"]
        self.assertIsNone(can["first_available_year"])
        self.assertIsNone(can["last_available_year"])
        self.assertEqual(can["gaps"], [
            {"start_year": 2020, "end_year": 2026, "length": 7, "position": "all_unavailable",
             "counts": {"missing": 3, "not_returned": 4}},
        ])
        ind = countries["IND"]
        self.assertEqual(ind["gaps"], [])
        self.assertEqual((ind["first_available_year"], ind["last_available_year"]), (2020, 2026))
        # Range flags retain availability, so a flagged value still ends a gap.
        self.assertEqual(usa["range_flag_count"], 2)
        self.assertEqual(usa["flagged_row_count"], 3)

    def test_full_row_ledger_keeps_values_statuses_units_names_flags_and_origin(self):
        expected = read_bundle(self.bundle)["rows"]
        report = quality_bundle(self.bundle)
        self.assertEqual([{key: value for key, value in row.items() if key != "flags"}
                          for row in report["rows"]], expected)
        self.assertEqual([(row["country_code"], row["year"]) for row in report["rows"]],
                         [(code, year) for code in ("CAN", "IND", "USA")
                          for year in range(2020, 2027)])
        indexed = {(row["country_code"], row["year"]): row for row in report["rows"]}
        expected_flags = {
            ("CAN", 2020): ["source_observation_flag"],
            ("IND", 2022): ["value_above_maximum"],
            ("USA", 2021): ["source_observation_flag"],
            ("USA", 2024): ["value_above_maximum"],
            ("USA", 2025): ["value_below_minimum", "source_observation_flag"],
        }
        self.assertEqual({key: row["flags"] for key, row in indexed.items() if row["flags"]},
                         expected_flags)
        self.assertEqual(indexed[("USA", 2021)]["value"], "-0.00")
        self.assertEqual(indexed[("USA", 2021)]["source_obs_status"], " ")
        self.assertEqual(indexed[("USA", 2024)]["value"], JUST_ABOVE_100)
        self.assertEqual(indexed[("USA", 2025)]["value"], TINY_NEGATIVE)
        self.assertEqual(indexed[("IND", 2022)]["value"], "1E+999999")
        self.assertEqual(indexed[("IND", 2023)]["value"], "1E-999999")
        self.assertEqual(indexed[("IND", 2024)]["value"], "-0E+999999")
        self.assertTrue(all(row["data_origin"] == "synthetic_fixture" for row in report["rows"]))
        with localcontext() as context:
            context.prec = 1
            context.Emax = 5
            context.Emin = -5
            self.assertEqual(quality_bundle(self.bundle), report)
        self.assertEqual(float(JUST_ABOVE_100), 100.0, "Test must detect a float-based comparison")

    def test_rule_endpoints_are_inclusive_and_only_renewables_has_upper_bound(self):
        for alias in ("gdp", "population"):
            with self.subTest(indicator=alias):
                bundle = synthetic_bundle(
                    self.root / alias, alias, ("USA",), 2020, 2023,
                    {("USA", 2020): -1, ("USA", 2021): 0,
                     ("USA", 2022): Decimal(JUST_ABOVE_100),
                     ("USA", 2023): Decimal("1E+999999")},
                )
                report = quality_bundle(bundle)
                self.assertEqual(report["rule"]["minimum"], "0")
                self.assertIsNone(report["rule"]["maximum"])
                self.assertEqual([row["flags"] for row in report["rows"]],
                                 [["value_below_minimum"], [], [], []])
                self.assertEqual(report["summary"]["counts"]["available"], 4)
                self.assertEqual(report["summary"]["gap_run_count"], 0)
        report = quality_bundle(self.bundle)
        self.assertEqual((report["rule"]["minimum"], report["rule"]["maximum"]), ("0", "100"))
        self.assertEqual(report["rule"]["kind"], "project_screening_bounds")
        self.assertIn("not a source validity guarantee", report["rule"]["interpretation"])

    def test_single_year_available_null_and_absent_windows_are_unambiguous(self):
        for label, values, expected_status in (
            ("zero", {("USA", 2020): 0}, "available"),
            ("null", {("USA", 2020): None}, "missing"),
            ("absent", {}, "not_returned"),
        ):
            with self.subTest(case=label):
                bundle = synthetic_bundle(self.root / label, "population", ("USA",), 2020, 2020, values)
                report = quality_bundle(bundle)
                country = report["countries"][0]
                self.assertEqual(report["rows"][0]["status"], expected_status)
                if expected_status == "available":
                    self.assertEqual(country["gaps"], [])
                    self.assertEqual(country["zero_value_count"], 1)
                    self.assertEqual(country["first_available_year"], 2020)
                else:
                    self.assertIsNone(country["first_available_year"])
                    self.assertEqual(country["gaps"], [{
                        "start_year": 2020, "end_year": 2020, "length": 1,
                        "position": "all_unavailable",
                        "counts": {"missing": int(expected_status == "missing"),
                                   "not_returned": int(expected_status == "not_returned")},
                    }])

    def test_metadata_lineage_and_qualified_licensing_are_retained(self):
        report = quality_bundle(self.bundle)
        overview = report["bundle"]
        manifest = read_json(self.bundle / "provenance.json")
        self.assertEqual(overview["original_manifest_sha256"], digest((self.bundle / "provenance.json").read_bytes()))
        self.assertEqual(overview["indicator"]["code"], "EG.FEC.RNEW.ZS")
        self.assertEqual(overview["indicator"]["unit"], "% of total final energy consumption")
        self.assertEqual(overview["indicator"]["api_unit"], "")
        self.assertEqual(overview["source"]["last_updated"], "2026-11-21")
        self.assertEqual(overview["data_origin"], "synthetic_fixture")
        self.assertEqual(overview["licensing"]["status"], "not_verified_from_archived_api_responses")
        self.assertIn("invented", overview["licensing"]["synthetic_context"])
        self.assertEqual({row["url"]: row["sha256"] for row in overview["raw_sources"]},
                         {row["url"]: row["sha256"] for row in manifest["raw_sources"]})
        self.assertTrue(all(row["retrieved_at"] == STAMP for row in overview["raw_sources"]))

    def test_read_once_and_read_only_generation_and_formatter_do_not_mutate_inputs(self):
        archive = read_bundle(self.bundle)
        original = deepcopy(archive)
        before = file_snapshot(self.root)
        with patch("eco_evidence.quality.read_bundle", wraps=read_bundle) as reader, \
             patch("eco_evidence.core.open_official", side_effect=AssertionError("Network forbidden")), \
             patch("pathlib.Path.write_bytes", side_effect=AssertionError("Writes forbidden")), \
             patch("pathlib.Path.write_text", side_effect=AssertionError("Writes forbidden")), \
             patch("pathlib.Path.mkdir", side_effect=AssertionError("Directory creation forbidden")):
            report = quality_bundle(self.bundle)
            reader.assert_called_once_with(self.bundle)
            untouched_report = deepcopy(report)
            self.assertEqual(render_quality_text(report), render_quality_text(report))
            self.assertEqual(report, untouched_report)
        with patch("eco_evidence.quality.read_bundle", return_value=archive):
            self.assertEqual(quality_bundle(self.bundle), report)
        self.assertEqual(archive, original)
        self.assertEqual(file_snapshot(self.root), before)

    def test_derived_csv_and_manifest_summaries_are_not_evidence(self):
        expected = quality_bundle(self.bundle)
        (self.bundle / "data.csv").write_text("untrusted derived garbage")
        manifest = read_json(self.bundle / "provenance.json")
        manifest.update({"row_count": -1, "counts": {}, "indicator": {}, "source": {}})
        rewrite_manifest(self.bundle, manifest)
        actual = quality_bundle(self.bundle)
        self.assertEqual(actual["rows"], expected["rows"])
        self.assertEqual(actual["summary"], expected["summary"])
        self.assertEqual(actual["bundle"]["indicator"], expected["bundle"]["indicator"])
        self.assertNotEqual(actual["bundle"]["original_manifest_sha256"], expected["bundle"]["original_manifest_sha256"])
        (self.bundle / "data.csv").unlink()
        self.assertEqual(quality_bundle(self.bundle), actual)

    def test_corruption_duplicates_and_unit_drift_are_fatal_before_a_report(self):
        for problem in ("hash", "duplicate", "metadata_unit", "observation_unit"):
            with self.subTest(problem=problem):
                bundle = self.root / problem
                shutil.copytree(self.bundle, bundle)
                if problem == "hash":
                    entry = read_json(bundle / "provenance.json")["raw_sources"][0]
                    path = bundle / entry["file"]
                    path.write_bytes(path.read_bytes() + b" ")
                elif problem == "duplicate":
                    def duplicate(payload):
                        payload[1].append(deepcopy(payload[1][0]))
                        payload[0]["total"] += 1
                    mutate_source(bundle, "observation", duplicate)
                elif problem == "metadata_unit":
                    mutate_source(bundle, "metadata", lambda p: p[1][0].update(unit="changed"))
                else:
                    mutate_source(bundle, "observation", lambda p: p[1][0].update(unit="changed"))
                with self.assertRaises(EvidenceError):
                    quality_bundle(bundle)
        with self.assertRaises(EvidenceError):
            quality_bundle(ROOT / "examples/noaa-global-co2")

    def test_literal_ascii_text_retains_all_row_fields_and_source_metadata(self):
        name = "Synthetic Cafe\u0301 \u202e\n\x1b[2J"
        source_note = "Invented <text>\t\u2028\u009b31m\n\U0001f9d0"
        flag = "invented\t\u202e\n\x1b[2J"
        bundle = synthetic_bundle(self.root / "literal", "population", ("USA",), 2020, 2022,
                                  {("USA", 2020): 0, ("USA", 2021): None},
                                  names={"USA": name}, flags={("USA", 2021): flag})
        mutate_source(bundle, "metadata", lambda p: p[1][0].update(sourceNote=source_note))
        report = quality_bundle(bundle)
        text = render_quality_text(report)
        self.assertTrue(all(char == "\n" or 32 <= ord(char) < 127 for char in text))
        self.assertIn(json.dumps(name, ensure_ascii=True), text)
        self.assertIn(json.dumps(source_note, ensure_ascii=True), text)
        self.assertIn(json.dumps(flag, ensure_ascii=True), text)
        for row in report["rows"]:
            expected = (f"  {row['country_code']} {row['year']}: status={row['status']}; "
                        f"value={json.dumps(row['value'])}; unit={json.dumps(row['unit'])}; "
                        f"source_obs_status={json.dumps(row['source_obs_status'])}; flags={json.dumps(row['flags'])}")
            self.assertIn(expected, text.splitlines())
        for raw in report["bundle"]["raw_sources"]:
            self.assertIn(raw["sha256"], text)
            self.assertIn(json.dumps(raw["url"]), text)
            self.assertIn(json.dumps(raw["retrieved_at"]), text)
        for note in report["limitations"]:
            self.assertIn(json.dumps(note), text)
        self.assertIn("Gap 2021-2022: trailing; length=2", text)
        self.assertIn("Status counts: {\"available\": 1, \"missing\": 1, \"not_returned\": 1}", text)

    def test_complete_json_and_text_limits_count_actual_ascii_wire_bytes(self):
        report = quality_bundle(self.bundle)
        wire = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("ascii")
        with patch("eco_evidence.quality.MAX_REPORT_BYTES", len(wire) - 1):
            with self.assertRaisesRegex(EvidenceError, "Complete quality report exceeds"):
                quality_bundle(self.bundle)
        with patch("eco_evidence.quality.MAX_REPORT_BYTES", len(wire)):
            self.assertEqual(quality_bundle(self.bundle), report)
        text = render_quality_text(report)
        with patch("eco_evidence.quality.MAX_REPORT_BYTES", len(text.encode("ascii")) - 1):
            with self.assertRaisesRegex(EvidenceError, "Rendered quality report exceeds"):
                render_quality_text(report)
        with patch("eco_evidence.quality.MAX_REPORT_BYTES", len(text.encode("ascii"))):
            self.assertEqual(render_quality_text(report), text)


if __name__ == "__main__":
    unittest.main()
