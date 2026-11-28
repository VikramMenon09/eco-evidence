"""Read-only coverage and explicit screening flags for one verified WDI archive.

Source validation stays in archive/core. This view keeps all reconstructed rows
and their value strings; review flags never alter availability or source data.
"""

from decimal import Decimal

from . import __version__
from .archive import read_bundle
from .core import EvidenceError, MAX_BYTES
from .inspection import _bounded_json, _literal, _overview, _overview_lines

MAX_REPORT_BYTES = MAX_BYTES
STATUSES = ("available", "missing", "not_returned")
GAP_POSITIONS = ("leading", "internal", "trailing", "all_unavailable")
RANGE_BOUNDS = {"gdp": ("0", None), "population": ("0", None),
                "renewables": ("0", "100")}
RANGE_FLAGS = ("value_below_minimum", "value_above_maximum")
FLAG_DEFINITIONS = {
    "value_below_minimum": "Available value is strictly below the project's inclusive minimum.",
    "value_above_maximum": "Available value is strictly above the project's inclusive maximum.",
    "source_observation_flag": "Source obs_status is nonempty, including whitespace; its meaning is not interpreted.",
}
GAP_DEFINITION = (
    "A gap is a maximal consecutive run of missing/not_returned years within one "
    "country's requested window. Leading/internal/trailing positions refer only "
    "to that window's first and last available values; all_unavailable means "
    "there is no available value in the window."
)
LIMITATIONS = (
    "Screening flags are review prompts, not proof of an error, source authentication, certification, or automatic correction.",
    "Missing years can reflect source coverage or reporting delays. A gap is not automatically a data defect.",
    "Available means the source supplied a number, including zero and out-of-range values; it does not mean an independently verified measurement.",
    "All rows and reconstructed value strings are retained. No values are imputed, filtered, rounded, converted, or replaced.",
    "The original raw responses preserve numeric spelling. The derived input data.csv and manifest summaries are not used as evidence or authenticated by this audit.",
    "Raw hashes and the input manifest hash identify archived bytes, not a source signature. Source update labels are database dates, not observation dates.",
    "Range bounds are explicit project screening policy. Definitions, provider conventions, historical comparability, and indicator-specific reuse terms still need source review.",
    "A report with no flags is not a general quality score. This audit does not detect every anomaly, assess causality, compare revisions, or support company-specific conclusions.",
)


def _counts(rows):
    return {status: sum(row["status"] == status for row in rows) for status in STATUSES}


def _row_summary(rows):
    return {
        "row_count": len(rows),
        "counts": _counts(rows),
        "zero_value_count": sum(
            row["status"] == "available" and Decimal(row["value"]).is_zero()
            for row in rows
        ),
        "flagged_row_count": sum(bool(row["flags"]) for row in rows),
        "range_flag_count": sum(any(flag in RANGE_FLAGS for flag in row["flags"])
                                for row in rows),
        "source_flag_count": sum("source_observation_flag" in row["flags"]
                                 for row in rows),
    }


def _gaps(rows, first_available, last_available):
    runs = []
    pending = []

    def finish():
        if not pending:
            return
        start, end = pending[0]["year"], pending[-1]["year"]
        if first_available is None:
            position = "all_unavailable"
        elif end < first_available:
            position = "leading"
        elif start > last_available:
            position = "trailing"
        else:
            position = "internal"
        runs.append({
            "start_year": start, "end_year": end, "length": len(pending),
            "position": position,
            "counts": {status: sum(row["status"] == status for row in pending)
                       for status in ("missing", "not_returned")},
        })
        pending.clear()

    for row in rows:
        if row["status"] == "available":
            finish()
        else:
            pending.append(row)
    finish()
    return runs


def quality_bundle(bundle_path):
    """Audit one canonical archive without network access or filesystem writes."""
    archive = read_bundle(bundle_path)
    overview = _overview(archive)
    try:
        minimum, maximum = RANGE_BOUNDS[archive["alias"]]
    except KeyError as exc:
        raise EvidenceError("No reviewed quality screening rule exists for this indicator.") from exc
    lower = Decimal(minimum)
    upper = None if maximum is None else Decimal(maximum)
    rows = []
    for original in archive["rows"]:
        flags = []
        if original["status"] == "available":
            value = Decimal(original["value"])
            if value < lower:
                flags.append("value_below_minimum")
            if upper is not None and value > upper:
                flags.append("value_above_maximum")
        if original["source_obs_status"] != "":
            flags.append("source_observation_flag")
        rows.append({**original, "flags": flags})

    countries = []
    for code in archive["query"].countries:
        selected = [row for row in rows if row["country_code"] == code]
        available = [row["year"] for row in selected if row["status"] == "available"]
        first = available[0] if available else None
        last = available[-1] if available else None
        countries.append({
            "country_code": code, "country_name": selected[0]["country_name"],
            **_row_summary(selected),
            "first_available_year": first, "last_available_year": last,
            "gaps": _gaps(selected, first, last),
        })
    gaps = [gap for country in countries for gap in country["gaps"]]
    summary = {
        **_row_summary(rows), "country_count": len(countries),
        "gap_run_count": len(gaps), "gap_year_count": sum(gap["length"] for gap in gaps),
        "countries_with_gaps": sum(bool(country["gaps"]) for country in countries),
        "all_unavailable_country_count": sum(country["first_available_year"] is None
                                             for country in countries),
        "gap_position_counts": {position: sum(gap["position"] == position for gap in gaps)
                                for position in GAP_POSITIONS},
    }
    report = {
        "schema_version": 1, "artifact_type": "bundle_quality",
        "tool": "eco-evidence", "tool_version": __version__, "bundle": overview,
        "rule": {
            "kind": "project_screening_bounds", "minimum": minimum, "maximum": maximum,
            "unit": overview["indicator"]["unit"], "applies_to": "available",
            "interpretation": "Inclusive bounds are project review policy, not a source validity guarantee. A null maximum means no upper-bound check.",
        },
        "flag_definitions": dict(FLAG_DEFINITIONS), "gap_definition": GAP_DEFINITION,
        "summary": summary, "countries": countries, "rows": rows,
        "limitations": list(LIMITATIONS),
    }
    _bounded_json(report, MAX_REPORT_BYTES, "Complete quality report")
    return report


def render_quality_text(report):
    """Render quality_bundle() output completely as bounded ASCII plain text."""
    summary, rule = report["summary"], report["rule"]
    lines = [f"EcoEvidence {report['tool_version']} - archived evidence quality audit", ""]
    lines.extend(_overview_lines("Selected bundle", report["bundle"]))
    lines.extend([
        "", "Screening rule",
        f"  Kind: {rule['kind']}; applies to: {rule['applies_to']}",
        f"  Minimum: {_literal(rule['minimum'])}; maximum: {_literal(rule['maximum'])}; unit: {_literal(rule['unit'])}",
        f"  Interpretation: {_literal(rule['interpretation'])}",
        "  Flag definitions:",
    ])
    for code, meaning in report["flag_definitions"].items():
        lines.append(f"    {code}: {_literal(meaning)}")
    lines.extend([
        "", "Summary",
        f"  Requested rows: {summary['row_count']}; countries: {summary['country_count']}",
        f"  Status counts: {_literal(summary['counts'])}",
        f"  Zero values: {summary['zero_value_count']}",
        f"  Flagged rows (union): {summary['flagged_row_count']}; range flags: {summary['range_flag_count']}; source flags: {summary['source_flag_count']}",
        f"  Gap runs: {summary['gap_run_count']}; gap years: {summary['gap_year_count']}",
        f"  Countries with gaps: {summary['countries_with_gaps']}; all-unavailable countries: {summary['all_unavailable_country_count']}",
        f"  Gap positions (run counts): {_literal(summary['gap_position_counts'])}",
        "", f"Gap definition: {_literal(report['gap_definition'])}", "", "Country coverage",
    ])
    for country in report["countries"]:
        lines.extend([
            f"  {country['country_code']}: {_literal(country['country_name'])}",
            f"    Rows: {country['row_count']}; status counts: {_literal(country['counts'])}",
            f"    First available year: {_literal(country['first_available_year'])}; last available year: {_literal(country['last_available_year'])}",
            f"    Zero values: {country['zero_value_count']}; flagged rows (union): {country['flagged_row_count']}; range flags: {country['range_flag_count']}; source flags: {country['source_flag_count']}",
        ])
        if not country["gaps"]:
            lines.append("    Gaps: none within the requested window")
        for gap in country["gaps"]:
            lines.append(
                f"    Gap {gap['start_year']}-{gap['end_year']}: {gap['position']}; "
                f"length={gap['length']}; counts={_literal(gap['counts'])}"
            )
    lines.extend(["", "Full row ledger (source strings are JSON literals)"])
    for row in report["rows"]:
        lines.append(
            f"  {row['country_code']} {row['year']}: status={row['status']}; "
            f"value={_literal(row['value'])}; unit={_literal(row['unit'])}; "
            f"source_obs_status={_literal(row['source_obs_status'])}; flags={_literal(row['flags'])}"
        )
    lines.extend(["", "Interpretation and limits"])
    lines.extend(f"  {_literal(note)}" for note in report["limitations"])
    text = "\n".join(lines) + "\n"
    if len(text.encode("ascii")) > MAX_REPORT_BYTES:
        raise EvidenceError(f"Rendered quality report exceeds the {MAX_REPORT_BYTES:,}-byte limit; no partial report is returned.")
    return text
