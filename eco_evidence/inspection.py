"""Read-only metadata inspection and explicit baseline-to-bundle comparisons."""

from decimal import Decimal
import json
from urllib.parse import urlparse

from . import __version__
from .archive import read_bundle
from .core import EvidenceError, MAX_BYTES, MAX_MANIFEST_BYTES, digest

ROW_FIELDS = ("value", "status", "source_obs_status", "country_name")
METADATA_FIELDS = (
    "indicator.code", "indicator.name", "indicator.unit", "indicator.api_unit",
    "indicator.unit_basis", "indicator.registry_checked_on", "indicator.source_note",
    "indicator.source_organization", "source.name", "source.id", "source.last_updated",
    "source.catalog_url", "source.license", "source.terms_url",
)
MAX_REPORT_BYTES = MAX_BYTES
LICENSE_SCOPE = (
    "Catalog-level context; no indicator-specific reuse authorization is inferred. "
    "Review source/provider terms before redistribution."
)


def _bounded_json(value, limit, label):
    # Match the public JSON CLI exactly, including escapes, indentation and
    # its terminating newline; compact UTF-8 size can undercount that output.
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > limit:
        raise EvidenceError(f"{label} exceeds the {limit:,}-byte limit; no partial report is returned.")
    return encoded


def _role(url):
    path = urlparse(url).path
    if path.startswith("/v2/indicator/"):
        return "indicator_metadata"
    if "/indicator/" in path:
        return "observations"
    return "country_metadata"


def _overview(archive):
    provenance = archive["provenance"]
    indicator = dict(provenance["indicator"])
    # collect() verifies the archived API unit field is exactly blank; the
    # reviewed registry supplies the descriptive unit, not a numeric conversion.
    indicator["api_unit"] = ""
    source = dict(provenance["source"])
    origin = provenance["data_origin"]
    result = {
        "query": archive["query"].as_dict(),
        "data_origin": origin,
        "original_manifest_sha256": archive["original_manifest_sha256"],
        "indicator": indicator,
        "source": source,
        "row_count": provenance["row_count"],
        "counts": dict(provenance["counts"]),
        "licensing": {
            "status": "not_verified_from_archived_api_responses",
            "catalog_statement": source["license"],
            "catalog_url": source["catalog_url"],
            "terms_url": source["terms_url"],
            "scope": LICENSE_SCOPE,
            "synthetic_context": (
                "These strings describe underlying WDI catalog context; the fixture's invented "
                "values and source text are not official World Bank observations or metadata."
                if origin == "synthetic_fixture" else None
            ),
        },
        "raw_sources": [
            {"role": _role(snapshot.url), "url": snapshot.url,
             "retrieved_at": snapshot.retrieved_at, "sha256": digest(snapshot.body)}
            for snapshot in sorted(archive["snapshots"], key=lambda item: item.url)
        ],
    }
    _bounded_json(result, MAX_MANIFEST_BYTES, "Reconstructed inspection metadata")
    return result


def _metadata_value(overview, field):
    section, key = field.split(".")
    return overview[section][key]


def _compare_raw_sources(baseline, bundle):
    old = {item["url"]: item for item in baseline["raw_sources"]}
    new = {item["url"]: item for item in bundle["raw_sources"]}
    changes = []
    counts = {"added": 0, "removed": 0, "body_changed": 0, "retrieval_timestamp_changed": 0}
    for url in sorted(old.keys() | new.keys()):
        before, after = old.get(url), new.get(url)
        fields = []
        if before is None:
            kind = "added"
            counts[kind] += 1
        elif after is None:
            kind = "removed"
            counts[kind] += 1
        else:
            fields = [field for field in ("sha256", "retrieved_at") if before[field] != after[field]]
            if not fields:
                continue
            kind = "changed"
            counts["body_changed"] += int("sha256" in fields)
            counts["retrieval_timestamp_changed"] += int("retrieved_at" in fields)
        changes.append({"url": url, "change": kind, "baseline": before,
                        "bundle": after, "changed_fields": fields})
    return changes, counts


def _comparison(baseline_archive, bundle_archive, baseline, bundle):
    if baseline_archive["query"] != bundle_archive["query"]:
        raise EvidenceError("Baseline and bundle must have exactly the same canonical indicator, countries and year query.")
    if baseline["data_origin"] != bundle["data_origin"]:
        raise EvidenceError("Baseline and bundle must have the same data origin; synthetic and live-source evidence cannot be compared.")
    metadata_changes = []
    for field in METADATA_FIELDS:
        before, after = _metadata_value(baseline, field), _metadata_value(bundle, field)
        if before != after:
            metadata_changes.append({"field": field, "baseline": before, "bundle": after})

    indexed_baseline = {(row["country_code"], row["year"]): row for row in baseline_archive["rows"]}
    indexed_bundle = {(row["country_code"], row["year"]): row for row in bundle_archive["rows"]}
    if indexed_baseline.keys() != indexed_bundle.keys():
        raise EvidenceError("Reconstructed comparison rows do not match the common query.")
    field_counts = dict.fromkeys(ROW_FIELDS, 0)
    numeric_count = 0
    representation_count = 0
    row_changes = []
    for country, year in sorted(indexed_baseline):
        before, after = indexed_baseline[(country, year)], indexed_bundle[(country, year)]
        changes = {field: {"baseline": before[field], "bundle": after[field]}
                   for field in ROW_FIELDS if before[field] != after[field]}
        if not changes:
            continue
        for field in changes:
            field_counts[field] += 1
        numeric_changed = representation_changed = False
        if before["status"] == after["status"] == "available":
            numeric_changed = Decimal(before["value"]) != Decimal(after["value"])
            representation_changed = not numeric_changed and before["value"] != after["value"]
        numeric_count += int(numeric_changed)
        representation_count += int(representation_changed)
        row_changes.append({"country_code": country, "year": year, "fields": changes,
                            "numeric_value_changed": numeric_changed,
                            "value_representation_changed": representation_changed})
    raw_changes, raw_counts = _compare_raw_sources(baseline, bundle)
    return {
        "direction": "baseline_to_bundle",
        "chronology_inferred": False,
        "baseline": baseline,
        "metadata_change_count": len(metadata_changes),
        "metadata_changes": metadata_changes,
        "source_update_changed": baseline["source"]["last_updated"] != bundle["source"]["last_updated"],
        "row_count": len(indexed_bundle),
        "changed_row_count": len(row_changes),
        "row_field_change_counts": field_counts,
        "numeric_value_change_count": numeric_count,
        "value_representation_change_count": representation_count,
        "numeric_change_definition": "Both cells available and exact Decimal values unequal; availability/status changes are counted separately.",
        "representation_change_definition": "Both cells available with equal Decimal values but different reconstructed value strings.",
        "row_changes": row_changes,
        "raw_source_change_count": len(raw_changes),
        "raw_source_change_counts": raw_counts,
        "raw_source_count_definition": "Added/removed URLs are separate; body and retrieval timestamp changes count only URLs present in both archives.",
        "raw_source_changes": raw_changes,
    }


def inspect_bundle(bundle_path, baseline_path=None):
    """Reconstruct an archived query and return metadata without network or writes.

    A baseline selects comparison direction; neither source update labels nor
    retrieval timestamps establish that the selected bundle is newer. Complete
    detail is returned within explicit bounds, otherwise the request is rejected.
    """
    archive = read_bundle(bundle_path)
    bundle = _overview(archive)
    report = {
        "schema_version": 1, "artifact_type": "bundle_inspection",
        "tool": "eco-evidence", "tool_version": __version__,
        "inspection_basis": "Metadata and rows are reconstructed from checksum-verified archived raw responses.",
        "input_data_policy": "Input data.csv and derived manifest metadata are not read as evidence or authenticated by this inspection.",
        "lineage_policy": "Manifest hashes identify the bytes read, not a source signature or independent proof of authenticity.",
        "source_update_semantics": "The source update field is a database label, not an observation date or a retrieval timestamp.",
        "bundle": bundle,
        "comparison": None,
    }
    if baseline_path is not None:
        baseline_archive = read_bundle(baseline_path)
        baseline = _overview(baseline_archive)
        report["comparison"] = _comparison(baseline_archive, archive, baseline, bundle)
    _bounded_json(report, MAX_REPORT_BYTES, "Complete inspection report")
    return report


def _literal(value):
    """Quote untrusted source text so terminal control characters stay escaped."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _overview_lines(title, overview):
    indicator, source, query = overview["indicator"], overview["source"], overview["query"]
    lines = [
        title,
        f"  Origin: {overview['data_origin']}",
        f"  Indicator: {indicator['code']} - {_literal(indicator['name'])}",
        f"  Countries: {', '.join(query['countries'])}; years: {query['start_year']}-{query['end_year']}",
        f"  Rows: {overview['row_count']}; status counts: {_literal(overview['counts'])}",
        f"  Unit: {_literal(indicator['unit'])}; archived API unit: {_literal(indicator['api_unit'])}",
        f"  Unit basis: {_literal(indicator['unit_basis'])}",
        f"  Registry checked on: {indicator['registry_checked_on']}",
        f"  Definition: {_literal(indicator['source_note'])}",
        f"  Original provider/organization: {_literal(indicator['source_organization'])}",
        f"  Dataset: {_literal(source['name'])} (source ID {source['id']})",
        f"  Source update label: {_literal(source['last_updated'])}",
        f"  Input manifest SHA-256: {overview['original_manifest_sha256']}",
        f"  License verification: {overview['licensing']['status']}",
        f"  Catalog context: {_literal(overview['licensing']['catalog_statement'])}",
        f"  Catalog URL: {_literal(overview['licensing']['catalog_url'])}",
        f"  Terms URL: {_literal(overview['licensing']['terms_url'])}",
        f"  Licensing scope: {overview['licensing']['scope']}",
    ]
    if overview["licensing"]["synthetic_context"]:
        lines.append(f"  Synthetic fixture context: {overview['licensing']['synthetic_context']}")
    lines.append("  Original source retrievals:")
    for snapshot in overview["raw_sources"]:
        lines.extend([
            f"    {snapshot['role']}: {_literal(snapshot['url'])}",
            f"      Retrieved: {_literal(snapshot['retrieved_at'])}; SHA-256: {snapshot['sha256']}",
        ])
    return lines


def render_text(report):
    """Render a report returned by inspect_bundle, with complete change details."""
    lines = [f"EcoEvidence {report['tool_version']} - archived evidence inspection", ""]
    lines.extend(_overview_lines("Selected bundle", report["bundle"]))
    lines.extend(["", report["inspection_basis"], report["input_data_policy"],
                  report["lineage_policy"], report["source_update_semantics"]])
    comparison = report["comparison"]
    if comparison is not None:
        lines.extend(["", "Comparison direction: baseline -> selected bundle; chronology is not inferred.", ""])
        lines.extend(_overview_lines("Baseline bundle", comparison["baseline"]))
        lines.extend([
            "", f"Source update label changed: {'yes' if comparison['source_update_changed'] else 'no'}",
            f"Metadata fields changed: {comparison['metadata_change_count']}",
        ])
        for change in comparison["metadata_changes"]:
            lines.append(f"  {change['field']}: {_literal(change['baseline'])} -> {_literal(change['bundle'])}")
        lines.extend([
            f"Changed rows: {comparison['changed_row_count']} of {comparison['row_count']}",
            f"Row field change counts: {_literal(comparison['row_field_change_counts'])}",
            f"Exact numeric value changes: {comparison['numeric_value_change_count']}",
            f"Representation-only value changes: {comparison['value_representation_change_count']}",
            comparison["numeric_change_definition"], comparison["representation_change_definition"],
        ])
        for change in comparison["row_changes"]:
            lines.append(f"  {change['country_code']} {change['year']}:")
            for field, values in change["fields"].items():
                lines.append(f"    {field}: {_literal(values['baseline'])} -> {_literal(values['bundle'])}")
            lines.append(f"    Numeric change: {change['numeric_value_changed']}; representation-only: {change['value_representation_changed']}")
        lines.extend([
            f"Raw source records changed: {comparison['raw_source_change_count']}",
            f"Raw source change counts: {_literal(comparison['raw_source_change_counts'])}",
            comparison["raw_source_count_definition"],
        ])
        for change in comparison["raw_source_changes"]:
            lines.append(f"  {change['change']}: {_literal(change['url'])}")
            if change["change"] == "changed":
                for field in change["changed_fields"]:
                    lines.append(f"    {field}: {_literal(change['baseline'][field])} -> {_literal(change['bundle'][field])}")
            else:
                lines.append(f"    Baseline: {_literal(change['baseline'])}")
                lines.append(f"    Bundle: {_literal(change['bundle'])}")
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > MAX_REPORT_BYTES:
        raise EvidenceError("Rendered inspection report exceeds the 5,000,000-byte limit; no partial report is returned.")
    return text
