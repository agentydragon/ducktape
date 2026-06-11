"""Static spec of augur's public exogenous evidence sources.

Single source of truth shared by the git scraper (`fetch`) and the runtime
loader (`fit/evidence_data`): which public series exist, where each is fetched
from, and the local filename `augur/fit/evidence_data.py` reads it back as.
Deliberately free of numpy/pandas/JAX so the scraper image stays slim and
decoupled from the simulator.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

_CONTENT_TYPES = {"csv": "text/csv", "json": "application/json"}

# Per-kind User-Agent: Yahoo demands a browser UA; FRED rejects "Mozilla/*"
# (anti-scraping) but serves a curl UA; Zillow's static CSV host is indifferent.
_USER_AGENTS = {"fred": "curl/8.0", "yahoo": "Mozilla/5.0", "zillow": "curl/8.0"}


class EvidenceKind(StrEnum):
    FRED = "fred"
    YAHOO = "yahoo"
    ZILLOW = "zillow"


@dataclass(frozen=True)
class EvidenceSource:
    kind: EvidenceKind
    # Series identity within its kind: FRED series id / Yahoo symbol / Zillow dataset stem.
    series_id: str
    upstream_url: str
    # Filename the scraper writes into the evidence repo and the loader reads back by basename
    # (also the vendored fallback name). test_evidence_sources checks the spec matches the
    # checked-in data files.
    output_filename: str

    @property
    def user_agent(self) -> str:
        return _USER_AGENTS[self.kind]

    @property
    def extension(self) -> str:
        return self.output_filename.rsplit(".", 1)[1]

    @property
    def content_type(self) -> str:
        return _CONTENT_TYPES[self.extension]

    @property
    def provenance_label(self) -> str:
        """Stable logical id for the series (e.g. `fred:CPIAUCSL`), recorded as the evidence source."""
        return f"{self.kind}:{self.series_id}"


def _fred(series_id: str, output_filename: str) -> EvidenceSource:
    return EvidenceSource(
        kind=EvidenceKind.FRED,
        series_id=series_id,
        upstream_url=f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
        output_filename=output_filename,
    )


def _yahoo(symbol: str, output_filename: str) -> EvidenceSource:
    # range=max&interval=1d matches the daily snapshots the loader expects (the SPY
    # variant requires >=1000 daily samples); the full response carries the same
    # chart.result[0].{timestamp,indicators.adjclose} arrays the loader reads.
    return EvidenceSource(
        kind=EvidenceKind.YAHOO,
        series_id=symbol,
        upstream_url=f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=max&interval=1d",
        output_filename=output_filename,
    )


def _zillow(dataset_stem: str, output_filename: str) -> EvidenceSource:
    return EvidenceSource(
        kind=EvidenceKind.ZILLOW,
        series_id=dataset_stem,
        upstream_url=f"https://files.zillowstatic.com/research/public_csvs/{dataset_stem}.csv",
        output_filename=output_filename,
    )


# The complete public evidence set augur fits + calibrates against, each a named
# EvidenceSource — the canonical identity used everywhere (loaders, provenance).
# Stored raw (untrimmed upstream bytes); deployment-specific trimming (the Zillow city
# filter) stays at read time in evidence_data.py, so this spec stays deployment-agnostic.
FRED_CPI = _fred("CPIAUCSL", "fred_cpi_us.csv")
FRED_SP500 = _fred("SP500", "fred_sp500.csv")
FRED_MORTGAGE30 = _fred("MORTGAGE30US", "fred_mortgage30.csv")
FRED_SFXRSA = _fred("SFXRSA", "fred_sfxrsa.csv")
FRED_FHFA_SF = _fred("ATNHPIUS41884Q", "fred_fhfa_sf_oakland_berkeley.csv")
# SF rent CPI: only the FRED-only degraded path uses it; production rent is Zillow ZORI.
FRED_SF_RENT_CPI = _fred("CUURA422SEHA", "fred_sf_rent_cpi.csv")
YAHOO_SPY = _yahoo("SPY", "yahoo_spy_chart_adjusted.json")
YAHOO_BTC = _yahoo("BTC-USD", "yahoo_btc_chart_adjusted.json")
YAHOO_ETH = _yahoo("ETH-USD", "yahoo_eth_chart_adjusted.json")
ZILLOW_ZHVI = _zillow(
    "zhvi/City_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month",
    "zillow_city_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
)
ZILLOW_ZORI = _zillow("zori/City_zori_uc_sfrcondomfr_sm_sa_month", "zillow_city_zori_uc_sfrcondomfr_sm_sa_month.csv")

EVIDENCE_SOURCES: tuple[EvidenceSource, ...] = (
    FRED_CPI,
    FRED_SP500,
    FRED_MORTGAGE30,
    FRED_SFXRSA,
    FRED_FHFA_SF,
    FRED_SF_RENT_CPI,
    YAHOO_SPY,
    YAHOO_BTC,
    YAHOO_ETH,
    ZILLOW_ZHVI,
    ZILLOW_ZORI,
)
