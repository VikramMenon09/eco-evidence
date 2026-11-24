"""Offline outer joins of validated indicator bundles, with explicit coverage.

Input CSVs are deliberately not consumed. Each indicator is reconstructed from
its archived source responses, then embedded as a replayable evidence bundle.
"""

import csv
import io
import json
from pathlib import Path
import shutil
import tempfile

from . import __version__
from .core import (BundleReader, EvidenceError, MAX_MANIFEST_BYTES, Query, collect,
                   decode_json, digest, export_bundle, read_bounded_file, utc_now)
from .registry import get_indicator

STATUSES = ("available", "missing", "not_returned", "not_requested")
VALUE_FIELDS = ("value", "status", "unit", "country_name", "source_obs_status")
QUERY_FIELDS = {"indicator", "countries", "start_year", "end_year"}
MAX_PANEL_INPUTS = 3


class _FrozenReader:
    """Replay already verified snapshots without re-reading mutable input files."""

    def __init__(self, snapshots):
        self.snapshots = {snapshot.url: snapshot for snapshot in snapshots}
        if len(self.snapshots) != len(snapshots):
            raise EvidenceError("Repeated source request in reconstructed panel input.")

    def get(self, url):
        try:
            return self.snapshots[url]
        except KeyError as exc:
            raise EvidenceError("Reconstructed panel input has no matching source request.") from exc


def _read_input(directory):
    """Validate the recorded query and reconstruct source data, not cached CSV."""
    try:
        directory = Path(directory).resolve()
        manifest_bytes = read_bounded_file(directory / "provenance.json", MAX_MANIFEST_BYTES)
        manifest = decode_json(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        recorded = manifest["query"]
        if not isinstance(recorded, dict) or set(recorded) != QUERY_FIELDS:
            raise ValueError("query fields must match the bundle query contract")
        if not isinstance(recorded["countries"], list) or not isinstance(recorded["indicator"], str):
            raise ValueError("query field types")
        query = Query(recorded["indicator"], tuple(recorded["countries"]),
                      recorded["start_year"], recorded["end_year"])
        if query.as_dict() != recorded:
            raise ValueError("query must use canonical indicator code and sorted country codes")
        reader = BundleReader(directory)
        # The query and the raw-response catalog must belong to the same manifest.
        # Concurrent modifications are unsupported; detect a changed catalog before
        # collecting rather than combining two observed manifest versions.
        if read_bounded_file(directory / "provenance.json", MAX_MANIFEST_BYTES) != manifest_bytes:
            raise ValueError("input manifest changed while it was being opened")
        rows, provenance, snapshots = collect(query, reader)
    except EvidenceError as exc:
        raise EvidenceError(f"Panel input {directory}: {exc}") from exc
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        raise EvidenceError(f"Panel input is not a valid replayable indicator bundle: {directory}.") from exc
    return {"directory": directory, "query": query, "rows": rows,
            "provenance": provenance, "snapshots": snapshots,
            "alias": get_indicator(query.indicator).alias,
            "original_manifest_sha256": digest(manifest_bytes)}


def _json_bytes(value):
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise EvidenceError("Generated panel metadata exceeds the 1 MB manifest limit.")
    return encoded


def _output_is_inside_input(output, directories):
    """Check lexical paths and real ancestor identities without creating parents.

    Filesystem aliases can differ in case or Unicode normalization even after
    resolve(). Device/inode identities catch those aliases on their actual
    filesystem; missing ancestors do not hide an existing input farther up.
    """
    if any(output.is_relative_to(directory) for directory in directories):
        return True
    identities = set()
    for directory in directories:
        metadata = directory.stat()
        identities.add((metadata.st_dev, metadata.st_ino))
    for ancestor in output.parents:
        try:
            metadata = ancestor.stat()
        except FileNotFoundError:
            continue
        if (metadata.st_dev, metadata.st_ino) in identities:
            return True
    return False


def export_panel(bundle_paths, output):
    """Write a self-contained, offline panel from 2–3 distinct indicator bundles.

    All requested country-year rows participate in the outer union, regardless
    of whether a source provided a value. A key outside an input's query has
    status ``not_requested``; this does not assert anything about source data.
    """
    if not isinstance(bundle_paths, (list, tuple)) or not 2 <= len(bundle_paths) <= MAX_PANEL_INPUTS:
        raise EvidenceError("A panel requires 2–3 distinct indicator bundles.")
    try:
        output = Path(output)
        resolved_output = output.resolve()
    except (TypeError, ValueError, RuntimeError) as exc:
        raise EvidenceError("Panel output must be a valid new directory path.") from exc
    if output.exists() or output.is_symlink():
        raise EvidenceError("Output already exists; choose a new directory to preserve earlier evidence.")
    inputs = [_read_input(directory) for directory in bundle_paths]
    inputs.sort(key=lambda item: item["alias"])
    aliases = [item["alias"] for item in inputs]
    if len(set(aliases)) != len(aliases):
        raise EvidenceError("Duplicate indicators are not allowed in a panel; use one bundle per indicator.")
    origins = {item["provenance"]["data_origin"] for item in inputs}
    if len(origins) != 1:
        raise EvidenceError("Cannot mix synthetic fixtures and live-source bundles in a panel.")
    origin = origins.pop()
    if _output_is_inside_input(resolved_output, [item["directory"] for item in inputs]):
        raise EvidenceError("Panel output must be outside every input bundle to preserve earlier evidence.")

    indexed = {item["alias"]: {(row["country_code"], row["year"]): row for row in item["rows"]}
               for item in inputs}
    keys = sorted(set().union(*(set(rows) for rows in indexed.values())))
    counts = {alias: dict.fromkeys(STATUSES, 0) for alias in aliases}
    complete_case_count = 0
    panel_rows = []
    for country, year in keys:
        panel_row = {"country_code": country, "year": year, "data_origin": origin}
        complete = True
        for item in inputs:
            alias = item["alias"]
            row = indexed[alias].get((country, year))
            if row is None:
                values = {"value": "", "status": "not_requested",
                          "unit": item["provenance"]["indicator"]["unit"],
                          "country_name": "", "source_obs_status": ""}
            else:
                values = {field: row[field] for field in VALUE_FIELDS}
            for field in VALUE_FIELDS:
                panel_row[f"{alias}_{field}"] = values[field]
            counts[alias][values["status"]] += 1
            complete = complete and values["status"] == "available"
        complete_case_count += int(complete)
        panel_rows.append(panel_row)

    coverage = {
        "schema_version": 1,
        "artifact_type": "indicator_panel_coverage",
        "data_origin": origin,
        "row_count": len(panel_rows),
        "denominator": "All country-year rows in the outer union of input queries.",
        "complete_case_count": complete_case_count,
        "complete_case_definition": "Rows with status available for every selected indicator; source zero counts as available.",
        "status_definitions": {
            "available": "The source returned a number, including zero.",
            "missing": "The source explicitly returned null for a requested country-year.",
            "not_returned": "The country-year was requested but absent from the complete source response.",
            "not_requested": "The country-year falls outside this indicator bundle's recorded query.",
        },
        "indicators": {item["alias"]: {"indicator_code": item["query"].indicator,
                                        "unit": item["provenance"]["indicator"]["unit"],
                                        "counts": counts[item["alias"]]} for item in inputs},
    }
    coverage_bytes = _json_bytes(coverage)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(panel_rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(panel_rows)
    csv_bytes = buffer.getvalue().encode("utf-8")
    provenance = {
        "schema_version": 1, "artifact_type": "indicator_panel",
        "tool": "eco-evidence", "tool_version": __version__, "created_at": utc_now(),
        "data_origin": origin, "row_count": len(panel_rows),
        "complete_case_count": complete_case_count,
        "join": {"kind": "outer", "keys": ["country_code", "year"],
                 "row_order": ["country_code", "year"], "indicator_order": aliases,
                 "outside_query_status": "not_requested"},
        "panel_csv_file": "panel.csv", "panel_csv_sha256": digest(csv_bytes),
        "coverage_file": "coverage.json", "coverage_sha256": digest(coverage_bytes),
        "input_data_policy": "Rows are reconstructed from verified raw responses; input data.csv is not read or authenticated.",
        "lineage_policy": "Original manifest hashes identify the bytes read from each input; they are not source signatures.",
        "inputs": [],
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        (temporary / "panel.csv").write_bytes(csv_bytes)
        (temporary / "coverage.json").write_bytes(coverage_bytes)
        for item in inputs:
            bundle_path = f"inputs/{item['alias']}"
            embedded = export_bundle(item["query"], _FrozenReader(item["snapshots"]), temporary / bundle_path)
            embedded_manifest = read_bounded_file(temporary / bundle_path / "provenance.json", MAX_MANIFEST_BYTES)
            provenance["inputs"].append({
                "alias": item["alias"], "bundle_path": bundle_path,
                "data_origin": embedded["data_origin"],
                "original_manifest_sha256": item["original_manifest_sha256"],
                "embedded_manifest_sha256": digest(embedded_manifest),
                "query": item["query"].as_dict(), "indicator": embedded["indicator"],
                "source": embedded["source"],
            })
        (temporary / "provenance.json").write_bytes(_json_bytes(provenance))
        if output.exists() or output.is_symlink():
            raise EvidenceError("Output appeared during reconstruction; refusing to replace it.")
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return provenance
