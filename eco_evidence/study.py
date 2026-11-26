"""Reproducible GDP-per-person and renewable-energy-share worked study.

This is transparent arithmetic and coverage reporting, not a statistical model.
No source observation is imputed, ranked, or discarded from the common query.
"""

from collections import Counter
import csv
from decimal import (Clamped, Context, Decimal, DecimalException, DivisionByZero,
                     InvalidOperation, Overflow, ROUND_HALF_EVEN, Subnormal,
                     Underflow, localcontext)
import io
import json
from pathlib import Path
import shutil
import tempfile

from . import __version__
from .archive import read_bundle
from .core import (EvidenceError, MAX_BYTES, MAX_MANIFEST_BYTES, digest,
                   export_bundle, read_bounded_file, utc_now)
from .panel import _FrozenReader, _output_is_inside_input

REQUIRED_CODES = {"gdp": "NY.GDP.MKTP.KD", "population": "SP.POP.TOTL", "renewables": "EG.FEC.RNEW.ZS"}
SOURCE_FIELDS = ("value", "status", "unit", "country_name", "source_obs_status")
SOURCE_STATUSES = ("available", "missing", "not_returned")
PER_PERSON_UNIT = "constant 2015 US$ per person"
PRECISION = 28
MIN_EXPONENT = -100
MAX_EXPONENT = 100
MAX_STUDY_BYTES = MAX_BYTES


def _json_bytes(value):
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise EvidenceError("Generated study metadata exceeds the 1 MB limit.")
    return encoded


def _source_number(alias, row):
    if row["status"] != "available":
        return None
    try:
        number = Decimal(row["value"])
    except DecimalException as exc:
        raise EvidenceError("Study source value cannot be represented as a finite decimal.") from exc
    if not number.is_finite():
        raise EvidenceError("Study source value must be a finite decimal.")
    where = f"{alias} {row['country_code']} {row['year']}"
    if number < 0 or alias == "renewables" and number > 100:
        raise EvidenceError(f"Study source value is outside its supported domain: {where}.")
    if (alias in ("gdp", "population") and not number.is_zero()
            and not MIN_EXPONENT <= number.adjusted() <= MAX_EXPONENT):
        raise EvidenceError(f"Study operand exceeds the supported adjusted-exponent range [-100, 100]: {where}.")
    return number


def _decimal_context():
    # All settings, including flags and traps, are independent of both the
    # caller's current context and mutable decimal.DefaultContext settings.
    return Context(prec=PRECISION, rounding=ROUND_HALF_EVEN,
                   Emin=MIN_EXPONENT, Emax=MAX_EXPONENT, capitals=1, clamp=0,
                   flags=[], traps=[InvalidOperation, DivisionByZero, Overflow,
                                    Underflow, Subnormal, Clamped])


def _per_person(gdp, population, country, year):
    if gdp.is_zero():
        return "0"
    # Inexact/Rounded are expected at the declared precision. String conversion
    # stays inside this context because Decimal scientific notation uses capitals.
    try:
        with localcontext(_decimal_context()):
            result = gdp / population
            if (not result.is_finite() or result.is_zero()
                    or not MIN_EXPONENT <= result.adjusted() <= MAX_EXPONENT):
                raise EvidenceError(f"GDP per person is outside the supported decimal calculation range: {country} {year}.")
            return str(result)
    except DecimalException as exc:
        raise EvidenceError(f"GDP per person is outside the supported decimal calculation range: {country} {year}.") from exc

def _year_summary(year, rows):
    selected = [row for row in rows if row["year"] == year]
    excluded = [
        {"country_code": row["country_code"], "reasons": row["pair_exclusion_reasons"].split(";")}
        for row in selected if row["pair_complete"] == "false"
    ]
    counts = Counter(reason for item in excluded for reason in item["reasons"])
    return {
        "year": year, "row_count": len(selected),
        "derived_count": sum(row["gdp_per_person_status"] == "derived" for row in selected),
        "pair_complete_count": sum(row["pair_complete"] == "true" for row in selected),
        "eligible_country_codes": [row["country_code"] for row in selected if row["pair_complete"] == "true"],
        "excluded": excluded,
        "exclusion_reason_counts": dict(sorted(counts.items())),
        "source_status_counts": {
            alias: {status: sum(row[f"{alias}_status"] == status for row in selected) for status in SOURCE_STATUSES}
            for alias in REQUIRED_CODES
        },
    }


def export_study(gdp_path, population_path, renewables_path, output):
    """Write one self-contained, reproducible study from the three input archives."""
    try:
        output = Path(output)
        resolved_output = output.resolve()
    except (TypeError, ValueError, RuntimeError) as exc:
        raise EvidenceError("Study output must be a valid new directory path.") from exc
    if output.exists() or output.is_symlink():
        raise EvidenceError("Output already exists; choose a new directory to preserve earlier evidence.")
    # Reconstructed source numeric strings must also ignore ambient exponent
    # capitalization settings; original raw bytes remain untouched.
    with localcontext(_decimal_context()):
        inputs = {alias: read_bundle(path) for alias, path in zip(REQUIRED_CODES, (gdp_path, population_path, renewables_path))}
    for alias, item in inputs.items():
        if item["query"].indicator != REQUIRED_CODES[alias]:
            raise EvidenceError(f"The {alias} input must contain reviewed indicator {REQUIRED_CODES[alias]}.")
    reference = inputs["gdp"]["query"]
    common = (reference.countries, reference.start_year, reference.end_year)
    if any((item["query"].countries, item["query"].start_year, item["query"].end_year) != common for item in inputs.values()):
        raise EvidenceError("Study inputs must have exactly the same countries and year window.")
    origins = {item["provenance"]["data_origin"] for item in inputs.values()}
    if len(origins) != 1:
        raise EvidenceError("Study inputs must have the same data origin; cannot mix synthetic and live-source bundles.")
    origin = origins.pop()
    if _output_is_inside_input(resolved_output, [item["directory"] for item in inputs.values()]):
        raise EvidenceError("Study output must be outside every input bundle to preserve earlier evidence.")
    indexed = {alias: {(row["country_code"], row["year"]): row for row in item["rows"]}
               for alias, item in inputs.items()}
    if any(rows.keys() != indexed["gdp"].keys() for rows in indexed.values()):
        raise EvidenceError("Study source rows do not match the common query.")

    rows = []
    for country, year in sorted(indexed["gdp"]):
        row = {"country_code": country, "year": year, "data_origin": origin}
        numbers = {}
        for alias in REQUIRED_CODES:
            source = indexed[alias][(country, year)]
            numbers[alias] = _source_number(alias, source)
            for field in SOURCE_FIELDS:
                row[f"{alias}_{field}"] = source[field]
        reasons = []
        for alias in ("gdp", "population"):
            if row[f"{alias}_status"] != "available":
                reasons.append(f"{alias}_{row[f'{alias}_status']}")
        if numbers["population"] is not None and numbers["population"].is_zero():
            reasons.append("population_zero")
        derived = not reasons
        row["gdp_per_person"] = _per_person(numbers["gdp"], numbers["population"], country, year) if derived else ""
        row["gdp_per_person_unit"] = PER_PERSON_UNIT
        row["gdp_per_person_status"] = "derived" if derived else "not_derived"
        row["gdp_per_person_reason"] = ";".join(reasons)
        pair_reasons = list(reasons)
        if row["renewables_status"] != "available":
            pair_reasons.append(f"renewables_{row['renewables_status']}")
        row["pair_complete"] = "false" if pair_reasons else "true"
        row["pair_exclusion_reasons"] = ";".join(pair_reasons)
        rows.append(row)

    derived_count = sum(row["gdp_per_person_status"] == "derived" for row in rows)
    pair_count = sum(row["pair_complete"] == "true" for row in rows)
    by_year = [_year_summary(year, rows) for year in range(reference.start_year, reference.end_year + 1)]
    all_reasons = Counter(reason for item in by_year for entry in item["excluded"] for reason in entry["reasons"])
    summary = {
        "schema_version": 1, "artifact_type": "output_per_person_study_summary", "data_origin": origin,
        "row_count": len(rows), "derived_count": derived_count, "pair_complete_count": pair_count,
        "pair_complete_definition": "GDP per person is derived and the renewable-energy share is available; zero values remain available.",
        "exclusion_count_definition": "Counts are country-year rows per reason; a row may have multiple reasons, so reason counts may overlap.",
        "exclusion_reason_counts": dict(sorted(all_reasons.items())),
        "source_status_counts": {alias: dict(item["provenance"]["counts"]) for alias, item in inputs.items()},
        "by_year": by_year,
    }
    summary_bytes = _json_bytes(summary)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = buffer.getvalue().encode("utf-8")
    if len(csv_bytes) > MAX_STUDY_BYTES:
        raise EvidenceError("Generated study CSV exceeds the 5 MB limit; no rows are silently dropped.")
    provenance = {
        "schema_version": 1, "artifact_type": "output_per_person_study", "tool": "eco-evidence",
        "tool_version": __version__, "created_at": utc_now(), "data_origin": origin,
        "query": {"countries": list(reference.countries), "start_year": reference.start_year, "end_year": reference.end_year},
        "row_count": len(rows), "derived_count": derived_count, "pair_complete_count": pair_count,
        "calculation": {
            "name": "GDP per person", "formula": "NY.GDP.MKTP.KD / SP.POP.TOTL", "unit": PER_PERSON_UNIT,
            "precision_significant_digits": PRECISION, "rounding": "ROUND_HALF_EVEN",
            "nonzero_operand_adjusted_exponent_range": [MIN_EXPONENT, MAX_EXPONENT],
            "nonzero_result_adjusted_exponent_range": [MIN_EXPONENT, MAX_EXPONENT],
            "exponent_scope": "GDP and population operands and their quotient; computation support limits, not economic plausibility bounds.",
            "trapped_signals": ["InvalidOperation", "DivisionByZero", "Overflow", "Underflow", "Subnormal", "Clamped"],
            "zero_numerator": "A reported GDP of zero with a positive population derives the exact string 0.",
        },
        "domain_policy": "Available GDP and population must be nonnegative; available renewable shares must be in [0, 100]. Invalid values reject the whole export. Zero population is retained with population_zero exclusion.",
        "interpretation": "Constant-price domestic output per person paired with renewable share of final energy consumption. Descriptive arithmetic only; not household income, purchasing power, company performance, a ranking, or a causal estimate.",
        "missingness_policy": "Original values, units, flags, names and available/missing/not_returned statuses are retained. No interpolation or zero filling. GDP per person remains available when the renewable share is missing.",
        "input_data_policy": "Reconstruct source rows from verified archived raw responses; derived input CSV files and manifest summaries are not treated as evidence.",
        "lineage_policy": "Manifest hashes identify the bytes read; they are not source signatures or independent authentication.",
        "study_csv_file": "study.csv", "study_csv_sha256": digest(csv_bytes),
        "summary_file": "summary.json", "summary_sha256": digest(summary_bytes), "inputs": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        (temporary / "study.csv").write_bytes(csv_bytes)
        (temporary / "summary.json").write_bytes(summary_bytes)
        for alias, item in inputs.items():
            bundle_path = f"inputs/{alias}"
            with localcontext(_decimal_context()):
                embedded = export_bundle(item["query"], _FrozenReader(item["snapshots"]), temporary / bundle_path)
            embedded_manifest = read_bounded_file(temporary / bundle_path / "provenance.json", MAX_MANIFEST_BYTES)
            provenance["inputs"].append({
                "alias": alias, "bundle_path": bundle_path, "data_origin": embedded["data_origin"],
                "original_manifest_sha256": item["original_manifest_sha256"],
                "embedded_manifest_sha256": digest(embedded_manifest), "query": item["query"].as_dict(),
                "indicator": embedded["indicator"], "source": embedded["source"],
            })
        (temporary / "provenance.json").write_bytes(_json_bytes(provenance))
        if output.exists() or output.is_symlink():
            raise EvidenceError("Output appeared during reconstruction; refusing to replace it.")
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return provenance
