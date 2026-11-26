"""Worked-study acceptance tests. Every constructed observation is synthetic."""

from copy import deepcopy
import csv
from decimal import (Clamped, Decimal, Inexact, Overflow, ROUND_UP, Rounded,
                     Subnormal, localcontext)
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eco_evidence.core import EvidenceError, MAX_BYTES, MAX_MANIFEST_BYTES, digest
import eco_evidence.study as study_module
from eco_evidence.registry import get_indicator
from eco_evidence.study import export_study
from test_panel import (STAMP, file_snapshot, mutate_source, read_json,
                        rewrite_manifest, synthetic_bundle)


ALIASES = ("gdp", "population", "renewables")
UNIT = "constant 2015 US$ per person"


def read_study(directory):
    return list(csv.DictReader(io.StringIO((Path(directory) / "study.csv").read_text(encoding="utf-8"))))


def context_state(context):
    return (context.prec, context.rounding, context.Emin, context.Emax, context.capitals,
            context.clamp, dict(context.flags), dict(context.traps))


class StudyAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eco-study-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.group("matrix", {
            "gdp": {("CAN", 2020): 0, ("CAN", 2021): None,
                    ("IND", 2020): 12, ("IND", 2021): 9, ("IND", 2022): 6,
                    ("USA", 2020): 1, ("USA", 2021): 2, ("USA", 2022): Decimal("12.50")},
            "population": {("CAN", 2020): 10, ("CAN", 2021): None,
                           ("IND", 2020): 0, ("IND", 2021): None,
                           ("USA", 2020): 3, ("USA", 2021): 3, ("USA", 2022): Decimal("2.5")},
            "renewables": {("CAN", 2020): 0, ("CAN", 2021): None,
                           ("IND", 2020): 50, ("IND", 2021): None,
                           ("USA", 2020): 100, ("USA", 2021): None, ("USA", 2022): 25},
        }, countries=("USA", "CAN", "IND"), start=2020, end=2022)

    def group(self, name, values, *, countries=("USA",), start=2020, end=2020):
        return tuple(synthetic_bundle(
            self.root / name / alias, alias, countries, start, end, values[alias],
            names={"USA": f"Synthetic USA ({alias})"},
            flags={("USA", end): f"Synthetic {alias} flag"},
        ) for alias in ALIASES)

    def one_row(self, name, gdp=1, population=1, renewables=50):
        return self.group(name, {alias: {("USA", 2020): value}
                                for alias, value in zip(ALIASES, (gdp, population, renewables))})

    def build(self, name="study", inputs=None):
        output = self.root / name
        provenance = export_study(*(inputs or self.inputs), output)
        return output, provenance

    def assert_no_output(self, output):
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.glob(f".{output.name}-*")), [])

    def test_all_source_rows_and_distinct_exclusions_survive_the_derivation(self):
        output, provenance = self.build()
        rows = read_study(output)
        self.assertEqual([(row["country_code"], row["year"]) for row in rows],
                         [(country, str(year)) for country in ("CAN", "IND", "USA") for year in range(2020, 2023)])
        indexed = {(row["country_code"], row["year"]): row for row in rows}
        self.assertEqual(indexed[("CAN", "2020")]["gdp_per_person"], "0")
        self.assertEqual(indexed[("CAN", "2020")]["pair_complete"], "true")
        self.assertEqual(indexed[("USA", "2020")]["gdp_per_person"], "0.3333333333333333333333333333")
        self.assertEqual(indexed[("USA", "2021")]["gdp_per_person"], "0.6666666666666666666666666667")
        self.assertEqual(indexed[("USA", "2021")]["gdp_per_person_status"], "derived")
        self.assertEqual(indexed[("USA", "2021")]["gdp_per_person_reason"], "")
        self.assertEqual(indexed[("USA", "2021")]["pair_complete"], "false")
        self.assertEqual(indexed[("USA", "2021")]["pair_exclusion_reasons"], "renewables_missing")
        self.assertEqual(Decimal(indexed[("USA", "2022")]["gdp_per_person"]), Decimal("5"))
        self.assertEqual(indexed[("USA", "2022")]["gdp_value"], "12.50")
        self.assertEqual(indexed[("USA", "2022")]["population_value"], "2.5")
        expected = {
            ("CAN", "2021"): ("gdp_missing;population_missing", "gdp_missing;population_missing;renewables_missing"),
            ("CAN", "2022"): ("gdp_not_returned;population_not_returned", "gdp_not_returned;population_not_returned;renewables_not_returned"),
            ("IND", "2020"): ("population_zero", "population_zero"),
            ("IND", "2021"): ("population_missing", "population_missing;renewables_missing"),
            ("IND", "2022"): ("population_not_returned", "population_not_returned;renewables_not_returned"),
        }
        for key, (derived_reason, pair_reason) in expected.items():
            row = indexed[key]
            self.assertEqual(row["gdp_per_person"], "")
            self.assertEqual(row["gdp_per_person_status"], "not_derived")
            self.assertEqual(row["gdp_per_person_reason"], derived_reason)
            self.assertEqual(row["pair_exclusion_reasons"], pair_reason)
            self.assertEqual(row["pair_complete"], "false")
        expected_fields = ["country_code", "year", "data_origin"]
        for alias in ALIASES:
            expected_fields.extend(f"{alias}_{field}" for field in ("value", "status", "unit", "country_name", "source_obs_status"))
        expected_fields.extend(("gdp_per_person", "gdp_per_person_unit", "gdp_per_person_status",
                                "gdp_per_person_reason", "pair_complete", "pair_exclusion_reasons"))
        self.assertEqual(list(rows[0]), expected_fields)
        for row in rows:
            self.assertEqual(row["data_origin"], "synthetic_fixture")
            self.assertEqual(row["gdp_per_person_unit"], UNIT)
            for alias in ALIASES:
                self.assertEqual(row[f"{alias}_unit"], get_indicator(alias).unit)
        for alias in ALIASES:
            self.assertEqual(indexed[("USA", "2022")][f"{alias}_country_name"], f"Synthetic USA ({alias})")
            self.assertEqual(indexed[("USA", "2022")][f"{alias}_source_obs_status"], f"Synthetic {alias} flag")
        self.assertEqual(provenance["row_count"], 9)

    def test_year_summaries_identify_every_eligible_and_excluded_country(self):
        output, _ = self.build()
        summary = read_json(output / "summary.json")
        self.assertEqual((summary["row_count"], summary["derived_count"], summary["pair_complete_count"]), (9, 4, 3))
        self.assertEqual(summary["source_status_counts"], {
            "gdp": {"available": 7, "missing": 1, "not_returned": 1},
            "population": {"available": 5, "missing": 2, "not_returned": 2},
            "renewables": {"available": 4, "missing": 3, "not_returned": 2},
        })
        years = summary["by_year"]
        self.assertEqual([item["year"] for item in years], [2020, 2021, 2022])
        self.assertEqual([(item["row_count"], item["derived_count"], item["pair_complete_count"]) for item in years],
                         [(3, 2, 2), (3, 1, 0), (3, 1, 1)])
        self.assertEqual([item["eligible_country_codes"] for item in years], [["CAN", "USA"], [], ["USA"]])
        self.assertEqual(years[0]["excluded"], [{"country_code": "IND", "reasons": ["population_zero"]}])
        self.assertEqual(years[1]["excluded"], [
            {"country_code": "CAN", "reasons": ["gdp_missing", "population_missing", "renewables_missing"]},
            {"country_code": "IND", "reasons": ["population_missing", "renewables_missing"]},
            {"country_code": "USA", "reasons": ["renewables_missing"]},
        ])
        self.assertEqual(years[2]["excluded"], [
            {"country_code": "CAN", "reasons": ["gdp_not_returned", "population_not_returned", "renewables_not_returned"]},
            {"country_code": "IND", "reasons": ["population_not_returned", "renewables_not_returned"]},
        ])
        self.assertEqual([item["exclusion_reason_counts"] for item in years], [
            {"population_zero": 1},
            {"gdp_missing": 1, "population_missing": 2, "renewables_missing": 3},
            {"gdp_not_returned": 1, "population_not_returned": 2, "renewables_not_returned": 2},
        ])
        for year in years:
            self.assertEqual(len(year["eligible_country_codes"]) + len(year["excluded"]), year["row_count"])
            for counts in year["source_status_counts"].values():
                self.assertEqual(set(counts), {"available", "missing", "not_returned"})
                self.assertEqual(sum(counts.values()), year["row_count"])

    def test_half_even_rounding_uses_twenty_eight_significant_digits(self):
        inputs = self.group("rounding", {
            "gdp": {("USA", 2020): Decimal("1.2345678901234567890123456785"),
                    ("USA", 2021): Decimal("1.2345678901234567890123456795")},
            "population": {("USA", 2020): 1, ("USA", 2021): 1},
            "renewables": {("USA", 2020): 50, ("USA", 2021): 50},
        }, start=2020, end=2021)
        output, provenance = self.build("rounded", inputs)
        rows = read_study(output)
        self.assertEqual([Decimal(row["gdp_per_person"]) for row in rows],
                         [Decimal("1.234567890123456789012345678"), Decimal("1.234567890123456789012345680")])
        self.assertEqual(rows[0]["gdp_value"], "1.2345678901234567890123456785")
        self.assertEqual(rows[1]["gdp_value"], "1.2345678901234567890123456795")
        calculation = provenance["calculation"]
        self.assertEqual(calculation["formula"], "NY.GDP.MKTP.KD / SP.POP.TOTL")
        self.assertEqual(calculation["unit"], UNIT)
        self.assertEqual(calculation["precision_significant_digits"], 28)
        self.assertEqual(calculation["rounding"], "ROUND_HALF_EVEN")
        self.assertEqual(calculation["nonzero_operand_adjusted_exponent_range"], [-100, 100])
        self.assertEqual(calculation["nonzero_result_adjusted_exponent_range"], [-100, 100])
        self.assertEqual(set(calculation["trapped_signals"]),
                         {"Overflow", "Underflow", "Subnormal", "Clamped", "InvalidOperation", "DivisionByZero"})

    def test_hostile_ambient_decimal_settings_do_not_change_any_exported_values(self):
        inputs = self.group("ambient", {
            "gdp": {("USA", 2020): 1, ("USA", 2021): Decimal("1E+100")},
            "population": {("USA", 2020): 3, ("USA", 2021): Decimal("1E+1")},
            "renewables": {("USA", 2020): 50, ("USA", 2021): Decimal("1E-1")},
        }, start=2020, end=2021)
        ordinary, _ = self.build("ordinary", inputs)
        with localcontext() as ambient:
            ambient.prec = 3
            ambient.rounding = ROUND_UP
            ambient.Emin = -2
            ambient.Emax = 2
            ambient.clamp = 1
            ambient.capitals = 0
            ambient.traps[Inexact] = True
            ambient.traps[Rounded] = True
            ambient.traps[Overflow] = False
            ambient.traps[Subnormal] = False
            ambient.traps[Clamped] = False
            before = context_state(ambient)
            unusual, _ = self.build("unusual", inputs)
            self.assertEqual(context_state(ambient), before)
        for filename in ("study.csv", "summary.json"):
            self.assertEqual((ordinary / filename).read_bytes(), (unusual / filename).read_bytes())
        for alias in ALIASES:
            self.assertEqual((ordinary / "inputs" / alias / "data.csv").read_bytes(),
                             (unusual / "inputs" / alias / "data.csv").read_bytes())

    def test_embedded_inputs_replay_identically_with_verified_hashes_and_original_timestamps(self):
        before = [file_snapshot(path) for path in self.inputs]
        with patch("eco_evidence.core.open_official", side_effect=AssertionError("Network forbidden")):
            output, provenance = self.build()
            embedded = tuple(output / "inputs" / alias for alias in ALIASES)
            replay, _ = self.build("replay", embedded)
        self.assertEqual((output / "study.csv").read_bytes(), (replay / "study.csv").read_bytes())
        self.assertEqual((output / "summary.json").read_bytes(), (replay / "summary.json").read_bytes())
        self.assertEqual(provenance["study_csv_sha256"], digest((output / "study.csv").read_bytes()))
        self.assertEqual(provenance["summary_sha256"], digest((output / "summary.json").read_bytes()))
        self.assertEqual([entry["alias"] for entry in provenance["inputs"]], list(ALIASES))
        for index, entry in enumerate(provenance["inputs"]):
            original = self.inputs[index]
            copied = output / entry["bundle_path"]
            self.assertEqual(entry["data_origin"], "synthetic_fixture")
            self.assertEqual(entry["original_manifest_sha256"], digest((original / "provenance.json").read_bytes()))
            self.assertEqual(entry["embedded_manifest_sha256"], digest((copied / "provenance.json").read_bytes()))
            old_sources = {item["url"]: item for item in read_json(original / "provenance.json")["raw_sources"]}
            for source in read_json(copied / "provenance.json")["raw_sources"]:
                prior = old_sources[source["url"]]
                self.assertEqual(source["retrieved_at"], STAMP)
                self.assertEqual(source["sha256"], prior["sha256"])
                self.assertEqual((copied / source["file"]).read_bytes(), (original / prior["file"]).read_bytes())
            self.assertEqual(file_snapshot(original), before[index])

    def test_named_codes_query_and_origin_must_be_compatible(self):
        gdp, population, renewables = self.inputs
        other_country = synthetic_bundle(self.root / "other-country", "population", ("USA",), 2020, 2022, {})
        other_years = synthetic_bundle(self.root / "other-years", "population", ("USA", "CAN", "IND"), 2020, 2021, {})
        other_origin = synthetic_bundle(self.root / "other-origin", "population", ("USA", "CAN", "IND"), 2020, 2022, {})
        manifest = read_json(other_origin / "provenance.json")
        manifest["data_origin"] = "worldbank_api"  # Deliberately false label only exercises rejection.
        rewrite_manifest(other_origin, manifest)
        cases = [(population, gdp, renewables), (gdp, population, gdp),
                 (gdp, other_country, renewables), (gdp, other_years, renewables),
                 (gdp, other_origin, renewables)]
        for index, inputs in enumerate(cases):
            output = self.root / f"incompatible-{index}"
            before = file_snapshot(self.root)
            with self.subTest(index=index), self.assertRaises(EvidenceError):
                export_study(*inputs, output)
            self.assert_no_output(output)
            self.assertEqual(file_snapshot(self.root), before)

    def test_negative_levels_and_renewable_shares_outside_percentage_domain_reject_whole_study(self):
        for index, values in enumerate(((-1, 1, 50), (1, -1, 50), (1, 1, -1), (1, 1, Decimal("100.0000001")))):
            inputs = self.one_row(f"bad-domain-{index}", *values)
            output = self.root / f"rejected-domain-{index}"
            with self.subTest(values=values), self.assertRaises(EvidenceError):
                export_study(*inputs, output)
            self.assert_no_output(output)

    def test_unsupported_operand_exponents_and_ratio_overflow_or_subnormal_results_reject(self):
        cases = [(Decimal("1E+101"), 1), (Decimal("1E-101"), 1),
                 (1, Decimal("1E+101")), (1, Decimal("1E-101")),
                 (Decimal("1E+100"), Decimal("1E-100")),
                 (Decimal("1E-100"), Decimal("1E+100")),
                 (Decimal("1E-100"), 3)]
        for index, (gdp, population) in enumerate(cases):
            inputs = self.one_row(f"bad-exponent-{index}", gdp, population)
            output = self.root / f"rejected-exponent-{index}"
            with self.subTest(index=index), self.assertRaises(EvidenceError):
                export_study(*inputs, output)
            self.assert_no_output(output)

    def test_supported_exponent_boundaries_and_zero_with_large_spelling_remain_valid(self):
        inputs = self.group("boundary", {
            "gdp": {("USA", 2020): Decimal("1E-100"), ("USA", 2021): Decimal("1E+100"),
                    ("USA", 2022): Decimal("0E+999")},
            "population": {("USA", 2020): 1, ("USA", 2021): 1, ("USA", 2022): Decimal("1E+100")},
            "renewables": {("USA", 2020): 0, ("USA", 2021): 100, ("USA", 2022): 50},
        }, start=2020, end=2022)
        output, _ = self.build("boundaries", inputs)
        rows = read_study(output)
        self.assertEqual([Decimal(row["gdp_per_person"]) for row in rows],
                         [Decimal("1E-100"), Decimal("1E+100"), Decimal("0")])
        self.assertEqual(rows[-1]["gdp_per_person"], "0")
        self.assertEqual(rows[-1]["gdp_value"], "0E+999")
        self.assertTrue(all(row["pair_complete"] == "true" for row in rows))

    def test_raw_tampering_and_semantically_duplicated_observations_are_rejected(self):
        directory = self.inputs[2]
        manifest = read_json(directory / "provenance.json")
        raw_path = directory / manifest["raw_sources"][-1]["file"]
        original = raw_path.read_bytes()
        changed = original.replace(b'"value": 25', b'"value": 26')
        self.assertNotEqual(changed, original)
        raw_path.write_bytes(changed)
        output = self.root / "tampered"
        with self.assertRaises(EvidenceError):
            export_study(*self.inputs, output)
        self.assert_no_output(output)
        raw_path.write_bytes(original)

        def duplicate(payload):
            payload[1].append(deepcopy(payload[1][0]))
            payload[0]["total"] += 1

        mutate_source(directory, "observation", duplicate)
        output = self.root / "duplicate"
        before = file_snapshot(directory)
        with self.assertRaises(EvidenceError):
            export_study(*self.inputs, output)
        self.assert_no_output(output)
        self.assertEqual(file_snapshot(directory), before)

    def test_derived_input_csv_and_summaries_cannot_substitute_for_raw_evidence(self):
        expected, _ = self.build("expected")
        for index, directory in enumerate(self.inputs):
            if index == 0:
                (directory / "data.csv").unlink()
            else:
                (directory / "data.csv").write_text("invented replacement CSV", encoding="utf-8")
            manifest = read_json(directory / "provenance.json")
            manifest["row_count"] = 999999
            manifest["indicator"]["unit"] = "invented replacement unit"
            rewrite_manifest(directory, manifest)
        before = [file_snapshot(path) for path in self.inputs]
        actual, _ = self.build("reconstructed")
        for filename in ("study.csv", "summary.json"):
            self.assertEqual((expected / filename).read_bytes(), (actual / filename).read_bytes())
        self.assertEqual([file_snapshot(path) for path in self.inputs], before)

    def test_existing_outputs_and_output_inside_an_input_are_preserved(self):
        existing_file = self.root / "existing-file"
        existing_file.write_bytes(b"Earlier evidence")
        existing_directory = self.root / "existing-directory"
        existing_directory.mkdir()
        (existing_directory / "keep.txt").write_bytes(b"Earlier evidence")
        empty_directory = self.root / "empty-directory"
        empty_directory.mkdir()
        gdp = self.inputs[0]
        symlink = self.root / "input-alias"
        symlink.symlink_to(gdp, target_is_directory=True)
        candidates = [existing_file, existing_directory, empty_directory,
                      gdp / "new-parent" / "nested-study", symlink / "nested-study"]
        case_alias = gdp.with_name(gdp.name.upper())
        if case_alias.exists() and case_alias.samefile(gdp):
            candidates.append(case_alias / "missing-parent" / "nested-study")
        for output in candidates:
            before = file_snapshot(self.root)
            with self.subTest(output=output), self.assertRaises(EvidenceError):
                export_study(*self.inputs, output)
            self.assertEqual(file_snapshot(self.root), before)
            self.assertEqual(list(output.parent.glob(f".{output.name}-*")), [])

    def test_individually_valid_archives_can_exceed_csv_limit_without_partial_output(self):
        countries = ("ARG", "AUS", "BGD", "BRA", "CAN", "CHN", "DEU", "EGY", "FRA", "GBR",
                     "IDN", "IND", "JPN", "KEN", "KOR", "MEX", "NGA", "PAK", "USA", "ZAF")
        names = {}
        for country in countries:
            prefix = f"Synthetic {country} "
            names[country] = prefix + '"' * (1024 - len(prefix))
        keys = [(country, year) for country in countries for year in range(1960, 2010)]
        flags = {key: "Synthetic status " + "x" * (256 - len("Synthetic status ")) for key in keys}
        inputs = tuple(synthetic_bundle(
            self.root / "large-csv-inputs" / alias, alias, countries, 1960, 2009,
            {key: value for key in keys}, names=names, flags=flags,
        ) for alias, value in zip(ALIASES, (3, 2, 50)))
        # Just the three repeated, correctly CSV-escaped name columns already
        # exceed 5 MB. The source files each remain within their own limits.
        names_only_lower_bound = len(ALIASES) * sum(
            len(('"' + names[country].replace('"', '""') + '"').encode("utf-8"))
            for country, _ in keys
        )
        self.assertGreater(names_only_lower_bound, MAX_BYTES)
        for directory in inputs:
            self.assertEqual(read_json(directory / "provenance.json")["row_count"], 1000)
            self.assertLess((directory / "provenance.json").stat().st_size, MAX_MANIFEST_BYTES)
            self.assertLess((directory / "data.csv").stat().st_size, MAX_BYTES)
            self.assertTrue(all(path.stat().st_size < MAX_BYTES for path in (directory / "raw").iterdir()))
        before = [file_snapshot(directory) for directory in inputs]
        output = self.root / "too-large-csv"
        with self.assertRaisesRegex(EvidenceError, "study CSV exceeds the 5 MB limit"):
            export_study(*inputs, output)
        self.assert_no_output(output)
        self.assertEqual([file_snapshot(directory) for directory in inputs], before)

    def test_combined_metadata_limit_cleans_fully_staged_inputs(self):
        for alias, directory in zip(ALIASES, self.inputs):
            prefix = f"Synthetic {alias} metadata size fixture. "
            note = prefix + "x" * (340000 - len(prefix))
            mutate_source(directory, "metadata", lambda payload, note=note:
                          payload[1][0].update({"sourceNote": note}))
            manifest = read_json(directory / "provenance.json")
            manifest["indicator"]["source_note"] = note
            rewrite_manifest(directory, manifest)
            self.assertLess((directory / "provenance.json").stat().st_size, MAX_MANIFEST_BYTES)
            self.assertTrue(all(path.stat().st_size < MAX_BYTES for path in (directory / "raw").iterdir()))
        before = [file_snapshot(directory) for directory in self.inputs]
        output = self.root / "too-large-metadata"
        original_encoder = study_module._json_bytes
        observed = {}

        def observe_encoder(value):
            if value.get("artifact_type") == "output_per_person_study":
                stages = list(output.parent.glob(f".{output.name}-*"))
                self.assertEqual(len(stages), 1)
                embedded = sorted((stages[0] / "inputs").glob("*/provenance.json"))
                observed["completed_inputs"] = [path.parent.name for path in embedded]
                observed["input_manifest_sizes"] = [path.stat().st_size for path in embedded]
                observed["combined_bytes"] = len((json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            return original_encoder(value)

        # This observer leaves the production size limit and encoding unchanged.
        with patch("eco_evidence.study._json_bytes", side_effect=observe_encoder):
            with self.assertRaisesRegex(EvidenceError, "study metadata exceeds the 1 MB limit"):
                export_study(*self.inputs, output)
        self.assertEqual(observed["completed_inputs"], list(ALIASES))
        self.assertTrue(all(size < MAX_MANIFEST_BYTES for size in observed["input_manifest_sizes"]))
        self.assertGreater(observed["combined_bytes"], MAX_MANIFEST_BYTES)
        self.assert_no_output(output)
        self.assertEqual([file_snapshot(directory) for directory in self.inputs], before)

    def test_late_manifest_failure_cleans_three_embedded_bundles_without_altering_inputs(self):
        before = file_snapshot(self.root)
        original_write = Path.write_bytes
        completed = []

        def fail_final_manifest(path, data):
            if path.name == "provenance.json" and (path.parent / "study.csv").is_file():
                completed.extend(sorted(item.parent.name for item in (path.parent / "inputs").glob("*/provenance.json")))
                raise OSError("Synthetic disk full after all input bundles were staged")
            return original_write(path, data)

        output = self.root / "disk-full"
        with patch("pathlib.Path.write_bytes", fail_final_manifest):
            with self.assertRaises(OSError):
                export_study(*self.inputs, output)
        self.assertEqual(completed, list(ALIASES))
        self.assert_no_output(output)
        self.assertEqual(file_snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
