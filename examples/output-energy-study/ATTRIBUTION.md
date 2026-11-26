# Source attribution and data licenses

This directory is a real World Bank API extract and an EcoEvidence derivation for BRA, CAN, DEU, IND, USA and ZAF, 2019–2023. It contains no invented observations. Raw bytes and exact retrieval timestamps are in `inputs/*/raw/` and their `provenance.json` files. Retrieval occurred on November 26, 2026 UTC (November 25 Pacific); the observation responses report database update `2026-11-11`.

## Sources

- **The World Bank: World Development Indicators: national statistical organizations and central banks, OECD national accounts files, and World Bank staff estimates.** GDP, `NY.GDP.MKTP.KD`, constant 2015 US$. [Series definition and license](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/NY.GDP.MKTP.KD).
- **The World Bank: World Development Indicators: UN Population Division, national statistical offices, Eurostat, and UN Statistics Division.** Midyear population, `SP.POP.TOTL`. [Series definition and license](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/SP.POP.TOTL).
- **The World Bank: World Development Indicators: International Energy Agency, Energy Statistics Data Browser.** Renewable share of total final energy consumption, `EG.FEC.RNEW.ZS`. [Series definition and license](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/EG.FEC.RNEW.ZS). The archived WDI metadata records its provider access date as March 25, 2025; this is distinct from our API retrieval date.

Full original provider strings, indicator definitions, units, source IDs, query URLs and observation flags are retained in the embedded bundles. EcoEvidence's reshaped CSVs are transformations of these responses. GDP per person is a new calculation from the archived GDP and population values, rounded to 28 significant digits with half-even rounding. It is not an independently sourced observation. Coverage summaries and the documentation's display rounding are also EcoEvidence transformations.

## License review: November 25, 2026 Pacific

Each of the three exact DataBank series pages linked above explicitly lists **CC BY-4.0**. The [WDI catalog](https://datacatalog.worldbank.org/search/dataset/0037712/world-development-indicators) also lists CC BY 4.0. Reuse remains subject to the [World Bank Dataset Terms](https://www.worldbank.org/ext/en/legal/terms-conditions/datasets), including attribution and any applicable third-party conditions. Retain these acknowledgments and the terms link when sharing the extract. The [CC BY 4.0 license](https://creativecommons.org/licenses/by/4.0/) requires credit, a license link and an indication of changes.

The precise IEA product named by WDI, [Energy Statistics Data Browser](https://www.iea.org/data-and-statistics/data-tools/energy-statistics-data-browser), independently lists CC BY 4.0. Its page was last updated September 28, 2026 when checked. Attribution: **IEA, Energy Statistics Data Browser (accessed by the World Bank March 25, 2025), CC BY 4.0**, available at the linked product page. The later product-page update does not establish which edition supplied the archived WDI rows. Follow the [IEA notice for CC-licensed material](https://www.iea.org/terms/creative-commons-cc-licenses), including original-provider rights and no endorsement or use of logos.

Required IEA derived-work notice:

> This is a work derived by EcoEvidence from IEA material and EcoEvidence is solely liable and responsible for this derived work. The derived work is not endorsed by the IEA or its Member countries in any manner.

The World Bank and other named providers likewise do not endorse EcoEvidence or its interpretation. No provider logos or report figures are reproduced. This review concerns the three named WDI series and the bounded extract here. It does not extend to other IEA products, photographs or the separately licensed Tracking SDG7 report. The repository's software license does not relicense upstream data.

## Reproduction

Use the [worked-study guide](../../docs/OUTPUT_ENERGY_STUDY.md) to reproduce `study.csv` and `summary.json` from the embedded archives, entirely offline. Source hashes establish internal consistency with the supplied manifests, not independent source authentication. Preserve the directory and its repository history together. `ATTRIBUTION.md` documents reuse and interpretation; it is not an additional source observation or a file authenticated by the study's output hashes.
