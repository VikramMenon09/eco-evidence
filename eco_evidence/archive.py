"""Read-only reconstruction of one canonical, checksum-verified source archive.

Shared by panels and metadata inspection so both enforce the same query and
raw-source contract. The derived data.csv and manifest summaries are not read
as evidence for values or metadata.
"""

from pathlib import Path

from .core import (BundleReader, EvidenceError, MAX_MANIFEST_BYTES, Query, collect,
                   decode_json, digest, read_bounded_file)
from .registry import get_indicator

QUERY_FIELDS = {"indicator", "countries", "start_year", "end_year"}


def read_bundle(directory):
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
        raise EvidenceError(f"Evidence bundle {directory}: {exc}") from exc
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        raise EvidenceError(f"Input is not a valid replayable indicator bundle: {directory}.") from exc
    return {"directory": directory, "query": query, "rows": rows,
            "provenance": provenance, "snapshots": snapshots,
            "alias": get_indicator(query.indicator).alias,
            "original_manifest_sha256": digest(manifest_bytes)}
