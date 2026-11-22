"""Bounded World Bank retrieval, schema validation, and auditable bundle export."""

import base64
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from http.client import HTTPException
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from . import __version__
from .registry import API_BASE, CATALOG_URL, CHECKED_ON, TERMS_URL, get_indicator

MAX_BYTES = 5_000_000
MAX_PAGES = 10
MAX_REQUESTS = 30
PER_PAGE = 1000


class EvidenceError(ValueError):
    """A request or source could not be validated; no final bundle is written."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def decode_json(raw):
    def invalid_constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError("Source is not valid finite JSON.") from exc


def required_text(value, field, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise EvidenceError(f"Invalid or missing source field: {field}.")
    return value


def integer(value, field, minimum=0):
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or value < minimum:
        raise EvidenceError(f"Invalid source integer: {field}.")
    return value


@dataclass(frozen=True)
class Query:
    indicator: str
    countries: tuple[str, ...]
    start_year: int
    end_year: int

    def __post_init__(self):
        try:
            selected = get_indicator(self.indicator)
        except ValueError as exc:
            raise EvidenceError(str(exc)) from exc
        if not isinstance(self.countries, tuple) or not 1 <= len(self.countries) <= 20:
            raise EvidenceError("Choose 1–20 distinct three-letter country/economy codes.")
        if any(not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code) for code in self.countries):
            raise EvidenceError("Country codes must contain exactly three uppercase ASCII letters.")
        if len(set(self.countries)) != len(self.countries):
            raise EvidenceError("Duplicate country codes are not allowed.")
        current_year = datetime.now(timezone.utc).year
        if (type(self.start_year) is not int or type(self.end_year) is not int
                or not 1960 <= self.start_year <= self.end_year <= current_year):
            raise EvidenceError(f"Years must be integers in ascending order between 1960 and {current_year}.")
        object.__setattr__(self, "indicator", selected.code)
        object.__setattr__(self, "countries", tuple(sorted(self.countries)))

    def as_dict(self):
        return {"indicator": self.indicator, "countries": list(self.countries),
                "start_year": self.start_year, "end_year": self.end_year}


@dataclass(frozen=True)
class Snapshot:
    url: str
    body: bytes
    retrieved_at: str
    origin: str = "worldbank_api"

    def __post_init__(self):
        if len(self.body) > MAX_BYTES:
            raise EvidenceError("Source exceeds the 5 MB response limit.")
        if self.origin not in ("worldbank_api", "synthetic_fixture"):
            raise EvidenceError("Unknown data origin.")
        try:
            stamp = datetime.fromisoformat(self.retrieved_at.replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                raise ValueError("timezone required")
        except (ValueError, AttributeError) as exc:
            raise EvidenceError("Source retrieval timestamp must include a timezone.") from exc


def checked_payload(snapshot):
    payload = decode_json(snapshot.body)
    if (not isinstance(payload, list) or len(payload) != 2
            or not isinstance(payload[0], dict)
            or not (isinstance(payload[1], list) or payload[1] is None)):
        raise EvidenceError("World Bank returned an error or an unexpected response schema.")
    return payload


class NetworkReader:
    """Cache observations; fetch current metadata online on every run."""

    def __init__(self, cache_dir, *, refresh=False, timeout=15):
        if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
            raise EvidenceError("Timeout must be between 1 and 30 seconds.")
        self.cache_dir = Path(cache_dir)
        self.refresh = refresh
        self.timeout = timeout
        self.requests = 0

    def get(self, url):
        target = urlparse(url)
        if target.scheme != "https" or target.netloc != "api.worldbank.org":
            raise EvidenceError("Only the official HTTPS World Bank API is supported.")
        cache_file = self.cache_dir / f"{digest(url.encode())}.json"
        is_metadata = "/indicator/" not in target.path or target.path.startswith("/v2/indicator/")
        if cache_file.exists() and not self.refresh and not is_metadata:
            try:
                entry = json.loads(cache_file.read_text(encoding="utf-8"))
                body = base64.b64decode(entry["body_base64"], validate=True)
                if entry["url"] != url or digest(body) != entry["sha256"]:
                    raise ValueError("checksum mismatch")
                result = Snapshot(url, body, entry["retrieved_at"])
                checked_payload(result)
                return result
            except (KeyError, ValueError, OSError, TypeError, RecursionError) as exc:
                raise EvidenceError("Cache entry failed verification; rerun with --refresh.") from exc
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise EvidenceError("Request budget exceeded (30 requests).")
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": f"EcoEvidence/{__version__}"})
            with urlopen(request, timeout=self.timeout) as response:
                final = urlparse(response.geturl())
                if final.scheme != "https" or final.netloc != "api.worldbank.org":
                    raise EvidenceError("API redirected outside the official HTTPS origin.")
                body = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            raise EvidenceError(f"World Bank HTTP {exc.code}; no bundle was produced. Retry later if rate-limited.") from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            raise EvidenceError(f"World Bank request failed: {exc}. Check connectivity and trusted TLS certificates.") from exc
        result = Snapshot(url, body, utc_now())
        checked_payload(result)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        entry = {"url": url, "retrieved_at": result.retrieved_at, "sha256": digest(body),
                 "body_base64": base64.b64encode(body).decode("ascii")}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.cache_dir, delete=False) as handle:
            json.dump(entry, handle, sort_keys=True)
            temporary = Path(handle.name)
        temporary.replace(cache_file)
        return result


class BundleReader:
    """Network-free replay of byte-verified raw responses from a saved bundle."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        try:
            manifest = json.loads((self.directory / "provenance.json").read_text(encoding="utf-8"))
            self.origin = manifest["data_origin"]
            sources = manifest["raw_sources"]
            if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_REQUESTS:
                raise ValueError("source count")
            self.sources = {item["url"]: item for item in sources}
            if len(self.sources) != len(sources):
                raise ValueError("duplicate sources")
        except (KeyError, ValueError, OSError, TypeError, RecursionError) as exc:
            raise EvidenceError("Offline bundle has no valid provenance/source manifest.") from exc

    def get(self, url):
        try:
            source = self.sources[url]
            filename = (self.directory / source["file"]).resolve()
            filename.relative_to(self.directory)
            body = filename.read_bytes()
            if digest(body) != source["sha256"]:
                raise ValueError("checksum mismatch")
            return Snapshot(url, body, source["retrieved_at"], self.origin)
        except (KeyError, ValueError, OSError, TypeError) as exc:
            raise EvidenceError("Offline source missing or invalid; query must match archived requests and hashes.") from exc


def request_url(path, params, page):
    return f"{API_BASE}/{path}?{urlencode({**params, 'format': 'json', 'per_page': PER_PAGE, 'page': page})}"


def fetch_pages(reader, path, params, snapshots):
    records = []
    first = None
    for page in range(1, MAX_PAGES + 1):
        snapshot = reader.get(request_url(path, params, page))
        meta, items = checked_payload(snapshot)
        actual = integer(meta.get("page"), "page", 1)
        pages = integer(meta.get("pages"), "pages")
        total = integer(meta.get("total"), "total")
        per_page = integer(meta.get("per_page"), "per_page", 1)
        if actual != page or pages > MAX_PAGES or (total and pages < 1):
            raise EvidenceError("Invalid or excessive source pagination.")
        signature = (pages, total, per_page, meta.get("sourceid"), meta.get("lastupdated"))
        if first is None:
            first = signature
        elif signature != first:
            raise EvidenceError("Source pagination or revision changed during retrieval; retry with --refresh.")
        if items is None:
            if total != 0:
                raise EvidenceError("Source returned null records for a nonempty result.")
            items = []
        if len(items) > per_page or not all(isinstance(item, dict) for item in items):
            raise EvidenceError("Invalid records within source page.")
        snapshots.append(snapshot)
        records.extend(items)
        if page >= max(1, pages):
            if len(records) != total:
                raise EvidenceError("Returned row count does not match pagination total.")
            return records, meta
    raise EvidenceError("Source exceeds the page limit.")


def collect(query, reader):
    indicator = get_indicator(query.indicator)
    snapshots = []
    country_path = "country/" + ";".join(query.countries)
    countries, _ = fetch_pages(reader, country_path, {}, snapshots)
    names = {}
    for country in countries:
        code = country.get("id")
        if code not in query.countries or code in names:
            raise EvidenceError("Country response contains an unexpected or duplicate code.")
        region = country.get("region")
        if not isinstance(region, dict) or not region.get("id") or region["id"] == "NA":
            raise EvidenceError("Regional/income aggregates are not country/economy inputs.")
        names[code] = required_text(country.get("name"), "country name")
    if set(names) != set(query.countries):
        raise EvidenceError("One or more country codes were not recognized by the World Bank.")
    metadata, _ = fetch_pages(reader, f"indicator/{indicator.code}", {"source": 2}, snapshots)
    if len(metadata) != 1:
        raise EvidenceError("Expected exactly one indicator metadata record.")
    definition = metadata[0]
    if (definition.get("id") != indicator.code or definition.get("name") != indicator.name
            or not isinstance(definition.get("source"), dict) or definition["source"].get("id") != "2"):
        raise EvidenceError("Indicator metadata changed or is not WDI; review the registry before export.")
    source_note = required_text(definition.get("sourceNote"), "indicator definition")
    source_organization = required_text(definition.get("sourceOrganization"), "source organization")
    observations, header = fetch_pages(reader, f"{country_path}/indicator/{indicator.code}",
                                        {"source": 2, "date": f"{query.start_year}:{query.end_year}"}, snapshots)
    if header.get("sourceid") != "2":
        raise EvidenceError("Observation response does not identify WDI source 2.")
    values = {}
    for record in observations:
        code = record.get("countryiso3code")
        year_text = record.get("date")
        if (code not in names or not isinstance(year_text, str) or not re.fullmatch(r"[0-9]{4}", year_text)
                or not query.start_year <= int(year_text) <= query.end_year
                or not isinstance(record.get("indicator"), dict) or record["indicator"].get("id") != indicator.code
                or record["indicator"].get("value") != indicator.name):
            raise EvidenceError("Observation has an unexpected country, year, indicator code or indicator name; review metadata drift.")
        key = (code, int(year_text))
        if key in values:
            raise EvidenceError("Duplicate country-year observations are not allowed.")
        if "value" not in record:
            raise EvidenceError("Observation value field is missing.")
        value = record["value"]
        if value is not None and not (type(value) is int or isinstance(value, Decimal) and value.is_finite()):
            raise EvidenceError("Observation value must be a finite JSON number or null.")
        flag = required_text(record.get("obs_status", ""), "observation status", allow_empty=True)
        values[key] = (value, flag)
    origins = {item.origin for item in snapshots}
    if len(origins) != 1:
        raise EvidenceError("Cannot mix synthetic fixtures and live-source responses.")
    origin = origins.pop()
    rows = []
    for code in query.countries:
        for year in range(query.start_year, query.end_year + 1):
            present = (code, year) in values
            value, flag = values.get((code, year), (None, ""))
            rows.append({"country_code": code, "country_name": names[code], "year": year,
                         "indicator_code": indicator.code, "indicator_name": indicator.name,
                         "value": "" if value is None else str(value), "unit": indicator.unit,
                         "status": "available" if value is not None else "missing" if present else "not_returned",
                         "source_obs_status": flag, "data_origin": origin})
    provenance = {"schema_version": 1, "tool": "eco-evidence", "tool_version": __version__,
                  "created_at": utc_now(), "query": query.as_dict(), "data_origin": origin,
                  "indicator": {"code": indicator.code, "name": indicator.name, "unit": indicator.unit,
                                "unit_basis": "curated from official indicator name/definition; API unit may be blank",
                                "registry_checked_on": CHECKED_ON, "source_note": source_note,
                                "source_organization": source_organization},
                  "source": {"name": "World Development Indicators", "id": "2",
                             "last_updated": header.get("lastupdated"), "catalog_url": CATALOG_URL,
                             "license": "CC BY 4.0 as listed in WDI catalog; check indicator-specific terms",
                             "terms_url": TERMS_URL},
                  "row_count": len(rows), "counts": {status: sum(row["status"] == status for row in rows)
                                                        for status in ("available", "missing", "not_returned")}}
    return rows, provenance, snapshots


def export_bundle(query, reader, output):
    output = Path(output)
    if output.exists():
        raise EvidenceError("Output already exists; choose a new directory to preserve earlier evidence.")
    rows, provenance, snapshots = collect(query, reader)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        (temporary / "raw").mkdir()
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        csv_bytes = buffer.getvalue().encode("utf-8")
        (temporary / "data.csv").write_bytes(csv_bytes)
        provenance["csv_sha256"] = digest(csv_bytes)
        provenance["raw_sources"] = []
        for number, snapshot in enumerate(snapshots, 1):
            filename = f"raw/{number:03d}.json"
            (temporary / filename).write_bytes(snapshot.body)
            provenance["raw_sources"].append({"url": snapshot.url, "file": filename,
                                               "retrieved_at": snapshot.retrieved_at,
                                               "sha256": digest(snapshot.body)})
        (temporary / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if output.exists():
            raise EvidenceError("Output appeared during retrieval; refusing to replace it.")
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return provenance
