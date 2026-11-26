# Comparing archived indicators

`eco-evidence panel` creates a wide table from two or three distinct reviewed indicators. It joins on World Bank country/economy code and year, preserving every country-year requested by any input. It runs entirely offline and does not fill missing observations, convert units, calculate ratios or estimate relationships.

## A complete example

From the repository root:

```sh
python3 -m eco_evidence panel \
  --bundle examples/synthetic-population \
  --bundle examples/synthetic-gdp \
  --output runs/synthetic-panel
```

Both inputs are explicitly synthetic software-test fixtures. Population requests USA for 2020–2022; GDP requests IND and USA for 2021–2023. The result has seven rows: IND for 2021–2023 and USA for 2020–2023. Only USA 2022 has a supplied number for both indicators. Its population is the invented zero, and its GDP is the invented exact decimal `12345.67890123456789`. These values are not statistics about either country.

| Indicator | Available | Explicitly missing | Requested but not returned | Not requested | Total union rows |
| --- | ---: | ---: | ---: | ---: | ---: |
| GDP | 3 | 1 | 2 | 1 | 7 |
| Population | 1 | 1 | 1 | 4 | 7 |

The union is a set of existing country-year keys, not a new cross-product of every country and every year. There is no IND 2020 row because neither input requested it. The panel retains rows with no available values; `complete_case_count` reports the all-indicator intersection without filtering the table to it.

For actual research, retrieve the inputs first. This example uses two reviewed series and intentionally mismatched windows:

```sh
python3 -m eco_evidence fetch --indicator gdp --countries IND,USA \
  --start-year 2020 --end-year 2023 --refresh --output runs/gdp
python3 -m eco_evidence fetch --indicator population --countries IND,USA \
  --start-year 2021 --end-year 2024 --refresh --output runs/population
python3 -m eco_evidence panel --bundle runs/gdp --bundle runs/population \
  --output runs/gdp-population-panel
```

Each output path must be new. Existing bundles, including their research notes, are preserved. Source availability and values can change; this command is not a promise that all requested years contain numbers.

## Output contract

```text
panel-directory/
  panel.csv
  coverage.json
  provenance.json
  inputs/
    gdp/
      data.csv
      provenance.json
      raw/...
    population/
      data.csv
      provenance.json
      raw/...
```

`panel.csv` starts with `country_code`, `year` and `data_origin`. For each indicator alias, in alphabetical order, it adds `ALIAS_value`, `ALIAS_status`, `ALIAS_unit`, `ALIAS_country_name` and `ALIAS_source_obs_status`. Rows sort by country code and ascending numeric year. Input argument order does not affect the CSV or coverage report.

| Status | Meaning | Value cell |
| --- | --- | --- |
| `available` | Source supplies a number, including zero | Exact parsed decimal text |
| `missing` | Source explicitly supplies `null` for a requested key | Empty |
| `not_returned` | A requested key is absent from a complete source response | Empty |
| `not_requested` | The key is outside this input bundle's query | Empty |

Units are present even for `not_requested` cells. The corresponding country name and observation flag are blank because that input has no row at the key. Country names remain separate for each indicator: different archived names are not silently reconciled, and names are never join keys. Every row carries the common `data_origin`; combining `synthetic_fixture` and `worldbank_api` inputs is rejected.

`coverage.json` records all four status counts for each indicator. Each indicator's counts sum to `row_count`, the number of union rows. `complete_case_count` counts rows where every selected indicator is `available`; a numerical zero is available. This is a coverage description, not a quality score or an endorsement of complete-case analysis. Differences in requested windows alone can change it.

`provenance.json` uses `schema_version: 1` and `artifact_type: indicator_panel`. It records the tool version, creation time, join keys/order, row and complete-case counts, file hashes, and each input's canonical query, indicator identity/definition/unit, source organization, source revision and terms. `panel_csv_sha256` and `coverage_sha256` identify the derived files. Each input entry records its own `data_origin`, `bundle_path`, `original_manifest_sha256` and `embedded_manifest_sha256`. A `synthetic_fixture` input is invented test material, even though it uses the WDI response structure and associated catalog fields.

The original-manifest hash identifies the bytes read from the supplied bundle. The embedded manifest is newly generated with this tool version and export time, so its hash can differ. Exact raw response bytes and their original retrieval timestamps are retained. The hashes detect inconsistencies with a trusted manifest; they are not source signatures or independent proof that a bundle came from the World Bank.

## Reconstructing and auditing

The composer validates each input's canonical query, source manifest, raw hashes and source schemas, then rebuilds the indicator rows. It does **not** read or authenticate earlier input CSVs. A changed or missing input `data.csv` does not change a panel built from intact raw evidence. To audit an earlier CSV, compare it separately to a reconstructed CSV.

Rebuild using only the embedded inputs, in either order:

```sh
python3 -m eco_evidence panel \
  --bundle runs/synthetic-panel/inputs/gdp \
  --bundle runs/synthetic-panel/inputs/population \
  --output runs/synthetic-panel-replay
```

For the same code version, `panel.csv` and `coverage.json` are byte-identical. New export timestamps and manifest lineage can differ. Preserve the tool version and its reviewed registry for historical replay: a later registry update can reject an earlier indicator name or unit. No original input directory or network access is needed. Pass the `inputs/ALIAS` directories, not the top-level panel directory, to this command or to a matching `fetch --offline` query.

The panel permits one bundle per indicator. Duplicate indicators, mixed origins, malformed queries, source/schema/hash errors and an existing output path are rejected with exit status 2. Output inside an input bundle is also rejected. Writing uses a sibling staging directory so a normal failure does not leave a partial final panel. Input count is capped at three; the existing source/file limits apply, and generated panel metadata is capped at 1 MB per JSON file. A hard process interruption can leave staging files. Inputs must not be modified concurrently during collection; closely concurrent filesystem replacement is not an atomic locking protocol.

## Interpretation and reuse

Alignment does not make unlike concepts interchangeable. GDP is total output in constant 2015 US dollars, population is people, and renewables is the percentage of **total final energy consumption**, not electricity generation. Source definitions, estimation methods, revision vintages and country coverage still matter. Separate indicator downloads are not an atomic database snapshot, even when database revision labels match. Retained timestamps and metadata let a researcher assess these differences.

No country panel should be treated as a company-specific estimate or causal effect. Cite the indicator code/name, World Development Indicators, original provider, source URL and retrieval timestamp in each embedded manifest, and describe the outer join as a subsequent transformation. Retain the [WDI catalog license](https://datacatalog.worldbank.org/search/dataset/0037712/world-development-indicators) and applicable [source terms](https://www.worldbank.org/ext/en/legal/terms-conditions/datasets); the project code license does not relicense upstream data. The example fixtures in this guide are invented project-authored test material.
