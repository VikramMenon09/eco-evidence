# EcoEvidence roadmap

The aim is a research resource that makes economic and environmental comparisons reproducible and easy to audit. Build one meaningful increment at a time, using this ordering as a starting point rather than a quota.

## Foundation

- A small verified World Bank indicator registry with original units and source links.
- A command-line download and tidy export workflow with raw-response archives and provenance.
- Missing observations remain distinct from zero, and offline fixtures are clearly synthetic.
- Tests and an independently reviewed first release.

## Next research capabilities

1. **Comparable panels — implemented in 0.2.0.** [Offline panels](PANELS.md) join two or three archived indicators by country/economy code and year, retain unmatched queries and all missing-data distinctions, report coverage, and embed replayable source bundles. Follow-up extensions should address a demonstrated research need rather than expanding joins speculatively.
2. **Research-ready metadata — implemented in 0.3.0.** [Offline inspection](INSPECTION.md) exposes definitions, source organizations, qualified licensing context, original units and per-response retrieval timestamps. Same-query archive comparisons distinguish database update labels, metadata changes, numerical changes, missingness and representation changes. Indicator-specific reuse permission and historical semantic compatibility still require source review.
3. **Reproducible worked study — first example implemented in 0.4.0.** The [output and energy study](OUTPUT_ENERGY_STUDY.md) pairs derived constant-price GDP per person with renewable final-energy shares, preserving missingness and source archives. It demonstrates economic/environmental context with actual licensed data; it does not measure household income, emissions or causal effects. A later study can add directly measured environmental outcomes after source review.
4. **A second primary-data adapter — implemented in 0.5.0.** The [NOAA climate adapter](CLIMATE.md) archives annual global marine-surface CO₂ with exact mean/uncertainty tokens, source headers, retrieval lineage and offline replay. Its fixed, bounded text download needs no credentials or pagination. NOAA attribution and public-data terms accompany the real example. Global concentrations remain separate from World Bank country indicators; additional sources still need their own access, reuse, identifier and unit review.
5. **Data-quality reports — first audit implemented in 0.6.0.** The [offline quality report](QUALITY.md) preserves every World Bank query row, summarizes gap positions and status distinctions, and flags values outside explicit review bounds or carrying source observation flags. Text and JSON retain metadata and lineage. Structural duplicates, unit drift and hash failures remain validation errors; use the existing archive comparison for revisions. Further rules need a documented research use and source basis rather than a universal quality score.
6. **Business-relevant comparisons.** Add transparent sector or energy-cost context where licensed data supports it. Document boundaries and avoid turning country averages into company-specific estimates.
7. **Usable research interfaces.** Improve CLI help, examples, package installation, and notebooks. Add a browser interface only when it serves an established workflow.

## Choosing a portfolio session task

The [portfolio routine](PORTFOLIO.md) targets three useful contributions per day across EcoEvidence, Carbon Commons and Circular Economy Lab, with weekly time for new projects. Read open issues and recent commits, reproduce the next gap, and set a checkable acceptance criterion before coding. Prefer finishing an existing useful PR over starting another. Create a new issue only for a real problem or planned capability. A contribution need not be large in line count; it must make the research workflow meaningfully better.

Keep the repository coherent. A new repository is warranted only for a genuinely separate project, not to satisfy a daily activity count. If research, source access, review, or tests block publication, record the exact blocker and retain useful work for the next attempt.
