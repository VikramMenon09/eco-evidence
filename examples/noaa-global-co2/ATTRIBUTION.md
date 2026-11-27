# NOAA source attribution and reuse

Data provided by NOAA Global Monitoring Laboratory, Boulder, Colorado, USA, [Carbon Cycle and Greenhouse Gases group](https://gml.noaa.gov/ccgg/).

Dataset citation: Lan, X., Tans, P. and K. W. Thoning, *Trends in globally-averaged CO₂ determined from NOAA Global Monitoring Laboratory measurements*, [DOI: 10.15138/9N0H-ZH07](https://doi.org/10.15138/9N0H-ZH07). The [annual global source file](https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_gl.txt) supplies the records in this archive. Its creation label is `Thu Nov 19 14:14:29 2026`; that literal label does not specify a timezone. The exact UTC retrieval timestamp, URL and SHA-256 are recorded in `provenance.json`. Retrieval occurred on November 27, 2026 UTC (November 26 Pacific).

The full original raw file contains 47 annual records for 1979–2025, with NOAA's use notice, contact information, uncertainty explanation and revision caveat. The tidy CSV selects 2020–2026, preserving six source means and uncertainties exactly and adding one explicit `not_returned` row for the absent 2026 annual record. No observed value was invented, interpolated, rounded or converted. This selection, reshaping, provenance and missing-year annotation are EcoEvidence transformations, not a new official NOAA product. The DOI and web-page version do not uniquely identify these archived bytes; retain the raw file and its retrieval history.

## Source-specific terms review

Reviewed November 26, 2026 Pacific. NOAA's [GML Disclaimer and Terms of Reference](https://gml.noaa.gov/about/disclaimer.html) identifies government-server information as public domain unless specifically annotated otherwise and permits public use with acknowledgement, no implied NOAA endorsement, and no altered information presented as official government material. The NOAA data incorporated here are not subject to copyright protection under that stated government-data policy. This notice concerns this annual NOAA data file; it makes no claim over separately annotated third-party material, photographs or logos. The original file carries a request for relevant scientific citation and no separate restrictive license annotation.

The data remain attributed to NOAA and the named dataset authors. The repository's software license does not establish ownership of NOAA information. EcoEvidence is independent and is not endorsed by or affiliated with NOAA. No provider images or logos are included.

## Interpretation and reproduction

This is global marine-surface atmospheric CO₂ concentration in dry-air ppm, with the annual product's reported uncertainty. It is not a national emissions inventory or a company's footprint. NOAA derives the background estimate from a sampling network; source preprocessing and possible revisions remain part of its interpretation. Reported uncertainty is not converted into a confidence interval. See the [product description](https://gml.noaa.gov/ccgg/trends/global.html), [global-mean methodology](https://gml.noaa.gov/ccgg/about/global_means.html) and [EcoEvidence reproduction guide](../../docs/CLIMATE.md).

Source hashes establish consistency with the supplied manifest, not independent source authentication. `ATTRIBUTION.md` explains reuse and transformation; it is not a source observation or a file authenticated by the manifest's data hashes.
