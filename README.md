# EcoEvidence

Reproducible evidence bundles for economics and environmental research. Retrieve reviewed World Bank country indicators or NOAA global annual atmospheric CO₂, keeping exact source responses next to tidy CSVs. Every bundle records source identity, units, retrieval timestamps and content hashes. World Bank indicator panels retain unmatched countries and years and report their coverage; NOAA concentration records retain the reported uncertainty and global spatial scope.

This toolkit is a data foundation for a long-term research resource. It does not infer causality, rank countries or silently fill missing values. Its useful next contributions are documented indicator additions, source checks, validation improvements and reproducible research examples. The [engineering guide](docs/ENGINEERING.md) explains its data flow, trust boundaries, failure behavior and maintenance checks.

## Run it

Requires Python 3.11 or later. There are **no runtime dependencies**. From this checkout:

```sh
python3 -m eco_evidence indicators
python3 -m eco_evidence --version
python3 -m eco_evidence fetch --indicator population --countries USA,IND \
  --start-year 2020 --end-year 2022 --output runs/population-2020-2022
```

Each output directory must be new; the command never intentionally replaces an earlier bundle. The default cache is `.cache/eco-evidence` in the current working directory. Cached observations retain their original retrieval dates; online country and indicator metadata are fetched afresh on every run. Add `--refresh` to fetch observation pages afresh too, or `--cache-dir PATH` to choose the cache location. Use `--offline` for a fully archived replay.

Optional installation in a virtual environment creates the `eco-evidence` command:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/eco-evidence indicators
```

Installation needs the build backend; running `python3 -m eco_evidence` directly needs only the standard library.

## Reviewed indicator registry

Official metadata was checked on **22 November 2026**. Online runs fetch current country and indicator metadata; offline runs validate the archived metadata. Indicator names and source IDs are checked against the registry, and each observation's indicator name must match. The API's `unit` field is blank for these series, so the registry explicitly records units derived from official names and definitions. Both metadata and observation unit fields must remain blank; a changed name or unit field stops export for review. Definition changes not reflected in these fields still need source review.

| Alias | World Bank code | Unit | Meaning and scope |
| --- | --- | --- | --- |
| `gdp` | `NY.GDP.MKTP.KD` | constant 2015 US$ | Annual domestic output in constant prices; total GDP, not per capita or PPP. [Official metadata](https://api.worldbank.org/v2/indicator/NY.GDP.MKTP.KD?format=json) |
| `population` | `SP.POP.TOTL` | people | Total midyear population estimate under the source's residency definition. [Official metadata](https://api.worldbank.org/v2/indicator/SP.POP.TOTL?format=json) |
| `renewables` | `EG.FEC.RNEW.ZS` | % of total final energy consumption | Renewable share of final energy consumption; not the renewable share of electricity generation. [Official metadata](https://api.worldbank.org/v2/indicator/EG.FEC.RNEW.ZS?format=json) |

All three use World Development Indicators, source ID `2`. Original providers are retained in each bundle. The renewable series metadata credits the IEA; population and GDP combine several official sources. Availability varies by indicator, country and year. The latest calendar year may have no reported values, and source revisions can change earlier years.

## Output contract

```text
your-bundle/
  data.csv
  provenance.json
  raw/001.json
  raw/002.json
  ...
```

`data.csv` has one row for every requested country-year, sorted by country code and ascending year. Columns are `country_code`, `country_name`, `year`, `indicator_code`, `indicator_name`, `value`, `unit`, `status`, `source_obs_status`, and `data_origin`.

| Source situation | `value` | `status` |
| --- | --- | --- |
| Source provides a number, including zero | Exact parsed decimal representation; zero remains `0` | `available` |
| Source explicitly returns `null` | Empty CSV cell | `missing` |
| A requested country-year is absent from a complete response | Empty CSV cell | `not_returned` |

`available` means a source supplied a number; it does not claim a direct measurement rather than an estimate. No interpolation, zero filling, rounding, unit conversion or economic transformation occurs. Decimal JSON numbers are parsed as decimal values rather than binary floats. Original numeric spelling remains available in the raw response.

For a query window with no source records, WDI can return its all-zero pagination/null-data envelope. EcoEvidence accepts that exact complete-empty format after validating country and indicator metadata, and exports every requested country-year as `not_returned`. The source revision date is then `null` because the empty response does not report one; the retrieval timestamp remains available. Partially empty or inconsistent envelopes are rejected.

`provenance.json` contains the canonical query, tool version, export timestamp, source metadata/revision date, row/status counts, the CSV SHA-256, and every raw response URL, original retrieval timestamp, filename and SHA-256. These checks detect accidental changes; they are not a cryptographic signature from the source. The manifest is not self-authenticating: someone able to edit it can change hashes or origin labels. Replay bundles from a trusted custodian, and retain their version-controlled history. The WDI update date is a database date, not the date an individual observation was measured.

## Offline reproducibility

Replay an earlier bundle with **zero network calls**:

```sh
python3 -m eco_evidence fetch --indicator population --countries USA,IND \
  --start-year 2020 --end-year 2022 --offline runs/population-2020-2022 \
  --output runs/population-replay
```

The archived query must match the requested URLs exactly. Country order is normalized, so `IND,USA` and `USA,IND` select the same query. Raw-response hashes and original retrieval timestamps are preserved. For the same code version, the resulting CSV is byte-identical; the new export timestamp in provenance can differ.

A deliberately **synthetic** offline fixture demonstrates a zero, a missing value and an omitted year:

```sh
python3 -m eco_evidence fetch --indicator population --countries USA \
  --start-year 2020 --end-year 2022 --offline examples/synthetic-population \
  --output runs/synthetic-demo
```

Every fixture row is marked `data_origin=synthetic_fixture`. Its values are invented for testing and must never be cited as country statistics. A live-source bundle uses `worldbank_api`. No source silently falls back to invented values when a request fails.

## Comparable indicator panels

Join **2–3 different indicator bundles** entirely offline:

```sh
python3 -m eco_evidence panel \
  --bundle examples/synthetic-population \
  --bundle examples/synthetic-gdp \
  --output runs/synthetic-panel
```

This runnable example contains **invented test data**, not country statistics. It produces seven country-year rows from overlapping queries, with one row having a reported number for both indicators. GDP and population keep their original units. For research, pass your own real `fetch` outputs using the same command.

The output contains `panel.csv`, `coverage.json`, `provenance.json` and self-contained input bundles under `inputs/`. Rows are the **union of country-year keys** from the input queries. Each indicator has separate value, status, unit, country-name and observation-flag columns. A key outside an indicator's query is `not_requested`; this is distinct from `missing`, `not_returned` and a real zero. Nothing is silently dropped, interpolated or converted.

The panel is reconstructed from checksum-verified raw responses. Earlier input `data.csv` files are not read or authenticated by this command. It preserves source metadata, original retrieval timestamps and revision labels in its embedded bundles. Synthetic and live sources cannot be mixed. Joining by country and year does not itself establish that definitions, time coverage or economic concepts are comparable.

See the [panel guide](docs/PANELS.md) for the exact output contract, coverage interpretation and an offline reconstruction command.

## Audit an archive's coverage and review flags

Version 0.6.0 adds an offline quality report for one World Bank indicator bundle:

```sh
python3 -m eco_evidence quality --bundle examples/synthetic-population
python3 -m eco_evidence quality --bundle examples/output-energy-study/inputs/renewables --format json
```

The first command audits invented test data; the second audits the attributed real archive already included in the worked study. Reports retain every country-year, exact reconstructed value string, source flag and unit. They show consecutive gaps, keeping explicit nulls distinct from omitted records and real zeroes, and flag values outside stated project review bounds. A flag requests review; it does not replace a value or establish a source error. No network access, input modification, imputation or quality score is involved.

Both text and JSON include source definitions, original retrieval dates, hashes and licensing context. See the [quality-report guide](docs/QUALITY.md) for the rules, gap classifications, complete row ledger and interpretation limits. Use `inspect --baseline` separately when comparing source revisions.

## Inspect metadata and compare archives

Read an archived indicator's definitions, original provider, units, coverage and source dates without making network requests:

```sh
python3 -m eco_evidence inspect --bundle examples/synthetic-population
python3 -m eco_evidence inspect --bundle examples/synthetic-population-revised \
  --baseline examples/synthetic-population --format json
```

The comparison example is **invented test data**. It shows three changed country-years, including two status transitions and one change between reported numbers, even though the database update label is unchanged. For actual research, use two real bundles collected with the same indicator, countries and years. `--baseline` defines the comparison direction; it does not establish chronological order.

Inspection reconstructs the raw responses instead of trusting an earlier CSV or its metadata summary. It distinguishes a changed definition, provider or database update label from changed values, missingness, observation flags and country names. Exact decimal comparison separates numerical changes from representation changes such as `1` versus `1.00`. Every raw response retains its own retrieval timestamp and hash. Catalog-level licensing context is labeled separately from metadata actually reported by the API; no indicator-specific reuse permission is inferred.

Text is the default; `--format json` emits a complete deterministic report on stdout. Inspection never writes to a bundle. See the [inspection guide](docs/INSPECTION.md) for report fields, source-date interpretation and a fresh-data comparison workflow.

## Worked study: output per person and renewable energy

Version 0.4.0 includes a [worked study](docs/OUTPUT_ENERGY_STUDY.md) with an attributed, archived World Bank extract for six countries, 2019–2023. Reproduce it without network access:

```sh
python3 -m eco_evidence study \
  --gdp-bundle examples/output-energy-study/inputs/gdp \
  --population-bundle examples/output-energy-study/inputs/population \
  --renewables-bundle examples/output-energy-study/inputs/renewables \
  --output runs/output-energy-replay
```

The study keeps all 30 country-years and computes GDP divided by population where possible, using explicitly recorded decimal rounding. It retains 12 rows whose renewable observations are missing; 18 rows form complete pairs. The output contains `study.csv`, `summary.json`, provenance and self-contained input bundles. This is a descriptive comparison of domestic output and final energy use, with [series-specific licensing and attribution](examples/output-energy-study/ATTRIBUTION.md). It is not a measure of household income, purchasing power, electricity procurement or company performance.

## NOAA global atmospheric CO₂

Version 0.5.0 adds a second primary source: [NOAA GML's annual global marine-surface CO₂ record](https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_gl.txt). Fetch a fresh archive:

```sh
python3 -m eco_evidence climate --start-year 2020 --end-year 2026 \
  --output runs/noaa-global-co2
```

Or reproduce the attributed example entirely offline:

```sh
python3 -m eco_evidence climate --start-year 2020 --end-year 2026 \
  --offline examples/noaa-global-co2 --output runs/noaa-replay
```

The example retains six reported annual means and uncertainties, plus an explicit `not_returned` row for 2026. Values are dry-air mole fractions in ppm. They describe the global marine-surface background, not national emissions or a business's carbon footprint. This separate archive format cannot be passed to the World Bank `panel`, `inspect` or `study` commands. See the [climate guide](docs/CLIMATE.md) for exact fields, source revisions, transport limits and [NOAA attribution](examples/noaa-global-co2/ATTRIBUTION.md).

## World Bank validation and failure behavior

- Select one reviewed indicator and 1–20 distinct three-letter World Bank country/economy codes. The CLI normalizes letter case. Official country metadata must recognize every code; regional and income aggregates are excluded in this MVP.
- Years run from 1960 through the current UTC year, in ascending order. An accepted year does not imply data availability.
- Requests use HTTPS with certificate verification. Foreign-host, insecure, credential-bearing and nonstandard-port redirects are rejected before dispatch. Same-origin redirects count toward the 30-request budget. Each endpoint is limited to ten pages and each raw response to 5 MB.
- The default 15-second timeout (configurable from 1–30) applies to blocking network operations; it is not a total wall-clock deadline for a download. There are no automatic retries or unbounded pagination loops.
- Local raw responses are capped at 5 MB, cache envelopes at 7 MB and bundle manifests at 1 MB before parsing. Generated manifests use the same 1 MB limit, so an export cannot produce an oversized manifest that its replay reader rejects. Country names are limited to 1,024 UTF-8 bytes and observation flags to 256 bytes to bound text repeated in CSV rows. Pipes and devices are not accepted as archived input. Failed cache writes remove their temporary file and preserve an existing entry when replacement fails.
- HTTP errors, invalid schemas, ambiguous duplicate JSON fields, duplicate observations, invalid numeric representations, invalid Unicode text, missing identity fields, unexpected indicator names/units, invalid source revision dates and changing pagination/revision metadata stop the run. The CLI reports these input failures without a traceback. A final output directory is written only after successful validation.
- Cache entries are retained until refreshed. For current research, use `--refresh`; for an exact historical replay, use `--offline`. Independent requests are not an atomic database snapshot: a stable database update label reduces, but cannot eliminate, revision timing risk.

Online runs can combine newly retrieved metadata with older cached observations; every raw timestamp remains explicit. Use `--refresh` when collecting a current research snapshot. New bundle directories are owner-only by default; publish or share their contents using your project's normal sharing process.

If Python reports a certificate verification failure, configure your Python installation's trusted CA bundle. On systems where `/etc/ssl/cert.pem` is the maintained trusted bundle, `SSL_CERT_FILE=/etc/ssl/cert.pem python3 ...` is a standard option. Certificate checks are never disabled by this project.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The suite uses synthetic, clearly labeled responses, attributed real archives and mocked HTTP failures. It exercises the actual command entry point, pagination consistency, precise numeric preservation, missing-versus-zero semantics, invalid inputs and schemas, hash-verified offline replay, CSV/provenance agreement, request limits, and incomplete-run cleanup. Redirect tests run urllib's actual redirect handlers with a fake transport and verify that rejected destinations receive no request. Tests make no network calls.

## World Bank sources, attribution and licenses

The [World Bank WDI catalog](https://datacatalog.worldbank.org/search/dataset/0037712/world-development-indicators) lists Creative Commons Attribution 4.0. Retain attribution to World Bank and the original providers named in indicator metadata; review [World Bank dataset terms](https://www.worldbank.org/ext/en/legal/terms-conditions/datasets) and any indicator-specific conditions before reuse. A code license does not relicense upstream data. EcoEvidence is independent and is not endorsed by the World Bank or other providers.

For a downloaded bundle, cite the indicator code/name, World Development Indicators, original provider, retrieval date and URL recorded in `provenance.json`. State any later transformations separately. For API mechanics, see the official [basic call structures](https://datahelpdesk.worldbank.org/knowledgebase/articles/898581-api-basic-call-structures) and [indicator query documentation](https://datahelpdesk.worldbank.org/knowledgebase/articles/898599-indicator-api-queries).
