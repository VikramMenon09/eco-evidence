# Offline archive quality reports

`quality` makes one archived World Bank indicator easier to review before building a panel or study. It reconstructs every requested country-year from checksum-verified raw responses, then reports coverage, consecutive gaps and transparent screening flags. It does not modify the archive or fetch current data.

```sh
python3 -m eco_evidence quality --bundle examples/synthetic-population
python3 -m eco_evidence quality --bundle examples/output-energy-study/inputs/renewables --format json
```

Text is the default; `--format json` selects the machine-readable report. Both go to stdout. Use a study or panel's `inputs/ALIAS` directory, not its top-level output directory. NOAA global climate archives have a separate schema and are not accepted by this World Bank command.

## Interpret the results

Every row retains its reconstructed value string, original unit, country/economy identity, year, source observation flag and data-origin label. Original numeric spelling remains in the archived raw response; parsing can normalize equivalent JSON number spellings. No value is rounded, replaced, dropped or imputed by the audit.

| Status | Meaning |
| --- | --- |
| `available` | The source returned a finite number, including zero; this may be an estimate rather than a direct measurement. |
| `missing` | The source returned an explicit null. |
| `not_returned` | The country-year was requested but absent from the complete source response. |

A gap is a consecutive run of `missing` or `not_returned` years **within the requested window**. A run may contain both statuses; its counts and the full row ledger preserve the distinction. Runs are `leading` before the first available year, `internal` between available years, `trailing` after the last available year, or `all_unavailable` when the entire country's requested window has no available value. An available value with a review flag still anchors a gap. These labels say nothing about years outside the query or why a value is absent. Source coverage and reporting delays can create gaps without indicating an error.

The project applies these screening bounds to available values using exact decimal comparisons:

| Indicator | Minimum, inclusive | Maximum, inclusive |
| --- | ---: | ---: |
| GDP, constant 2015 US$ | 0 | No upper screening bound |
| Total population, people | 0 | No upper screening bound |
| Renewable share of total final energy consumption, percent | 0 | 100 |

These are **project review rules**, inferred from the quantities' definitions, not World Bank validation guarantees. A negative value receives `value_below_minimum`; a renewable share above 100 receives `value_above_maximum`. Values on the boundaries pass the range screen. Numeric zero, including a signed decimal zero, remains available and contributes to `zero_value_count`. A very small negative value is flagged without rounding it to zero. Passing a range screen does not establish scientific validity, historical comparability or suitability for a business decision.

Any nonempty source observation-flag string, including whitespace, adds `source_observation_flag`. The original string is preserved and its meaning is not decoded. A source flag is not automatically an error. Range and source flags can occur together: `flagged_row_count` counts that row once, while `range_flag_count` and `source_flag_count` describe separate checks. Missingness is summarized independently and is not itself a row review flag.

## Report contract

Schema version 1 has `artifact_type: bundle_quality`, tool/version, a `bundle` metadata overview, the applied `rule`, flag definitions, gap definition, `summary`, `countries`, `rows` and `limitations`.

- `summary` contains all status counts, zero and flagged-row counts, gap runs/years/positions, countries with gaps and countries with no available observations.
- `countries` contains each country's status/flag counts, first and last available years (null when none), and gap spans with start/end year, length, position and separate missing/omitted counts.
- `rows` contains the complete original reconstructed row fields plus a `flags` list, sorted by country code and year. An empty list means no review rule fired; it is not a certification.
- `bundle` retains the definition, provider, unit basis, source update label, original per-response retrieval dates, hashes and qualified licensing context used by [archive inspection](INSPECTION.md).

Text includes the same summary, country gaps, complete row ledger, rules, limitations and metadata. Country names appear in the country section; the common indicator identity and data origin appear in the bundle overview. Each ledger line identifies its country/year and gives its status, value, unit, source flag and review flags. Source-supplied text is displayed as escaped JSON literals, including Unicode and terminal control characters. This is plain text, not a web/HTML or spreadsheet export. The same compatible version and unchanged archive produce deterministic reports; no current audit timestamp is inserted.

For in-process Python use, call `eco_evidence.quality.quality_bundle(path)` to obtain the report dictionary and `render_quality_text(report)` to obtain a string. The CLI entry point owns the process's stdout stream, including its failed-stream cleanup.

The synthetic population example has one zero, one explicit null and one omitted year. Its 2020–2021 leading gap has length two and one of each unavailable status. It is invented software-test data. The included real renewable archive has 30 requested country-years: 18 available and 12 explicit nulls, with a two-year trailing gap in each of six countries. That describes this retained retrieval, not current source coverage. Its [attribution record](../examples/output-energy-study/ATTRIBUTION.md) identifies the original providers, retrievals and reuse terms.

## Validation, limits and source scope

The existing loader rejects structural duplicates, unknown identities, changed reviewed units, invalid pagination, nonfinite values, raw hash mismatches and incompatible queries before an audit can succeed. Such failures produce exit 2 with no partial report. A complete report returns 0 even when it contains review flags or gaps. Operating-system and argument errors also return 2. The quality command handles a closed or failed stdout stream without a second shutdown-flush error. Stdout remains a stream: an I/O failure can occur after some bytes have been delivered. The no-partial-report guarantee applies to validation and size checks before writing, not to interrupted output or failed devices.

The reconstructed metadata overview is limited to 1,000,000 bytes. Every report must fit a 5,000,000-byte JSON representation; text requests must additionally fit a 5,000,000-byte complete text rendering. An exceeded bound fails before output; details are never sampled or silently truncated. These are output-size bounds, not peak-memory guarantees. Source files retain the existing archive limits. Concurrent archive editing is unsupported.

No input `data.csv` or derived manifest count is trusted as evidence. The command does not authenticate that CSV, the origin label, or a manifest an attacker could replace together with its raw files. Retain a trusted archive and version history. The report records source dates and hashes but is not itself a self-contained replay archive. Shell redirection can overwrite a file before the command runs; never redirect into an input archive.

The range-rule review used the official World Bank definitions and units for [GDP](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/NY.GDP.MKTP.KD), [population](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/SP.POP.TOTL) and [renewable energy share](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/EG.FEC.RNEW.ZS), checked November 28, 2026. They describe constant-price output, a resident population estimate and a final-energy share respectively. The thresholds above are this project's explicit screening interpretation of those definitions.

No new observed dataset is distributed by this feature. The existing real study retains its dated indicator-specific attribution and license review. The [World Bank Dataset Terms](https://www.worldbank.org/ext/en/legal/terms-conditions/datasets) require attribution and identify possible third-party conditions. The offline audit does not check current permissions or authorize redistribution; a successful audit cannot replace source and license review. Use `inspect --baseline` to investigate changes between otherwise matching archives; this single-bundle audit does not infer revisions, causality, measurement accuracy or a universal quality ranking.
