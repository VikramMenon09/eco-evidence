# EcoEvidence roadmap

The aim is a research resource that makes economic and environmental comparisons reproducible and easy to audit. Build one meaningful increment at a time, using this ordering as a starting point rather than a quota.

## Foundation

- A small verified World Bank indicator registry with original units and source links.
- A command-line download and tidy export workflow with raw-response archives and provenance.
- Missing observations remain distinct from zero, and offline fixtures are clearly synthetic.
- Tests and an independently reviewed first release.

## Next research capabilities

1. **Comparable panels — implemented in 0.2.0.** [Offline panels](PANELS.md) join two or three archived indicators by country/economy code and year, retain unmatched queries and all missing-data distinctions, report coverage, and embed replayable source bundles. Follow-up extensions should address a demonstrated research need rather than expanding joins speculatively.
2. **Research-ready metadata.** Expose indicator definitions, source organization, licensing/terms, last-update fields, and revision/vintage changes. Distinguish retrieval dates from source update dates.
3. **Reproducible worked study.** Create a documented example relating income, energy structure, and environmental outcomes. Use actual sourced data, show missingness, and distinguish descriptive associations from causation.
4. **A second primary-data adapter.** Evaluate OECD, UN, IEA open data, or another credible source for access and redistribution rights. Implement only after resolving units, identifiers, pagination, rate limits, and coverage.
5. **Data-quality reports.** Detect unexpected gaps, duplicates, impossible values, inconsistent units, and revisions. Export a human-readable audit alongside machine-readable records.
6. **Business-relevant comparisons.** Add transparent sector or energy-cost context where licensed data supports it. Document boundaries and avoid turning country averages into company-specific estimates.
7. **Usable research interfaces.** Improve CLI help, examples, package installation, and notebooks. Add a browser interface only when it serves an established workflow.

## Choosing the daily task

Read open issues and recent commits, reproduce the next gap, and set a checkable acceptance criterion before coding. Prefer finishing an existing useful PR over starting another. Create a new issue only for a real problem or planned capability. A contribution need not be large in line count; it must make the research workflow meaningfully better.

Keep the repository coherent. A new repository is warranted only for a genuinely separate project, not to satisfy a daily activity count. If research, source access, review, or tests block publication, record the exact blocker and retain useful work for the next attempt.
