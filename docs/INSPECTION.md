# Inspecting source metadata and archive changes

`inspect` turns an evidence bundle into a research-readable metadata report. With `--baseline`, it also compares two archives of the **same canonical query**. Both operations are offline and read-only. They consume the archived raw responses, not the previous `data.csv` or the manifest's derived indicator/source/count summaries.

```sh
python3 -m eco_evidence inspect --bundle examples/synthetic-population
python3 -m eco_evidence inspect --bundle examples/synthetic-population-revised \
  --baseline examples/synthetic-population --format json
```

These fixtures contain invented values and dates and remain labeled `synthetic_fixture`. The second example reports three changed rows: omitted-to-null in 2020, null-to-42 in 2021, and zero-to-10 in 2022. There are two status transitions and one numerical change between two available values. The definition and raw-response catalog also change. The unchanged database update label does not suppress those differences.

## Using real archives

Collect one query, keep its directory, and repeat that exact query when investigating later source updates. For example, run the first command now and the second when collecting the later archive:

```sh
python3 -m eco_evidence fetch --indicator population --countries IND,USA \
  --start-year 2020 --end-year 2022 --refresh --output runs/population-baseline
python3 -m eco_evidence fetch --indicator population --countries IND,USA \
  --start-year 2020 --end-year 2022 --refresh --output runs/population-followup
python3 -m eco_evidence inspect --bundle runs/population-followup \
  --baseline runs/population-baseline
```

`--refresh` requests observations afresh; metadata is already refreshed on every online fetch. Running both downloads immediately is valid but may find no source change. No change is a useful result, not a reason to invent revisions. The inspection step makes no requests even when the archives came from live retrievals.

The comparison direction is **baseline to bundle**, determined by the arguments. EcoEvidence does not infer that one archive is chronologically newer. Reversed arguments are valid and reverse the before/after values. Independent API requests are not an atomic database snapshot.

Different indicators, country sets, year windows or data origins are rejected. Use `panel` to combine indicators or differing query coverage. To inspect a panel's source, point `--bundle` at its `inputs/ALIAS` directory; a panel directory itself is not a single-indicator bundle.

## What the report means

The JSON report has `schema_version`, `artifact_type`, `tool_version`, `bundle` and `comparison`. `comparison` is null when no baseline is supplied. The `bundle` overview contains:

| Field | Interpretation |
| --- | --- |
| `query`, `data_origin` | Validated canonical query and archived origin label |
| `indicator` | Verified identity, definition, original provider text, reviewed unit and its basis |
| `source.last_updated` | WDI database update label reported in the observation response; null if the exact complete-empty envelope reports none |
| `raw_sources` | Each consumed response's URL, role, original retrieval timestamp and SHA-256 |
| `row_count`, `counts` | Reconstructed query coverage, distinguishing available, explicit null and omitted rows |
| `licensing` | Explicitly qualified catalog/registry context and links; not a license declaration extracted from the archived API |
| `original_manifest_sha256` | Identity of the manifest bytes read, not a source signature |

The current registry supplies the reviewed unit. The API's unit field must retain its reviewed blank value; a changed name or unit is refused for source review. Full provider and definition strings come from the archived indicator response. A newer export or inspection does not refresh those original source retrieval dates.

Observation years, database updates, retrieval timestamps and the original bundle's export time have different meanings. In particular, a database update is not a measurement date or proof that this indicator changed. Metadata and observation pages can have different retrieval dates when a fetch reused cached observations. The report retains those per-response dates rather than collapsing them into a misleading single vintage date.

## Comparing changes

- `metadata_changes` contains field-level before/after values. `source_update_changed` compares update labels only, including known-to-unknown transitions.
- `row_changes` contains every changed country-year, in sorted order, with field-level before/after values. Values remain decimal strings. Status, observation flags and country names are compared separately.
- `row_field_change_counts` counts exact field differences. Its `value` count includes changes in decimal representation and transitions between an empty field and an available number.
- `numeric_value_change_count` counts unequal exact decimal numbers **only where both archives have status `available`**. Zero is a reported number. Null-to-number and omitted-to-null transitions are captured by the status/value fields, not disguised as numeric differences from zero.
- `value_representation_change_count` counts unequal reconstructed strings with equal exact numeric values, such as `1` and `1.00`. Original JSON spelling is retained in raw files; equivalent spellings normalized by JSON parsing may be visible only as changed raw hashes.
- `raw_source_changes` identifies added, removed or changed response URLs and compares hashes and retrieval timestamps. A changed hash can result from formatting, pagination or metadata changes; it does not by itself prove changed observations. Counts for body/timestamp changes apply only to URLs present in both archives; added/removed responses have separate counts.

All details are retained, without sampling. The text report uses ASCII separators and escapes source-supplied strings so embedded terminal control characters are displayed as text. Legitimate non-ASCII names also appear as Unicode escapes; JSON parsers recover the original text from those escapes. With the same compatible code/registry and unchanged inputs, output is deterministic; no current inspection timestamp is injected.

## Licensing and interpretation

The [World Bank indicator API documentation](https://datahelpdesk.worldbank.org/knowledgebase/articles/898599-indicator-api-queries) describes source notes, source organizations, units and dataset identifiers. The [WDI catalog](https://datacatalog.worldbank.org/search/dataset/0037712/world-development-indicators) lists Creative Commons Attribution 4.0, checked on November 24, 2026. That catalog-level context is separate from an indicator-specific permission assessment.

The existing [dataset terms link](https://www.worldbank.org/en/about/legal/terms-of-use-for-datasets) redirected to the World Bank's general terms during that check, including when following its dataset-terms link. This inspection feature does not resolve provider-specific terms or authorize redistribution. Retain World Bank and original-provider attribution, review applicable terms, and distinguish synthetic software fixtures from official source observations. The software license does not relicense upstream data.

Definition changes can alter interpretation even when numbers stay the same. Conversely, equal definitions and units do not establish full historical comparability. This tool identifies observable archive differences; it does not infer why a revision occurred, whether it is an error, or a causal/economic conclusion.

## Validation and limits

Canonical queries, metadata identity, pagination, finite numbers, raw hashes, bounded regular files and timezone-bearing retrieval timestamps use the same archive validation as panels. Corrupt or incompatible archives fail with exit code 2 and no partial report. No network request, input modification, output-directory creation or cache write is part of inspection. Shell redirection is controlled by the caller; never redirect a report over an input file.

Each reconstructed overview is limited to 1 MB and the complete JSON report to 5 MB. Over-limit reports fail rather than silently dropping changes. These limits supplement the existing 1 MB input-manifest, 5 MB raw-response and 30-response limits. They are not a peak-memory guarantee.

Hashes and origin labels are not source authentication. Someone able to replace both raw files and the manifest can manufacture a consistent archive. Use a trusted custodian and retain version history. Concurrent archive modification is unsupported. Input CSVs are intentionally not read or authenticated; this report cannot certify that an earlier derived CSV was unchanged.
