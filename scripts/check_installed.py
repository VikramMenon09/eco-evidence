"""Check the built package's public CLI, independently of source-tree imports.

Install a wheel, then run: python -I scripts/check_installed.py
Uses only the standard library and the existing, explicitly synthetic archive.
"""

import csv
import hashlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import subprocess
import sysconfig
import tempfile

import eco_evidence


def main():
    checkout = Path(__file__).resolve().parents[1]
    if Path(eco_evidence.__file__).resolve().is_relative_to(checkout / "eco_evidence"):
        raise RuntimeError("This check must import an installed wheel, not the source tree.")
    command = Path(sysconfig.get_path("scripts")) / ("eco-evidence.exe" if os.name == "nt" else "eco-evidence")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    fixture = checkout / "examples" / "synthetic-population"
    input_directories = (fixture, checkout / "examples/synthetic-gdp",
                         checkout / "examples/synthetic-population-revised",
                         checkout / "examples/output-energy-study")

    def fixture_snapshot():
        return {path.relative_to(checkout): path.read_bytes() if path.is_file() else None
                for directory in input_directories for path in (directory, *directory.rglob("*"))}

    original_fixtures = fixture_snapshot()
    installed_version = version("eco-evidence")
    if eco_evidence.__version__ != installed_version:
        raise RuntimeError("Package metadata and runtime versions disagree.")

    with tempfile.TemporaryDirectory(prefix="eco-installed-check-") as directory:
        def run(arguments, expected=0):
            result = subprocess.run(
                [str(command), *arguments], cwd=directory, env=environment,
                text=True, capture_output=True, timeout=30, check=False,
            )
            if result.returncode != expected:
                raise RuntimeError(f"Installed command returned {result.returncode}: {result.stderr}")
            return result

        if run(["--version"]).stdout.strip() != f"eco-evidence {installed_version}":
            raise RuntimeError("The installed version command disagrees with package metadata.")
        arguments = [
            "fetch", "--indicator", "population", "--countries", "USA",
            "--start-year", "2020", "--end-year", "2022",
            "--offline", str(fixture), "--output", "replay",
        ]
        result = json.loads(run(arguments).stdout)
        bundle = Path(directory) / "replay"
        csv_bytes = (bundle / "data.csv").read_bytes()
        if csv_bytes != (fixture / "data.csv").read_bytes():
            raise RuntimeError("Installed replay changed the synthetic fixture's CSV.")
        manifest = json.loads((bundle / "provenance.json").read_text(encoding="utf-8"))
        if manifest["tool_version"] != installed_version:
            raise RuntimeError("Exported provenance records the wrong installed version.")
        if manifest["csv_sha256"] != hashlib.sha256(csv_bytes).hexdigest():
            raise RuntimeError("Exported CSV checksum is incorrect.")
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
        expected_rows = [("2020", "not_returned", ""), ("2021", "missing", ""), ("2022", "available", "0")]
        if ([(row["year"], row["status"], row["value"]) for row in rows] != expected_rows
                or any(row["data_origin"] != "synthetic_fixture" for row in rows)):
            raise RuntimeError("Zero, missing, omitted or synthetic-origin semantics changed.")
        if result["rows"] != 3 or result["data_origin"] != "synthetic_fixture":
            raise RuntimeError("Installed CLI summary is inconsistent with its output.")
        def snapshot():
            return {path.relative_to(directory): path.read_bytes() if path.is_file() else None
                    for path in Path(directory).rglob("*")}

        original = snapshot()
        refused = run(arguments, expected=2)
        if refused.stdout or "Output already exists" not in refused.stderr:
            raise RuntimeError("The second command did not refuse the existing output as expected.")
        if original != snapshot():
            raise RuntimeError("Refusing an existing output changed the earlier evidence.")

        panel_arguments = ["panel", "--bundle", str(fixture), "--bundle",
                           str(checkout / "examples/synthetic-gdp"), "--output", "panel"]
        panel_result = json.loads(run(panel_arguments).stdout)
        panel = Path(directory) / "panel"
        panel_bytes = (panel / "panel.csv").read_bytes()
        coverage_bytes = (panel / "coverage.json").read_bytes()
        panel_manifest = json.loads((panel / "provenance.json").read_text(encoding="utf-8"))
        coverage = json.loads(coverage_bytes)
        if (panel_result["rows"] != 7 or panel_result["complete_cases"] != 1
                or panel_result["indicators"] != ["gdp", "population"]
                or panel_result["data_origin"] != "synthetic_fixture"):
            raise RuntimeError("Installed panel CLI summary disagrees with the synthetic example.")
        if (panel_manifest["tool_version"] != installed_version
                or panel_manifest["panel_csv_sha256"] != hashlib.sha256(panel_bytes).hexdigest()
                or panel_manifest["coverage_sha256"] != hashlib.sha256(coverage_bytes).hexdigest()):
            raise RuntimeError("Installed panel provenance has incorrect version or file checksums.")
        panel_rows = {(row["country_code"], row["year"]): row
                      for row in csv.DictReader(io.StringIO(panel_bytes.decode("utf-8")))}
        if (set(panel_rows) != {("IND", str(year)) for year in range(2021, 2024)}
                              | {("USA", str(year)) for year in range(2020, 2024)}
                or panel_rows[("USA", "2022")]["gdp_value"] != "12345.67890123456789"
                or panel_rows[("USA", "2022")]["population_value"] != "0"):
            raise RuntimeError("Installed panel lost country-years or exact source numeric values.")
        expected_counts = {
            "gdp": {"available": 3, "missing": 1, "not_returned": 2, "not_requested": 1},
            "population": {"available": 1, "missing": 1, "not_returned": 1, "not_requested": 4},
        }
        if (coverage["row_count"] != 7 or coverage["complete_case_count"] != 1
                or any(coverage["indicators"][alias]["counts"] != counts
                       for alias, counts in expected_counts.items())):
            raise RuntimeError("Installed panel coverage collapsed distinct missing-data states.")
        run(["panel", "--bundle", str(panel / "inputs/gdp"), "--bundle",
             str(panel / "inputs/population"), "--output", "panel-replay"])
        for filename, expected in (("panel.csv", panel_bytes), ("coverage.json", coverage_bytes)):
            if (Path(directory) / "panel-replay" / filename).read_bytes() != expected:
                raise RuntimeError("Embedded inputs did not reproduce the installed panel exactly.")
        original = snapshot()
        refused = run(panel_arguments, expected=2)
        if (refused.stdout or "Output already exists" not in refused.stderr or original != snapshot()):
            raise RuntimeError("Panel overwrite refusal failed to preserve existing research.")

        inspection = json.loads(run(["inspect", "--bundle", str(bundle), "--baseline",
                                     str(fixture), "--format", "json"]).stdout)
        if (inspection["tool_version"] != installed_version
                or inspection["bundle"]["data_origin"] != "synthetic_fixture"
                or inspection["bundle"]["indicator"]["unit"] != "people"
                or inspection["comparison"]["changed_row_count"] != 0
                or inspection["comparison"]["metadata_changes"]):
            raise RuntimeError("Installed inspection changed the meaning of an archived replay.")
        revised_fixture = checkout / "examples/synthetic-population-revised"
        revision = json.loads(run(["inspect", "--bundle", str(revised_fixture),
                                   "--baseline", str(fixture), "--format", "json"]).stdout)["comparison"]
        if (revision["changed_row_count"] != 3 or revision["numeric_value_change_count"] != 1
                or revision["row_field_change_counts"]["status"] != 2
                or revision["source_update_changed"]):
            raise RuntimeError("Installed inspection confused source dates, values or missing-data revisions.")
        text = run(["inspect", "--bundle", str(bundle)]).stdout
        if "SP.POP.TOTL" not in text or "synthetic_fixture" not in text:
            raise RuntimeError("Installed inspection text omitted source identity or origin.")
        refused = run(["inspect", "--bundle", str(bundle), "--baseline",
                       str(checkout / "examples/synthetic-gdp"), "--format", "json"], expected=2)
        if refused.stdout or "Traceback" in refused.stderr or original != snapshot():
            raise RuntimeError("Inspection changed inputs or failed to reject incomparable queries cleanly.")

        study_example = checkout / "examples/output-energy-study"

        def study_arguments(inputs, output):
            return ["study", "--gdp-bundle", str(inputs / "gdp"), "--population-bundle",
                    str(inputs / "population"), "--renewables-bundle", str(inputs / "renewables"),
                    "--output", str(output)]

        study = Path(directory) / "study"
        study_result = json.loads(run(study_arguments(study_example / "inputs", study)).stdout)
        if (study_result["rows"], study_result["derived"], study_result["complete_pairs"], study_result["data_origin"]) != (30, 30, 18, "worldbank_api"):
            raise RuntimeError("Installed worked study changed coverage or source origin.")
        study_manifest = json.loads((study / "provenance.json").read_text(encoding="utf-8"))
        if study_manifest["tool_version"] != installed_version:
            raise RuntimeError("Study records the wrong installed version.")
        study_replay = Path(directory) / "study-replay"
        run(study_arguments(study / "inputs", study_replay))
        for filename, hash_key in (("study.csv", "study_csv_sha256"), ("summary.json", "summary_sha256")):
            content = (study / filename).read_bytes()
            if (content != (study_example / filename).read_bytes()
                    or content != (study_replay / filename).read_bytes()
                    or hashlib.sha256(content).hexdigest() != study_manifest[hash_key]):
                raise RuntimeError("Installed study cannot reproduce its archived calculation and coverage.")
        original = snapshot()
        refused = run(study_arguments(study_example / "inputs", study), expected=2)
        if refused.stdout or "Output already exists" not in refused.stderr or original != snapshot():
            raise RuntimeError("Study overwrite refusal did not preserve earlier research.")
        if fixture_snapshot() != original_fixtures:
            raise RuntimeError("An installed command changed one of the original example archives.")
    print(f"Installed eco-evidence {installed_version}: fetch, panel, inspect, study, offline replay and preservation passed.")


if __name__ == "__main__":
    main()
