"""A bounded, separate NOAA global marine-surface annual CO2 archive.

This series has no country dimension and does not participate in WDI joins.
Source numeric tokens are retained as text; the adapter performs no arithmetic.
"""

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException, HTTPResponse
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from . import __version__
from .core import EvidenceError, decode_json, digest, read_bounded_file, utc_now
from .panel import _output_is_inside_input

SOURCE_URL = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_gl.txt"
SERIES = "co2_global_annual"
ARTIFACT_TYPE = "noaa_global_annual_co2"
RAW_FILE = "raw/co2_annmean_gl.txt"
MAX_RAW_BYTES = 1_000_000
MAX_MANIFEST_BYTES = 1_000_000
MAX_CSV_BYTES = 5_000_000
MAX_NUMBER_CHARS = 64
UNIT = "ppm"
SPATIAL_SCOPE = "global_marine_surface"
UNIT_HEADER = "CO2 expressed as a mole fraction in dry air, micromol/mol, abbreviated as ppm"
EXPECTED_UNCERTAINTY = (
    "The uncertainty in the global annual mean is estimated using a monte carlo "
    "technique that computes 200 global annual averages, each time using a "
    "slightly different set of measurement records from the NOAA GML cooperative "
    "air sampling network. The reported uncertainty is the mean of the standard "
    "deviations for each annual average using this technique. Please see "
    "Conway et al., 1994, JGR, vol. 99, no. D11. for a complete discussion."
)


@dataclass(frozen=True)
class ClimateQuery:
    start_year: int
    end_year: int

    def __post_init__(self):
        current_year = datetime.now(timezone.utc).year
        if (type(self.start_year) is not int or type(self.end_year) is not int
                or not 1979 <= self.start_year <= self.end_year <= current_year):
            raise EvidenceError(f"Climate years must be integers in ascending order between 1979 and {current_year}.")

    def as_dict(self):
        return {"series": SERIES, "start_year": self.start_year, "end_year": self.end_year}


@dataclass(frozen=True)
class ClimateSnapshot:
    url: str
    body: bytes
    retrieved_at: str
    origin: str = "noaa_gml"

    def __post_init__(self):
        if self.url != SOURCE_URL:
            raise EvidenceError("Only the fixed official NOAA annual global CO2 URL is supported.")
        if not isinstance(self.body, bytes) or len(self.body) > MAX_RAW_BYTES:
            raise EvidenceError("NOAA source must be bytes within the 1 MB raw response limit.")
        if not isinstance(self.origin, str) or self.origin not in ("noaa_gml", "synthetic_fixture"):
            raise EvidenceError("Unknown climate data origin.")
        try:
            if not isinstance(self.retrieved_at, str):
                raise ValueError("timestamp required")
            stamp = datetime.fromisoformat(self.retrieved_at.replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                raise ValueError("timezone required")
        except (ValueError, TypeError) as exc:
            raise EvidenceError("Climate retrieval timestamp must include a timezone.") from exc


class NoRedirectHandler(HTTPRedirectHandler):
    """Never dispatch a redirected request, even to another NOAA URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise EvidenceError("NOAA redirects are not supported; review the fixed source URL.")


def open_noaa(request, *, timeout):
    context = ssl.create_default_context()
    return build_opener(NoRedirectHandler(), HTTPSHandler(context=context)).open(request, timeout=timeout)


def _validate_timeout(timeout):
    if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
        raise EvidenceError("Timeout must be between 1 and 30 seconds.")


def _read_framed_body(response):
    """Bound bytes and establish completion instead of trusting a bare EOF.

    HTTPResponse.read(amt) can return a short Content-Length body without raising
    IncompleteRead. Chunked reads use the native decoder, which must consume the
    terminating chunk when the decoded body stays within our limit.
    """
    lengths = response.headers.get_all("Content-Length", [])
    encodings = response.headers.get_all("Transfer-Encoding", [])
    if lengths and encodings:
        raise EvidenceError("NOAA response has ambiguous Content-Length and Transfer-Encoding framing.")
    expected_length = None
    if lengths:
        if len(lengths) != 1:
            raise EvidenceError("NOAA response must have exactly one Content-Length header.")
        value = lengths[0].strip(" \t")
        if not re.fullmatch(r"[0-9]+", value):
            raise EvidenceError("NOAA Content-Length must be an unsigned ASCII decimal integer.")
        # Remove leading zeros before conversion so even a long header cannot
        # trigger Python's integer-string conversion limit.
        significant = value.lstrip("0") or "0"
        if len(significant) > len(str(MAX_RAW_BYTES)) or int(significant) > MAX_RAW_BYTES:
            raise EvidenceError("NOAA Content-Length exceeds the 1 MB raw response limit.")
        expected_length = int(significant)
        if getattr(response, "chunked", False):
            raise EvidenceError("NOAA response decoder disagrees with Content-Length framing.")
    elif (len(encodings) != 1 or encodings[0].strip(" \t").lower() != "chunked"
          or not isinstance(response, HTTPResponse) or response.chunked is not True):
        raise EvidenceError("NOAA response requires a Content-Length or active native chunked framing; EOF-delimited bodies are refused.")
    body = response.read(MAX_RAW_BYTES + 1)
    if expected_length is not None and len(body) != expected_length:
        raise EvidenceError("NOAA response body does not match Content-Length; truncated or mislabeled body refused.")
    return body


def _fetch_snapshot(timeout):
    _validate_timeout(timeout)
    request = Request(SOURCE_URL, headers={"Accept": "text/plain", "User-Agent": f"EcoEvidence/{__version__}"})
    try:
        with open_noaa(request, timeout=timeout) as response:
            if response.getcode() != 200:
                raise EvidenceError("NOAA response must have HTTP status 200.")
            if response.geturl() != SOURCE_URL:
                raise EvidenceError("NOAA response URL differs from the fixed official source.")
            body = _read_framed_body(response)
    except HTTPError as exc:
        exc.close()
        raise EvidenceError(f"NOAA HTTP {exc.code}; no climate bundle was produced.") from exc
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise EvidenceError(f"NOAA request failed: {exc}. Check connectivity and trusted TLS certificates.") from exc
    return ClimateSnapshot(SOURCE_URL, body, utc_now())


def _paragraph(comments, prefix):
    starts = [index for index, text in enumerate(comments) if text.startswith(prefix)]
    if len(starts) != 1:
        raise EvidenceError("NOAA source header is missing or repeats a required explanatory paragraph.")
    paragraph = []
    for text in comments[starts[0]:]:
        if not text.strip():
            break
        paragraph.append(text)
    return "\n".join(paragraph)


def _parse_source(snapshot):
    try:
        text = snapshot.body.decode("utf-8")
    except UnicodeError as exc:
        raise EvidenceError("NOAA source must be valid UTF-8 text.") from exc
    header_lines = []
    comments = []
    observations = {}
    found_columns = False
    previous_year = None
    current_year = datetime.now(timezone.utc).year
    for physical_line in text.splitlines(keepends=True):
        terminated = physical_line.endswith("\n")
        original = physical_line.removesuffix("\n").removesuffix("\r")
        line = original.strip()
        if not line:
            continue
        if line.startswith("#"):
            if found_columns:
                raise EvidenceError("NOAA source has unexpected comments after its column header.")
            header_lines.append(original)
            comment = line[1:].lstrip(" \t")
            if re.fullmatch(r"year\s+mean\s+unc", comment):
                found_columns = True
            else:
                comments.append(comment)
            continue
        if not found_columns:
            raise EvidenceError("NOAA source data appeared before the year/mean/unc header.")
        if not terminated:
            raise EvidenceError("NOAA data rows must end with LF or CRLF; a partial final numeric token cannot be accepted.")
        fields = line.split()
        if len(fields) != 3 or not re.fullmatch(r"[0-9]{4}", fields[0]):
            raise EvidenceError("NOAA annual data must contain exactly year, mean and uncertainty columns.")
        year = int(fields[0])
        if not 1979 <= year <= current_year or previous_year is not None and year <= previous_year:
            raise EvidenceError("NOAA source years must be unique, increasing and within the supported range.")
        for token in fields[1:]:
            if len(token) > MAX_NUMBER_CHARS or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token):
                raise EvidenceError("NOAA mean and uncertainty must be bounded plain nonnegative decimal tokens.")
        if not any(character in "123456789" for character in fields[1]):
            raise EvidenceError("NOAA annual mean CO2 must be positive; missing sentinels are not inferred.")
        observations[year] = (fields[1], fields[2])
        previous_year = year
    if not found_columns or not observations:
        raise EvidenceError("NOAA source is missing its annual column header or observations.")
    if comments.count(UNIT_HEADER) != 1:
        raise EvidenceError("NOAA source units changed or are missing; review this adapter.")
    uncertainty = _paragraph(comments, "The uncertainty in the global annual mean")
    if " ".join(uncertainty.split()) != EXPECTED_UNCERTAINTY:
        raise EvidenceError("NOAA annual uncertainty method changed; review this adapter before export.")
    revision = _paragraph(comments, "NOTE:")
    if len(revision) <= len("NOTE:"):
        raise EvidenceError("NOAA source revision caveat is empty.")
    creation = [comment[len("File Creation:"):].strip() for comment in comments if comment.startswith("File Creation:")]
    if len(creation) != 1 or not creation[0] or len(creation[0]) > 256:
        raise EvidenceError("NOAA source must contain exactly one bounded File Creation label.")
    years = list(observations)
    source = {
        "name": "NOAA Global Monitoring Laboratory global marine-surface annual mean CO2",
        "url": SOURCE_URL,
        "citation_doi": "https://doi.org/10.15138/9N0H-ZH07",
        "unit": UNIT, "unit_definition": UNIT_HEADER, "spatial_scope": SPATIAL_SCOPE,
        "source_file_creation": creation[0],
        "source_file_creation_time_basis": "Source label has no timezone; it is not the retrieval timestamp.",
        "parsed_coverage": {"start_year": years[0], "end_year": years[-1], "row_count": len(years)},
        "header_comments": header_lines,
        "uncertainty_definition": uncertainty,
        "revision_caveat": revision,
        "data_use_context": "Consult the preserved NOAA source comments and GML data-use policy; no Creative Commons license is inferred.",
        "data_use_url": "https://gml.noaa.gov/about/disclaimer.html",
        "origin_context": "Synthetic fixtures are invented test data; NOAA metadata context does not make them official observations." if snapshot.origin == "synthetic_fixture" else "noaa_gml labels a NOAA source archive; online fetch verifies TLS, while offline replay preserves the supplied origin label without authenticating historical retrieval.",
    }
    return observations, source


def _read_offline(directory, query):
    try:
        directory = Path(directory).resolve()
        manifest_bytes = read_bounded_file(directory / "provenance.json", MAX_MANIFEST_BYTES)
        manifest = decode_json(manifest_bytes)
        if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
                or manifest["schema_version"] != 1 or manifest.get("artifact_type") != ARTIFACT_TYPE):
            raise ValueError("unsupported climate manifest")
        saved_query = manifest["query"]
        if not isinstance(saved_query, dict) or set(saved_query) != {"series", "start_year", "end_year"} or saved_query["series"] != SERIES:
            raise ValueError("invalid canonical climate query")
        archived_query = ClimateQuery(saved_query["start_year"], saved_query["end_year"])
        if archived_query != query:
            raise ValueError("query mismatch")
        sources = manifest["raw_sources"]
        if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
            raise ValueError("one raw source required")
        source = sources[0]
        if set(source) != {"url", "file", "sha256", "retrieved_at"}:
            raise ValueError("invalid source record")
        relative = source["file"]
        if (not isinstance(relative, str) or not relative or "\\" in relative
                or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts):
            raise ValueError("unsafe raw source path")
        filename = (directory / relative).resolve()
        filename.relative_to(directory)
        body = read_bounded_file(filename, MAX_RAW_BYTES)
        checksum = source["sha256"]
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum) or digest(body) != checksum:
            raise ValueError("source checksum mismatch")
        snapshot = ClimateSnapshot(source["url"], body, source["retrieved_at"], manifest["data_origin"])
        return directory, snapshot, digest(manifest_bytes)
    except (KeyError, ValueError, OSError, TypeError, RecursionError, RuntimeError) as exc:
        raise EvidenceError("Offline climate bundle failed verification; query, origin, source URL and raw hashes must match.") from exc


def _manifest_bytes(value):
    result = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(result) > MAX_MANIFEST_BYTES:
        raise EvidenceError("Generated climate provenance exceeds the 1 MB limit.")
    return result


def export_climate(query, output, *, offline=None, timeout=15):
    """Export a fixed-source archive, or replay an exact query without a network call."""
    if not isinstance(query, ClimateQuery):
        raise EvidenceError("Climate export requires a validated ClimateQuery.")
    _validate_timeout(timeout)
    try:
        output = Path(output)
        resolved_output = output.resolve()
    except (TypeError, ValueError, RuntimeError) as exc:
        raise EvidenceError("Climate output must be a valid new directory path.") from exc
    if output.exists() or output.is_symlink():
        raise EvidenceError("Output already exists; choose a new directory to preserve earlier evidence.")
    original_manifest_sha256 = None
    if offline is not None:
        directory, snapshot, original_manifest_sha256 = _read_offline(offline, query)
        if _output_is_inside_input(resolved_output, [directory]):
            raise EvidenceError("Climate output must be outside the input archive to preserve earlier evidence.")
    else:
        snapshot = _fetch_snapshot(timeout)
    observations, source = _parse_source(snapshot)
    rows = []
    for year in range(query.start_year, query.end_year + 1):
        mean, uncertainty = observations.get(year, ("", ""))
        rows.append({"year": year, "co2_mean": mean, "co2_uncertainty": uncertainty,
                     "unit": UNIT, "status": "available" if year in observations else "not_returned",
                     "spatial_scope": SPATIAL_SCOPE, "data_origin": snapshot.origin})
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = buffer.getvalue().encode("utf-8")
    if len(csv_bytes) > MAX_CSV_BYTES:
        raise EvidenceError("Generated climate CSV exceeds the 5 MB limit; no rows are dropped.")
    provenance = {
        "schema_version": 1, "artifact_type": ARTIFACT_TYPE, "tool": "eco-evidence",
        "tool_version": __version__, "created_at": utc_now(), "data_origin": snapshot.origin,
        "query": query.as_dict(), "row_count": len(rows),
        "counts": {status: sum(row["status"] == status for row in rows) for status in ("available", "not_returned")},
        "source": source,
        "raw_sources": [{"url": SOURCE_URL, "file": RAW_FILE, "sha256": digest(snapshot.body), "retrieved_at": snapshot.retrieved_at}],
        "data_csv_file": "data.csv", "data_csv_sha256": digest(csv_bytes),
        "missingness_policy": "Absent requested years are not_returned with empty mean and uncertainty. Numeric sentinels are not interpreted as missing; invalid or negative tokens reject the source.",
        "replay_policy": "Reconstruct values and metadata from checksum-verified raw text. Derived CSV and manifest summaries are not evidence. Replay requires the exact original query and supported source schema.",
        "lineage_policy": "Manifest and raw hashes identify bytes; they are not NOAA signatures or independent authentication.",
        "network_policy": "One fixed-source HTTPS GET with verified TLS, no redirects, retries or cache. Require one unambiguous Content-Length with matching body size, or active native chunked framing; reject EOF-delimited bodies. Offline replay makes no requests.",
        "scope_policy": "Global marine-surface atmospheric concentration, not national emissions or a country-level WDI series. No interpolation, arithmetic or causal interpretation.",
    }
    if original_manifest_sha256 is not None:
        provenance["original_manifest_sha256"] = original_manifest_sha256
    manifest_bytes = _manifest_bytes(provenance)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        (temporary / "raw").mkdir()
        (temporary / RAW_FILE).write_bytes(snapshot.body)
        (temporary / "data.csv").write_bytes(csv_bytes)
        (temporary / "provenance.json").write_bytes(manifest_bytes)
        if output.exists() or output.is_symlink():
            raise EvidenceError("Output appeared during export; refusing to replace it.")
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return provenance
