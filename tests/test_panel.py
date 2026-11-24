"""Panel acceptance tests; all observations and source text are synthetic fixtures."""

from copy import deepcopy
import csv
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eco_evidence.core import (EvidenceError, MAX_MANIFEST_BYTES, Query, Snapshot,
                               digest, export_bundle, request_url)
from eco_evidence.panel import export_panel
from eco_evidence.registry import get_indicator


STAMP = "2026-11-23T16:30:00Z"
PRECISE = "1234567890123.1234567890123456789"
STATUSES = ("available", "missing", "not_returned", "not_requested")


def synthetic_bundle(directory, alias, countries, start, end, values, *, names=None,
                     flags=None, revision="2026-11-21"):
    """Create a valid archive of explicitly invented World Bank-shaped responses.

    A missing mapping key is omitted from the response; None is explicit null.
    Decimal values are inserted as JSON number literals rather than binary floats.
    """
    query = Query(alias, tuple(countries), start, end)
    indicator = get_indicator(alias)
    country_path = "country/" + ";".join(query.countries)
    names = names or {}
    flags = flags or {}
    definitions = [{"id": indicator.code, "name": indicator.name, "unit": "",
                    "source": {"id": "2"},
                    "sourceNote": f"Synthetic {alias} fixture. Values are invented for testing.",
                    "sourceOrganization": "EcoEvidence synthetic fixture authors"}]
    countries_data = [{"id": code, "name": names.get(code, f"Synthetic {code}"),
                       "region": {"id": "TEST"}} for code in query.countries]
    observations = [{"countryiso3code": code, "date": str(year),
                     "indicator": {"id": indicator.code, "value": indicator.name},
                     "unit": "", "value": value,
                     "obs_status": flags.get((code, year), "")}
                    for (code, year), value in sorted(values.items(), reverse=True)]

    def page(items, is_data=False):
        header = {"page": 1, "pages": 1, "per_page": 1000, "total": len(items)}
        if is_data:
            header.update({"sourceid": "2", "lastupdated": revision})
        decimal_literals = []

        def encode_number(value):
            if isinstance(value, Decimal):
                assert value.is_finite()
                marker = f"__synthetic_decimal_{len(decimal_literals)}__"
                decimal_literals.append((marker, str(value)))
                return marker
            raise TypeError(type(value).__name__)

        raw = json.dumps([header, items], default=encode_number, ensure_ascii=False)
        for marker, literal in decimal_literals:
            raw = raw.replace(json.dumps(marker), literal)
        return raw.encode("utf-8")

    sources = {
        request_url(country_path, {}, 1): page(countries_data),
        request_url(f"indicator/{indicator.code}", {"source": 2}, 1): page(definitions),
        request_url(f"{country_path}/indicator/{indicator.code}",
                    {"source": 2, "date": f"{start}:{end}"}, 1): page(observations, True),
    }

    class FixtureReader:
        def get(self, url):
            return Snapshot(url, sources[url], STAMP, "synthetic_fixture")

    export_bundle(query, FixtureReader(), directory)
    return Path(directory)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_snapshot(directory):
    root = Path(directory)
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


def read_panel(directory):
    return list(csv.DictReader(io.StringIO((Path(directory) / "panel.csv").read_text(encoding="utf-8"))))


def rewrite_manifest(bundle, value):
    (Path(bundle) / "provenance.json").write_text(json.dumps(value), encoding="utf-8")


def mutate_source(bundle, kind, mutation):
    """Change test bytes and their checksum to reach semantic validation."""
    manifest = read_json(bundle / "provenance.json")
    for item in manifest["raw_sources"]:
        is_metadata = "/v2/indicator/" in item["url"]
        is_observation = "/indicator/" in item["url"] and not is_metadata
        if (kind == "metadata" and is_metadata) or (kind == "observation" and is_observation):
            path = bundle / item["file"]
            payload = read_json(path)
            mutation(payload)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            path.write_bytes(raw)
            item["sha256"] = digest(raw)
            rewrite_manifest(bundle, manifest)
            return
    raise AssertionError("Synthetic source not found")


class PanelAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="eco-panel-acceptance-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.gdp = synthetic_bundle(
            self.root / "gdp", "gdp", ("USA", "IND"), 2020, 2021,
            {("IND", 2020): Decimal(PRECISE), ("IND", 2021): None,
             ("USA", 2020): 0, ("USA", 2021): 20},
            names={"USA": "Synthetic USA, earlier name"},
            flags={("IND", 2020): "Synthetic estimate flag"},
            revision="2026-11-20",
        )
        self.population = synthetic_bundle(
            self.root / "population", "population", ("USA", "CAN"), 2021, 2023,
            {("CAN", 2021): 0, ("CAN", 2022): None,
             ("USA", 2021): 100, ("USA", 2023): 102},
            names={"USA": "Synthetic USA, later name"},
            revision="2026-11-21",
        )

    def build(self, name="panel", inputs=None):
        output = self.root / name
        provenance = export_panel(inputs or [self.population, self.gdp], output)
        return output, provenance

    def assert_no_partial_output(self, output):
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.glob(f".{output.name}-*")), [])

    def test_outer_union_preserves_mismatched_country_year_windows_and_four_statuses(self):
        with patch("eco_evidence.core.open_official", side_effect=AssertionError("Network forbidden")):
            output, provenance = self.build()
        rows = read_panel(output)
        self.assertEqual([(row["country_code"], row["year"]) for row in rows], [
            ("CAN", "2021"), ("CAN", "2022"), ("CAN", "2023"),
            ("IND", "2020"), ("IND", "2021"),
            ("USA", "2020"), ("USA", "2021"), ("USA", "2022"), ("USA", "2023"),
        ])
        indexed = {(row["country_code"], row["year"]): row for row in rows}
        self.assertEqual(indexed[("IND", "2020")]["gdp_value"], PRECISE)
        self.assertEqual(indexed[("USA", "2020")]["gdp_value"], "0")
        self.assertEqual(indexed[("USA", "2020")]["gdp_status"], "available")
        self.assertEqual(indexed[("CAN", "2021")]["population_value"], "0")
        self.assertEqual(indexed[("CAN", "2021")]["population_status"], "available")
        self.assertEqual(indexed[("CAN", "2022")]["population_status"], "missing")
        self.assertEqual(indexed[("CAN", "2023")]["population_status"], "not_returned")
        self.assertEqual(indexed[("IND", "2020")]["population_status"], "not_requested")
        self.assertEqual(indexed[("CAN", "2021")]["gdp_status"], "not_requested")
        for row in rows:
            for alias in ("gdp", "population"):
                if row[f"{alias}_status"] != "available":
                    self.assertEqual(row[f"{alias}_value"], "")
                self.assertEqual(row[f"{alias}_unit"], get_indicator(alias).unit)
            self.assertEqual(row["data_origin"], "synthetic_fixture")
        self.assertEqual(provenance["row_count"], 9)
        self.assertEqual(provenance["data_origin"], "synthetic_fixture")

    def test_names_flags_original_units_and_metadata_remain_indicator_specific(self):
        output, provenance = self.build()
        row = next(row for row in read_panel(output) if row["country_code"] == "USA" and row["year"] == "2021")
        self.assertEqual(row["gdp_country_name"], "Synthetic USA, earlier name")
        self.assertEqual(row["population_country_name"], "Synthetic USA, later name")
        self.assertEqual(row["gdp_unit"], "constant 2015 US$")
        self.assertEqual(row["population_unit"], "people")
        rows = {(row["country_code"], row["year"]): row for row in read_panel(output)}
        self.assertEqual(rows[("IND", "2020")]["gdp_source_obs_status"], "Synthetic estimate flag")
        self.assertEqual(rows[("IND", "2020")]["population_country_name"], "")
        self.assertEqual(rows[("IND", "2020")]["population_source_obs_status"], "")
        inputs = {item["alias"]: item for item in provenance["inputs"]}
        self.assertEqual(inputs["gdp"]["source"]["last_updated"], "2026-11-20")
        self.assertEqual(inputs["population"]["source"]["last_updated"], "2026-11-21")
        for alias, item in inputs.items():
            self.assertEqual(item["data_origin"], "synthetic_fixture")
            self.assertEqual(item["indicator"]["unit"], get_indicator(alias).unit)
            self.assertEqual(item["source"]["id"], "2")
            self.assertIn("CC BY 4.0", item["source"]["license"])
            self.assertEqual(item["indicator"]["source_organization"], "EcoEvidence synthetic fixture authors")

    def test_coverage_counts_union_rows_and_only_joint_available_cells_as_complete(self):
        output, _ = self.build()
        coverage = read_json(output / "coverage.json")
        self.assertEqual(coverage["row_count"], 9)
        self.assertEqual(coverage["complete_case_count"], 1)
        self.assertEqual(coverage["indicators"]["gdp"]["counts"], {
            "available": 3, "missing": 1, "not_returned": 0, "not_requested": 5,
        })
        self.assertEqual(coverage["indicators"]["population"]["counts"], {
            "available": 3, "missing": 1, "not_returned": 2, "not_requested": 3,
        })
        for alias, item in coverage["indicators"].items():
            self.assertEqual(set(item["counts"]), set(STATUSES))
            self.assertEqual(sum(item["counts"].values()), coverage["row_count"])
            self.assertEqual(item["indicator_code"], get_indicator(alias).code)
            self.assertEqual(item["unit"], get_indicator(alias).unit)

    def test_three_indicators_use_sorted_alias_columns_without_dropping_rows(self):
        renewables = synthetic_bundle(self.root / "renewables", "renewables", ("USA",), 2020, 2022,
                                     {("USA", 2020): 50, ("USA", 2021): 51, ("USA", 2022): None})
        output, _ = self.build(inputs=[renewables, self.population, self.gdp])
        rows = read_panel(output)
        expected = ["country_code", "year", "data_origin"]
        for alias in ("gdp", "population", "renewables"):
            expected.extend(f"{alias}_{field}" for field in ("value", "status", "unit", "country_name", "source_obs_status"))
        self.assertEqual(list(rows[0]), expected)
        self.assertEqual(len(rows), 9)
        self.assertEqual(read_json(output / "coverage.json")["complete_case_count"], 1)
        self.assertEqual(read_json(output / "coverage.json")["indicators"]["renewables"]["counts"], {
            "available": 2, "missing": 1, "not_returned": 0, "not_requested": 6,
        })

    def test_order_independence_embedded_replay_hashes_and_input_preservation(self):
        before = {name: file_snapshot(path) for name, path in (("gdp", self.gdp), ("population", self.population))}
        original_manifests = {alias: digest((path / "provenance.json").read_bytes())
                              for alias, path in (("gdp", self.gdp), ("population", self.population))}
        first, provenance = self.build("first", [self.population, self.gdp])
        second, _ = self.build("second", [self.gdp, self.population])
        with patch("eco_evidence.core.open_official", side_effect=AssertionError("Network forbidden")):
            replay, _ = self.build("replay", [first / "inputs/gdp", first / "inputs/population"])
        for filename in ("panel.csv", "coverage.json"):
            self.assertEqual((first / filename).read_bytes(), (second / filename).read_bytes())
            self.assertEqual((first / filename).read_bytes(), (replay / filename).read_bytes())
        self.assertEqual(provenance["artifact_type"], "indicator_panel")
        self.assertEqual(provenance["panel_csv_sha256"], digest((first / "panel.csv").read_bytes()))
        self.assertEqual(provenance["coverage_sha256"], digest((first / "coverage.json").read_bytes()))
        for entry in provenance["inputs"]:
            alias = entry["alias"]
            self.assertEqual(entry["bundle_path"], f"inputs/{alias}")
            self.assertEqual(entry["original_manifest_sha256"], original_manifests[alias])
            embedded = first / entry["bundle_path"]
            self.assertEqual(entry["embedded_manifest_sha256"], digest((embedded / "provenance.json").read_bytes()))
            manifest = read_json(embedded / "provenance.json")
            original = self.gdp if alias == "gdp" else self.population
            source_map = {item["url"]: item for item in read_json(original / "provenance.json")["raw_sources"]}
            for raw in manifest["raw_sources"]:
                self.assertEqual(raw["retrieved_at"], STAMP)
                self.assertEqual((embedded / raw["file"]).read_bytes(), (original / source_map[raw["url"]]["file"]).read_bytes())
                self.assertEqual(raw["sha256"], source_map[raw["url"]]["sha256"])
            self.assertEqual(file_snapshot(original), before[alias])

    def test_reconstructs_raw_evidence_instead_of_trusting_derived_csv_and_summary(self):
        (self.gdp / "data.csv").write_text("country_code,year,value\nUSA,2020,999999\n", encoding="utf-8")
        manifest = read_json(self.gdp / "provenance.json")
        manifest["indicator"]["unit"] = "invented derived unit"
        manifest["counts"] = {"available": 999999}
        rewrite_manifest(self.gdp, manifest)
        output, provenance = self.build()
        row = next(row for row in read_panel(output) if row["country_code"] == "USA" and row["year"] == "2020")
        self.assertEqual(row["gdp_value"], "0")
        self.assertEqual(row["gdp_unit"], "constant 2015 US$")
        entry = next(item for item in provenance["inputs"] if item["alias"] == "gdp")
        self.assertEqual(entry["indicator"]["unit"], "constant 2015 US$")
        self.assertNotEqual((output / "inputs/gdp/data.csv").read_bytes(), (self.gdp / "data.csv").read_bytes())

    def test_missing_derived_csv_is_reconstructed_from_archived_raw_responses(self):
        expected, _ = self.build("expected")
        (self.gdp / "data.csv").unlink()
        (self.population / "data.csv").unlink()
        with patch("eco_evidence.core.open_official", side_effect=AssertionError("Network forbidden")):
            output, _ = self.build("without-derived-csv")
        self.assertEqual((output / "panel.csv").read_bytes(), (expected / "panel.csv").read_bytes())
        self.assertFalse((self.gdp / "data.csv").exists())
        self.assertFalse((self.population / "data.csv").exists())

    def test_output_inside_an_input_or_its_symlink_is_rejected_without_changes(self):
        alias = self.root / "gdp-alias"
        alias.symlink_to(self.gdp, target_is_directory=True)
        for output in (self.gdp / "child-panel", alias / "child-panel"):
            before = file_snapshot(self.gdp)
            with self.subTest(output=output), self.assertRaises(EvidenceError):
                export_panel([self.gdp, self.population], output)
            self.assertEqual(file_snapshot(self.gdp), before)
            self.assert_no_partial_output(output)

    def test_case_alias_output_cannot_add_anything_to_an_input_archive(self):
        alias = self.gdp.with_name(self.gdp.name.upper())
        if not alias.exists() or not alias.samefile(self.gdp):
            self.skipTest("This filesystem treats differently cased directory names as distinct.")
        self.assertNotEqual(str(alias), str(self.gdp))
        for parts in (("aliased-panel",), ("new-parent", "new-child", "aliased-panel")):
            output = alias.joinpath(*parts)
            before = file_snapshot(self.gdp)
            with self.subTest(parts=parts):
                with self.assertRaises(EvidenceError):
                    export_panel([self.gdp, self.population], output)
                self.assertEqual(file_snapshot(self.gdp), before)
                self.assertFalse((self.gdp / parts[0]).exists())
                self.assert_no_partial_output(output)

    def test_unicode_alias_output_cannot_add_anything_to_an_input_archive(self):
        original = synthetic_bundle(self.root / "gdp-caf\u00e9", "gdp", ("USA",), 2020, 2020,
                                    {("USA", 2020): 17})
        alias = self.root / "gdp-cafe\u0301"
        if not alias.exists() or not alias.samefile(original):
            self.skipTest("This filesystem treats Unicode normalization spellings as distinct.")
        self.assertNotEqual(str(alias), str(original))
        for parts in (("aliased-panel",), ("new-parent", "new-child", "aliased-panel")):
            output = alias.joinpath(*parts)
            before = file_snapshot(original)
            with self.subTest(parts=parts):
                with self.assertRaises(EvidenceError):
                    export_panel([original, self.population], output)
                self.assertEqual(file_snapshot(original), before)
                self.assertFalse((original / parts[0]).exists())
                self.assert_no_partial_output(output)

    def test_dangling_output_symlink_is_preserved(self):
        output = self.root / "dangling"
        target = self.root / "missing-target"
        output.symlink_to(target, target_is_directory=True)
        with self.assertRaises(EvidenceError):
            export_panel([self.gdp, self.population], output)
        self.assertTrue(output.is_symlink())
        self.assertEqual(output.readlink(), target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".dangling-*")), [])

    def test_rejects_same_indicator_from_same_or_different_archives(self):
        duplicate = synthetic_bundle(self.root / "other-population", "population", ("IND",), 2020, 2020,
                                     {("IND", 2020): 1})
        for number, inputs in enumerate(([self.gdp, self.gdp], [self.population, duplicate])):
            output = self.root / f"duplicate-{number}"
            with self.subTest(inputs=inputs), self.assertRaises(EvidenceError):
                export_panel(inputs, output)
            self.assert_no_partial_output(output)

    def test_rejects_mixed_origin_labels_without_publishing_a_panel(self):
        # Deliberately falsify one fixture's origin solely to test rejection of a mix.
        manifest = read_json(self.gdp / "provenance.json")
        manifest["data_origin"] = "worldbank_api"
        rewrite_manifest(self.gdp, manifest)
        output = self.root / "mixed"
        with self.assertRaises(EvidenceError):
            export_panel([self.gdp, self.population], output)
        self.assert_no_partial_output(output)

    def test_rejects_wrong_count_and_non_sequence_arguments(self):
        for number, inputs in enumerate(([], [self.gdp], [self.gdp] * 4, None, str(self.gdp))):
            output = self.root / f"count-{number}"
            with self.subTest(inputs=inputs), self.assertRaises(EvidenceError):
                export_panel(inputs, output)
            self.assert_no_partial_output(output)

    def test_rejects_invalid_queries_and_ambiguous_or_oversized_manifests(self):
        original = (self.gdp / "provenance.json").read_bytes()
        parsed = json.loads(original)
        bad_queries = [None, [], {**parsed["query"], "unexpected": 1},
                       {**parsed["query"], "countries": "USA"},
                       {**parsed["query"], "countries": ["USA", "USA"]},
                       {**parsed["query"], "start_year": True},
                       {**parsed["query"], "end_year": "2021"},
                       {**parsed["query"], "indicator": "unsupported"},
                       {**parsed["query"], "indicator": "gdp"},
                       {**parsed["query"], "countries": ["USA", "IND"]}]
        cases = []
        for query in bad_queries:
            manifest = deepcopy(parsed)
            manifest["query"] = query
            cases.append(json.dumps(manifest).encode())
        missing = deepcopy(parsed)
        del missing["query"]
        cases.extend([json.dumps(missing).encode(), b'[]', b'{"query":null,"query":{}}', b' ' * (MAX_MANIFEST_BYTES + 1)])
        for number, raw in enumerate(cases):
            (self.gdp / "provenance.json").write_bytes(raw)
            output = self.root / f"malformed-{number}"
            with self.subTest(number=number), self.assertRaises(EvidenceError):
                export_panel([self.gdp, self.population], output)
            self.assert_no_partial_output(output)
        (self.gdp / "provenance.json").write_bytes(original)

    def test_raw_checksum_and_source_semantic_failures_do_not_publish(self):
        manifest = read_json(self.population / "provenance.json")
        raw_path = self.population / manifest["raw_sources"][-1]["file"]
        original = raw_path.read_bytes()
        # Still valid JSON and valid number; only the stored content hash rejects it.
        raw_path.write_bytes(original.replace(b'"value": 102', b'"value": 103'))
        self.assertNotEqual(original, raw_path.read_bytes())
        output = self.root / "bad-hash"
        with self.assertRaises(EvidenceError):
            export_panel([self.gdp, self.population], output)
        self.assert_no_partial_output(output)
        raw_path.write_bytes(original)

        def duplicate(payload):
            payload[1].append(deepcopy(payload[1][0]))
            payload[0]["total"] += 1

        mutate_source(self.population, "observation", duplicate)
        output = self.root / "duplicate-observation"
        with self.assertRaises(EvidenceError):
            export_panel([self.gdp, self.population], output)
        self.assert_no_partial_output(output)

    def test_verified_hash_does_not_bypass_metadata_unit_validation(self):
        mutate_source(self.gdp, "metadata", lambda payload: payload[1][0].update({"unit": "thousands"}))
        output = self.root / "changed-unit"
        with self.assertRaises(EvidenceError):
            export_panel([self.gdp, self.population], output)
        self.assert_no_partial_output(output)

    def test_existing_output_file_and_directory_are_preserved(self):
        for name, is_directory in (("file", False), ("empty-directory", True), ("nonempty-directory", True)):
            output = self.root / name
            if is_directory:
                output.mkdir()
                if name.startswith("nonempty"):
                    (output / "keep.txt").write_text("earlier evidence", encoding="utf-8")
            else:
                output.write_bytes(b"earlier evidence")
            before = file_snapshot(output) if is_directory else output.read_bytes()
            with self.subTest(name=name), self.assertRaises(EvidenceError):
                export_panel([self.gdp, self.population], output)
            self.assertEqual(file_snapshot(output) if is_directory else output.read_bytes(), before)
            self.assertEqual(list(self.root.glob(f".{name}-*")), [])

    def test_late_output_write_failure_removes_staging_and_preserves_inputs(self):
        before = file_snapshot(self.root)
        original_write = Path.write_bytes
        completed_input_manifests = []

        def fail_final_manifest(path, data):
            if path.name == "provenance.json":
                if path.parent.name.startswith(".disk-full-"):
                    completed_input_manifests.extend(sorted(
                        item.parent.name for item in (path.parent / "inputs").glob("*/provenance.json")
                    ))
                    raise OSError("Synthetic disk full while writing final panel manifest")
            return original_write(path, data)

        output = self.root / "disk-full"
        with patch("pathlib.Path.write_bytes", fail_final_manifest):
            with self.assertRaises(OSError):
                export_panel([self.gdp, self.population], output)
        self.assertEqual(completed_input_manifests, ["gdp", "population"])
        self.assert_no_partial_output(output)
        self.assertEqual(file_snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
