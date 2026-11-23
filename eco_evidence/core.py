"""Bounded World Bank retrieval, schema validation, and auditable bundle export."""

import base64
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
import hashlib
from http.client import HTTPException
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .registry import API_BASE, CATALOG_URL, CHECKED_ON, TERMS_URL, get_indicator

MAX_BYTES = 5_000_000
MAX_MANIFEST_BYTES = 1_000_000
MAX_CACHE_BYTES = 7_000_000  # Base64 for a 5 MB response plus its envelope.
MAX_COUNTRY_NAME_BYTES = 1024
MAX_OBS_STATUS_BYTES = 256
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
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Repeated JSON field: {key}")
            result[key] = value
        return result
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=invalid_constant,
                          object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError, DecimalException) as exc:
        raise EvidenceError("Source is not unambiguous finite JSON.") from exc


def read_bounded_file(filename, limit):
    """Read a regular file with a size cap; never consume a pipe or device."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(filename, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise EvidenceError("Input must be a regular file within its size limit.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            body = stream.read(limit + 1)
        if len(body) > limit:
            raise EvidenceError("Input grew beyond its size limit while being read.")
        return body
    finally:
        if descriptor is not None:
            os.close(descriptor)


def require_api_url(url):
    """Validate the destination before opening it, including every redirect."""
    try:
        target = urlparse(url)
        valid = (isinstance(url, str) and target.scheme == "https"
                 and target.hostname == "api.worldbank.org" and target.port in (None, 443)
                 and target.username is None and target.password is None and not target.fragment)
    except (ValueError, TypeError, AttributeError) as exc:
        raise EvidenceError("Invalid World Bank API URL.") from exc
    if not valid:
        raise EvidenceError("Only the official HTTPS World Bank API is supported.")
    return target


class OfficialRedirectHandler(HTTPRedirectHandler):
    """Reject foreign or downgraded redirects before their request is sent."""

    def __init__(self, charge_redirect):
        self.charge_redirect = charge_redirect

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            require_api_url(newurl)
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
            if redirected is not None:
                self.charge_redirect()
            return redirected
        except BaseException:
            fp.close()
            raise


def open_official(request, *, timeout, charge_redirect):
    return build_opener(OfficialRedirectHandler(charge_redirect)).open(request, timeout=timeout)


def required_text(value, field, allow_empty=False, *, max_bytes=None):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise EvidenceError(f"Invalid or missing source field: {field}.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise EvidenceError(f"Source field is not valid Unicode text: {field}.") from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise EvidenceError(f"Source field exceeds {max_bytes} UTF-8 bytes: {field}.")
    return value


def integer(value, field, minimum=0):
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        try:
            value = int(value)
        except ValueError as exc:
            raise EvidenceError(f"Invalid source integer: {field}.") from exc
    if type(value) is not int or value < minimum:
        raise EvidenceError(f"Invalid source integer: {field}.")
    return value


def source_date(value):
    required_text(value, "lastupdated")
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("expected ISO date")
        date.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceError("Source lastupdated must be a valid YYYY-MM-DD date.") from exc
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

    def charge_request(self):
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise EvidenceError("Request budget exceeded (30 requests, including redirects).")

    def get(self, url):
        target = require_api_url(url)
        cache_file = self.cache_dir / f"{digest(url.encode())}.json"
        is_metadata = "/indicator/" not in target.path or target.path.startswith("/v2/indicator/")
        if cache_file.exists() and not self.refresh and not is_metadata:
            try:
                entry = decode_json(read_bounded_file(cache_file, MAX_CACHE_BYTES))
                body = base64.b64decode(entry["body_base64"], validate=True)
                if entry["url"] != url or digest(body) != entry["sha256"]:
                    raise ValueError("checksum mismatch")
                result = Snapshot(url, body, entry["retrieved_at"])
                checked_payload(result)
                return result
            except (KeyError, ValueError, OSError, TypeError, RecursionError) as exc:
                raise EvidenceError("Cache entry failed verification; rerun with --refresh.") from exc
        self.charge_request()
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": f"EcoEvidence/{__version__}"})
            with open_official(request, timeout=self.timeout, charge_redirect=self.charge_request) as response:
                require_api_url(response.geturl())
                body = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            exc.close()
            raise EvidenceError(f"World Bank HTTP {exc.code}; no bundle was produced. Retry later if rate-limited.") from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            raise EvidenceError(f"World Bank request failed: {exc}. Check connectivity and trusted TLS certificates.") from exc
        result = Snapshot(url, body, utc_now())
        checked_payload(result)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        entry = {"url": url, "retrieved_at": result.retrieved_at, "sha256": digest(body),
                 "body_base64": base64.b64encode(body).decode("ascii")}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.cache_dir, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(entry, handle, sort_keys=True)
            temporary.replace(cache_file)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return result


class BundleReader:
    """Network-free replay of byte-verified raw responses from a saved bundle."""

    def __init__(self, directory):
        try:
            self.directory = Path(directory).resolve()
            manifest = decode_json(read_bounded_file(self.directory / "provenance.json", MAX_MANIFEST_BYTES))
            if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
                raise ValueError("unsupported bundle schema")
            self.origin = manifest["data_origin"]
            if self.origin not in ("worldbank_api", "synthetic_fixture"):
                raise ValueError("unknown bundle origin")
            sources = manifest["raw_sources"]
            if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_REQUESTS:
                raise ValueError("source count")
            for item in sources:
                if not isinstance(item, dict):
                    raise ValueError("invalid raw source")
                for field in ("url", "file", "retrieved_at", "sha256"):
                    required_text(item[field], field)
                require_api_url(item["url"])
                if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                    raise ValueError("invalid source checksum")
            self.sources = {item["url"]: item for item in sources}
            if len(self.sources) != len(sources):
                raise ValueError("duplicate sources")
        except (KeyError, ValueError, OSError, TypeError, RecursionError, RuntimeError) as exc:
            raise EvidenceError("Offline bundle has no valid provenance/source manifest.") from exc

    def get(self, url):
        try:
            source = self.sources[url]
            filename = (self.directory / source["file"]).resolve()
            filename.relative_to(self.directory)
            body = read_bounded_file(filename, MAX_BYTES)
            if digest(body) != source["sha256"]:
                raise ValueError("checksum mismatch")
            return Snapshot(url, body, source["retrieved_at"], self.origin)
        except (KeyError, ValueError, OSError, TypeError, RuntimeError) as exc:
            raise EvidenceError("Offline source missing or invalid; query must match archived requests and hashes.") from exc


def request_url(path, params, page):
    return f"{API_BASE}/{path}?{urlencode({**params, 'format': 'json', 'per_page': PER_PAGE, 'page': page})}"


def unreported_empty_response(meta, items):
    """The WDI API's exact no-records envelope has no revision/source header."""
    fields = {"page", "pages", "per_page", "total", "sourceid", "lastupdated"}
    return (set(meta) == fields and items is None
            and all(type(meta[key]) is int and meta[key] == 0 for key in ("page", "pages", "per_page", "total"))
            and meta["sourceid"] is None and meta["lastupdated"] is None)


def fetch_pages(reader, path, params, snapshots):
    records = []
    first = None
    for page in range(1, MAX_PAGES + 1):
        snapshot = reader.get(request_url(path, params, page))
        meta, items = checked_payload(snapshot)
        if page == 1 and unreported_empty_response(meta, items):
            snapshots.append(snapshot)
            return [], meta
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
        if not isinstance(code, str) or code not in query.countries or code in names:
            raise EvidenceError("Country response contains an unexpected or duplicate code.")
        region = country.get("region")
        if (not isinstance(region, dict) or not isinstance(region.get("id"), str)
                or not region["id"].strip() or region["id"] == "NA"):
            raise EvidenceError("Regional/income aggregates are not country/economy inputs.")
        names[code] = required_text(country.get("name"), "country name", max_bytes=MAX_COUNTRY_NAME_BYTES)
    if set(names) != set(query.countries):
        raise EvidenceError("One or more country codes were not recognized by the World Bank.")
    metadata, _ = fetch_pages(reader, f"indicator/{indicator.code}", {"source": 2}, snapshots)
    if len(metadata) != 1:
        raise EvidenceError("Expected exactly one indicator metadata record.")
    definition = metadata[0]
    if (definition.get("id") != indicator.code or definition.get("name") != indicator.name
            or definition.get("unit") != ""
            or not isinstance(definition.get("source"), dict) or definition["source"].get("id") != "2"):
        raise EvidenceError("Indicator metadata or its unit changed, or is not WDI; review the registry before export.")
    source_note = required_text(definition.get("sourceNote"), "indicator definition")
    source_organization = required_text(definition.get("sourceOrganization"), "source organization")
    observations, header = fetch_pages(reader, f"{country_path}/indicator/{indicator.code}",
                                        {"source": 2, "date": f"{query.start_year}:{query.end_year}"}, snapshots)
    if not observations and unreported_empty_response(header, None):
        # Dataset identity is already verified by the indicator metadata above.
        # The no-records envelope does not report a revision; never invent one.
        last_updated = None
    else:
        if header.get("sourceid") != "2":
            raise EvidenceError("Observation response does not identify WDI source 2.")
        last_updated = source_date(header.get("lastupdated"))
    values = {}
    for record in observations:
        code = record.get("countryiso3code")
        year_text = record.get("date")
        if (not isinstance(code, str) or code not in names
                or not isinstance(year_text, str) or not re.fullmatch(r"[0-9]{4}", year_text)
                or not query.start_year <= int(year_text) <= query.end_year
                or not isinstance(record.get("indicator"), dict) or record["indicator"].get("id") != indicator.code
                or record["indicator"].get("value") != indicator.name or record.get("unit") != ""):
            raise EvidenceError("Observation has an unexpected country, year, indicator or unit; review metadata drift.")
        key = (code, int(year_text))
        if key in values:
            raise EvidenceError("Duplicate country-year observations are not allowed.")
        if "value" not in record:
            raise EvidenceError("Observation value field is missing.")
        value = record["value"]
        if value is not None and not (type(value) is int or isinstance(value, Decimal) and value.is_finite()):
            raise EvidenceError("Observation value must be a finite JSON number or null.")
        flag = required_text(record.get("obs_status", ""), "observation status", allow_empty=True,
                             max_bytes=MAX_OBS_STATUS_BYTES)
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
                                "unit_basis": "curated from official indicator name/definition; API unit field verified blank",
                                "registry_checked_on": CHECKED_ON, "source_note": source_note,
                                "source_organization": source_organization},
                  "source": {"name": "World Development Indicators", "id": "2",
                             "last_updated": last_updated, "catalog_url": CATALOG_URL,
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
        manifest_bytes = (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise EvidenceError("Generated provenance exceeds the 1 MB manifest limit; review source metadata.")
        (temporary / "provenance.json").write_bytes(manifest_bytes)
        if output.exists():
            raise EvidenceError("Output appeared during retrieval; refusing to replace it.")
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return provenance
