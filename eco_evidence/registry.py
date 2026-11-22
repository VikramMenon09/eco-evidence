"""Curated units checked against official WDI indicator metadata on 2026-11-22.

The API's unit field is blank for these indicators. Units below are derived from
the official indicator name/definition, not invented from data magnitudes.
"""

from dataclasses import dataclass

API_BASE = "https://api.worldbank.org/v2"
CATALOG_URL = "https://datacatalog.worldbank.org/search/dataset/0037712/world-development-indicators"
TERMS_URL = "https://www.worldbank.org/en/about/legal/terms-of-use-for-datasets"
CHECKED_ON = "2026-11-22"


@dataclass(frozen=True)
class Indicator:
    alias: str
    code: str
    name: str
    unit: str

    @property
    def metadata_url(self):
        return f"{API_BASE}/indicator/{self.code}?format=json"


INDICATORS = (
    Indicator("gdp", "NY.GDP.MKTP.KD", "GDP (constant 2015 US$)", "constant 2015 US$"),
    Indicator("population", "SP.POP.TOTL", "Population, total", "people"),
    Indicator("renewables", "EG.FEC.RNEW.ZS",
              "Renewable energy consumption (% of total final energy consumption)",
              "% of total final energy consumption"),
)


def get_indicator(value):
    for indicator in INDICATORS:
        if value in (indicator.alias, indicator.code):
            return indicator
    raise ValueError("Unknown indicator. Run 'python -m eco_evidence indicators' for supported codes.")
