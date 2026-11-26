# Output per person and renewable energy: a reproducible comparison

This worked example asks how **domestic output per person** and **renewable energy's share of final consumption** appear together in a small country sample. It demonstrates a research workflow: inspect definitions, archive actual observations, derive a transparent quantity, retain missingness and reproduce the result offline.

The six countries are Brazil, Canada, Germany, India, the United States and South Africa. They are an illustrative selection, not a random or representative sample. The requested window is 2019–2023. The data were retrieved on **November 26, 2026 UTC (November 25 Pacific)**; all three observation responses report the database update label `2026-11-11`. That label is not a measurement date or proof of an atomic snapshot.

## Reproduce the archived study

From the checkout, with Python 3.11 or newer:

```sh
python3 -m eco_evidence study \
  --gdp-bundle examples/output-energy-study/inputs/gdp \
  --population-bundle examples/output-energy-study/inputs/population \
  --renewables-bundle examples/output-energy-study/inputs/renewables \
  --output runs/output-energy-replay
```

This command is offline. The same compatible code and archived inputs reproduce byte-identical `study.csv` and `summary.json`; newly generated provenance has a new export timestamp and input-manifest lineage. The committed example contains re-exported input bundles with the original raw bytes and retrieval timestamps. Its `original_manifest_sha256` fields refer to the initial fetch manifests, which are not included; those original manifest hashes cannot be independently checked from this checkout. The output directory must not exist. The installed `eco-evidence study` command accepts the same arguments.

Outputs include all source values, statuses, units, country names and observation flags, the derived value and exclusion reasons, coverage by year, SHA-256 hashes and replayable input bundles. Original retrieval timestamps and raw bytes are preserved. `inspect --bundle examples/output-energy-study/inputs/renewables` exposes that series' archived definition and providers.

## What the archived observations show

GDP and population each have 30 available country-year values. Renewable shares have 18 available values and **12 explicit nulls**, covering all six countries in both 2022 and 2023. Accordingly, 2019, 2020 and 2021 each have six complete pairs; 2022 and 2023 each have zero. Those later rows remain in the study with a derived output-per-person value and `renewables_missing` as their pair-exclusion reason.

The general indicator metadata lists a reference period through 2022. That does not guarantee a reported value for these economies in the archived API response. **2021 is the latest complete comparison year in this selected extract**, not a claim about every country or the latest data available elsewhere.

The table below is calculated from the [archived study CSV](../examples/output-energy-study/study.csv). Output per person is rounded to two decimal places here for display; the CSV retains the declared 28-significant-digit calculation. Rows are ordered by country code.

| Country (2021) | Derived GDP per person, constant 2015 US$ | Renewable share of total final energy consumption, % |
| --- | ---: | ---: |
| Brazil | 8,799.23 | 46.5 |
| Canada | 44,639.24 | 23.8 |
| Germany | 44,011.02 | 17.6 |
| India | 1,965.31 | 34.9 |
| United States | 63,096.79 | 10.9 |
| South Africa | 5,750.91 | 9.7 |

The pairs illustrate why the quantities need separate interpretation. For example, Canada's and Germany's output-per-person figures are close in this table, while their renewable shares differ. That descriptive observation cannot identify a causal mechanism, the effect of policy, or what a particular business pays for energy. No correlation, regression, country ranking or hypothesis test is computed.

## Definitions and calculation

The numerator is [GDP in constant 2015 US dollars, `NY.GDP.MKTP.KD`](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/NY.GDP.MKTP.KD), divided by [midyear population, `SP.POP.TOTL`](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/SP.POP.TOTL). This is an EcoEvidence derivation from two archived series, not a download of an official GDP-per-capita series. It measures average domestic production in those units. It is neither household income nor a purchasing-power-parity measure; cross-country levels can be affected by valuation and compilation differences.

The paired series is [`EG.FEC.RNEW.ZS`](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/EG.FEC.RNEW.ZS). Its denominator is total **final energy consumption across all energy products**, including all final electricity and heat. Its numerator counts direct renewable use plus the renewable portions of final electricity and heat. It is not the renewable share of electricity generation. Renewable energy includes biomass; renewability alone does not establish sustainable production or low emissions. See the [UN SDG 7.2.1 methodology](https://unstats.un.org/sdgs/metadata/files/Metadata-07-02-01.pdf) for the concept boundary.

Business use is limited to exploratory country context and identifying follow-up questions. A firm's costs, electricity contract, facility emissions and customer purchasing power require different evidence. The code performs no currency conversion, price forecast, company estimate or policy evaluation.

`gdp_per_person = gdp_value / population_value` uses a fresh Decimal context: **28 significant digits**, `ROUND_HALF_EVEN`. This computational precision is not a claim of measurement accuracy. Source cells retain exact reconstructed decimal strings; original numeric spelling remains in the raw responses. Division can be rounded. The calculation is independent of the caller's ambient Decimal settings.

| Situation | Result |
| --- | --- |
| GDP and positive population are available | Derive GDP per person, even when renewables are missing |
| Reported GDP is zero and population is positive | Derive a real zero |
| GDP/population is explicitly null or not returned | Leave the derived value blank and retain the specific reason |
| Reported population is zero | Retain it; no division, with `population_zero` reason |
| Available GDP/population is negative, or renewable share is outside 0–100 | Reject the whole export with a diagnostic |
| Nonzero GDP/population or their quotient has an adjusted decimal exponent outside −100 through 100 | Reject as outside the supported calculation range |

Adjusted exponent means the exponent of the most significant digit; for example, `123.4` has adjusted exponent 2. Exponent limits are computation support limits, not economic plausibility thresholds. Overflow, underflow, subnormal and clamped results are rejected instead of silently saturating or becoming zero. Nothing is interpolated or filled from another year.

## Coverage and evidence contract

The command requires the three specified indicators with the **same countries and year window**, and the same data-origin label. Use `panel` for unmatched queries. It reconstructs every source from checksum-verified raw responses; previous input CSVs and derived manifest summaries are not trusted as data.

`study.csv` is sorted by country code and year. Each source has `ALIAS_value`, `ALIAS_status`, `ALIAS_unit`, `ALIAS_country_name` and `ALIAS_source_obs_status`. Derived columns are `gdp_per_person`, its unit, status (`derived` or `not_derived`) and reason. `pair_complete` is a `true`/`false` string; `pair_exclusion_reasons` is a semicolon-separated list. A complete pair needs both a derived GDP-per-person value and an available renewable share.

`summary.json` records row, derivation and complete-pair counts, original source-status counts, and a `by_year` list. Each year lists eligible country codes and every excluded country's reasons. Exclusion counts can overlap because one country-year can have multiple reasons. `provenance.json` records the formula, rounding, domain limits, source identities and metadata, original and embedded manifest hashes, and output hashes.

The study CSV is capped at 5 MB and each generated JSON metadata file at 1 MB. Over-limit outputs are rejected, not truncated. Inputs retain the existing archive validation and size limits. The exporter stages a complete directory and refuses existing destinations or a destination inside an input, including filesystem aliases. Normal failures remove staging data. Concurrent filesystem replacement is unsupported, and hard interruptions can leave a staging directory. Parent directories created before a late failure can remain.

Hashes establish consistency with a manifest, not upstream authenticity. Preserve source history and use trusted archives. A matching update label does not establish common measurement periods, full comparability or causal identification.

## Collect a different vintage

Use new directories for fresh downloads. Run the following command once for each alias `gdp`, `population` and `renewables`, replacing `ALIAS` in both places:

```sh
python3 -m eco_evidence fetch --indicator ALIAS \
  --countries BRA,CAN,DEU,IND,USA,ZAF --start-year 2019 --end-year 2023 \
  --refresh --output runs/new-ALIAS
```

Then pass the three new directories to `study`. To audit source revisions, compare each new indicator bundle with its archived counterpart using `inspect --bundle NEW --baseline OLD`. Fresh results may differ; do not overwrite the committed example or present a new retrieval as its historical snapshot.

## Attribution and reuse

The [example attribution record](../examples/output-energy-study/ATTRIBUTION.md) identifies World Bank and original providers, the exact series-level CC BY 4.0 listings, the applicable dataset terms, the IEA source and derived-work notice. The licensing review is dated November 25, 2026 Pacific. Its scope is these archived inputs and this derivation, not every IEA product or third-party dataset. The software license does not replace data licenses.
