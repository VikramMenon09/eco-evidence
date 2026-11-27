# NOAA annual global atmospheric CO₂

`climate` downloads or replays one reviewed NOAA Global Monitoring Laboratory series: annual mean CO₂ at the global marine-surface background, with NOAA's reported uncertainty. This supplies environmental context for research alongside economic evidence. It does not measure national emissions, facility exposure, company footprints or policy effects.

## Fetch and reproduce

From the checkout, using Python 3.11 or newer:

```sh
python3 -m eco_evidence climate --start-year 2020 --end-year 2026 \
  --output runs/noaa-global-co2
```

The installed `eco-evidence climate` command accepts the same arguments. The output must be a new directory. Online use makes one fresh request to the fixed [NOAA annual global data URL](https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_gl.txt); there is no cache, API key, automatic retry or pagination.

Replay the committed example without network access:

```sh
python3 -m eco_evidence climate --start-year 2020 --end-year 2026 \
  --offline examples/noaa-global-co2 --output runs/noaa-replay
```

The requested years must match the archived query exactly. The full original file remains archived, including years outside that query, but replay does not silently select a different study window. With compatible code, `data.csv` is byte-identical; the new export timestamp can differ. Original source bytes and retrieval timestamps remain unchanged. For a new vintage or query, fetch into a new directory and retain the earlier archive.

## Meaning and limits

The [NOAA product description](https://gml.noaa.gov/ccgg/trends/global.html) defines CO₂ as a dry-air mole fraction, expressed in parts per million (ppm, equivalent to micromol/mol). The [global-mean methodology](https://gml.noaa.gov/ccgg/about/global_means.html) constructs a background estimate from selected marine boundary layer sites. It uses smoothing and data extension upstream. EcoEvidence does not recompute that estimate or impute additional values.

The mean and its reported uncertainty both retain their original decimal text and ppm units. The annual source file describes a Monte Carlo procedure with 200 global annual estimates using varied measurement records and summarizes its uncertainty as a mean of standard deviations. EcoEvidence preserves that explanation with the source. It does not reinterpret `unc` as a 95% confidence interval, a measurement count or the different uncertainty method used for NOAA growth rates.

NOAA warns that the latest annual data can change after recalibration and quality checks; earlier years can also revise. An `available` row means the source reports a value, not that it is final or a direct unprocessed measurement. The source's `File Creation` string has no timezone. It is retained literally and kept separate from the timezone-aware retrieval timestamp and from the version displayed on NOAA's web page. The DOI identifies the evolving product, not an immutable snapshot.

For economics and business research this series provides global background context. Assigning the same value to every country would not create independent national observations. The adapter therefore uses `global_marine_surface` as its spatial scope, has no country column and produces a separate artifact type. World Bank `panel`, `inspect` and `study` reject this archive. No automatic cross-source join, correlation, emissions conversion or business estimate is performed.

## Archived example

The attributed [example](../examples/noaa-global-co2/ATTRIBUTION.md) requests 2020–2026. Its raw source contains 47 annual records, 1979–2025, and carries the creation label `Thu Nov 19 14:14:29 2026`. It was retrieved on November 27, 2026 UTC (November 26 Pacific).

| Year | Mean CO₂, ppm | Reported uncertainty, ppm | Status |
| --- | ---: | ---: | --- |
| 2020 | 412.44 | 0.07 | available |
| 2021 | 414.70 | 0.06 | available |
| 2022 | 417.08 | 0.09 | available |
| 2023 | 419.35 | 0.10 | available |
| 2024 | 422.79 | 0.09 | available |
| 2025 | 425.63 | 0.05 | available |
| 2026 | — | — | not_returned |

These are the values of this archived vintage, not a promise that a fresh download returns the same numbers. The absent 2026 annual row does not imply that monthly NOAA observations for 2026 are unavailable.

## Output contract

```text
your-climate-bundle/
  data.csv
  provenance.json
  raw/co2_annmean_gl.txt
```

`data.csv` contains one row per requested year, in ascending order:

| Field | Meaning |
| --- | --- |
| `year` | Requested annual period |
| `co2_mean` | Exact source mean token; blank if the year is absent |
| `co2_uncertainty` | Exact source uncertainty token; blank if the year is absent |
| `unit` | `ppm` for both numerical columns |
| `status` | `available` or `not_returned` |
| `spatial_scope` | `global_marine_surface` |
| `data_origin` | `noaa_gml`, or `synthetic_fixture` for explicitly invented test data |

No documented missing-value sentinel was found for this annual product in the source review. Missing years receive `not_returned`; unsupported negative values are rejected, not guessed into a missing category. A reported zero uncertainty remains a real zero. The supported source format requires positive means and nonnegative uncertainties written as ordinary decimal tokens of at most 64 characters. Scientific notation, non-finite numbers or changed columns require source review rather than silent coercion.

`provenance.json` records `artifact_type: noaa_global_annual_co2`, a canonical `co2_global_annual` query, tool version, export timestamp, row/status counts and `data_csv_sha256`. `raw_sources` records the original source URL, contained file path, retrieval timestamp and SHA-256. Source metadata includes the literal file-creation label, source-wide parsed coverage, header comments, annual uncertainty explanation, revision caveat and citation DOI. Coverage of the full raw source is distinct from coverage of the requested window. Replay also records `original_manifest_sha256` for the input manifest; retain that input to verify this lineage reference, because the replay output does not embed the earlier manifest.

Replay verifies the manifest and raw file, then reconstructs values and source metadata from the raw text. It does not trust an earlier derived CSV or summary fields. Hashes establish consistency with the supplied manifest, not upstream authentication; use trusted archives and retain their repository history. The example's attribution file describes reuse and transformations but is not source data or an authenticated field of the output manifest.

## Validation and preservation

Years must be integers in ascending order from 1979 through the current UTC year. The fixed source URL and reviewed global-annual uncertainty explanation establish the supported series identity. The parser also requires ppm units, one file-creation label and the three-column `year mean unc` schema. It checks the uncertainty explanation after normalizing whitespace; changed method text requires review. Source years must be unique and strictly increasing, and data rows must end with a newline. Unknown schemas, malformed numbers, ambiguous JSON fields, changed hashes, inconsistent queries and unsupported origins are rejected.

The network uses verified HTTPS at the fixed NOAA URL. All redirects are refused before their destination is contacted; a moved endpoint requires a code/source review. Response completeness requires either one valid `Content-Length` matching the bytes received, or supported chunked framing decoded by the HTTP client. Duplicate, invalid, oversized or conflicting framing headers and responses delimited only by connection closure are refused. These conservative checks prevent an interrupted download from silently turning the end of the record into missing years. A final data row without its newline is also refused during offline replay. A newline alone cannot prove that a supplied archive is complete or authentic.

`--timeout` accepts 1–30 seconds (default 15) per blocking network operation, not a total wall-clock deadline. There are no background requests. This adapter uses a deliberately narrower transport contract than every response permitted by general [HTTP message-framing rules](https://www.rfc-editor.org/rfc/rfc9112.html#section-6.3).

Raw text and each manifest are capped at 1,000,000 bytes; generated CSV is capped at 5,000,000 bytes. Limits reject whole outputs without truncating rows. Local sources must be regular files inside the archive. Existing destinations, input-contained destinations and filesystem aliases into an input are refused. Exports stage a complete directory before publication; normal failures remove staging data. Hard interruptions can leave staging directories, and parent directories made before a late failure can remain. Concurrent replacement of an output or input while it is being processed is unsupported.

## Attribution and reuse

Data are provided by NOAA Global Monitoring Laboratory, Boulder, Colorado, USA, Carbon Cycle and Greenhouse Gases group. Cite Lan, X., Tans, P. and K. W. Thoning, *Trends in globally-averaged CO₂ determined from NOAA Global Monitoring Laboratory measurements*, [DOI: 10.15138/9N0H-ZH07](https://doi.org/10.15138/9N0H-ZH07), together with the archived file-creation label, retrieval date and source URL.

NOAA's [GML terms](https://gml.noaa.gov/about/disclaimer.html) describe government-server information as public domain unless specifically annotated otherwise, require acknowledgement and prohibit implied endorsement or altered material presented as official. The annual file separately asks for citation. The [example attribution](../examples/noaa-global-co2/ATTRIBUTION.md) records this source-specific review and EcoEvidence transformations. It does not claim rights over unrelated third-party material, photographs or logos. EcoEvidence is independent and is not endorsed by NOAA.
