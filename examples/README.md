# Example evidence bundles

- `population-2020-2022/` is a real World Bank API retrieval for India and the United States, years 2020–2022. It contains six reported values, full raw responses and provenance. Use the recorded retrieval timestamp and original providers when citing it. Values may be revised by the source later.
- `synthetic-population/` is an invented software-test fixture, not country statistics. It intentionally returns zero for one year, `null` for another, and no row for a third. Every exported row and its provenance are marked `synthetic_fixture`; its source names and definitions explicitly identify the fixture.
- `synthetic-gdp/` is an invented GDP fixture for IND and USA, 2021–2023. It includes zero, an exact decimal, explicit null and omitted observations. Source definitions, observation flags and origin identify it as software-test data. Combined with `synthetic-population/`, it demonstrates unmatched country/year coverage and all four panel statuses; see the [panel guide](../docs/PANELS.md). None of its values is an observed country statistic.

Replay either bundle without network access using the same query and `--offline PATH`, with a new `--output` directory. The main README includes exact commands. Retain all files in a bundle together; hashes in provenance let you check each raw response and exported CSV against that manifest. The manifest and origin label are not source-authenticated; use a trusted bundle custodian and preserve repository history.

The real bundle retains World Bank and original-provider attribution and is subject to the upstream data terms. Synthetic fixtures are project-authored test material. Neither example is an economic model, causal estimate or forecast.
