"""Monthly macro series for the pm_reifier backtests, read from the augur evidence checkout.

Reads the scraped exogenous evidence at `AUGUR_EVIDENCE_DIR` (the git-synced augur-evidence
dir, same mechanism as the app) instead of live-fetching from FRED/Yahoo — the daily scraper
already maintains these series, so the backtests don't duplicate that fetch.
"""

from __future__ import annotations

from finance.augur.fit.evidence_data import read_monthly_levels
from finance.augur.ingest import evidence_sources as es

# pm_reifier wire id -> the scraped EvidenceSource that backs it. (rent:sf_ca now tracks the SF
# rent CPI rather than the old national proxy; sp500 reads FRED's S&P 500 index.)
WIRE_SOURCES: dict[str, es.EvidenceSource] = {
    "inflation": es.FRED_CPI,
    "sp500": es.FRED_SP500,
    "crypto:BTC": es.YAHOO_BTC,
    "home_value:sf_ca": es.FRED_SFXRSA,
    "rent:sf_ca": es.FRED_SF_RENT_CPI,
}


def monthly_levels_by_wire() -> dict[str, dict[str, float]]:
    """wire -> {YYYY-MM: raw level}, last observation per calendar month."""
    return {
        wire: {level.month.strftime("%Y-%m"): level.value for level in read_monthly_levels(source)}
        for wire, source in WIRE_SOURCES.items()
    }
