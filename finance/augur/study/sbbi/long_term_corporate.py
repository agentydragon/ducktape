"""Reproduce Ibbotson's long-term corporate bond series from Moody's Aaa yields.

Roger G. Ibbotson and Rex A. Sinquefield, *Stocks, Bonds, Bills, and Inflation: Historical
Returns (1926-1987)*, CFA Research Foundation, 1989. This is the series the Trinity study's
bonds are, so this reproduction is what licenses `study/trinity`'s bond sleeve — and, more
broadly, it is the only external check augur has on turning an observed yield into a total
return over a real century.

**The construction is not ours to choose.** The monograph documents it exactly
("Description of the Basic Series", p. 21):

> Monthly capital appreciation returns for 1926-68 were calculated from yields assuming (at
> the beginning of each monthly holding period) a 20-year maturity, a bond price equal to par,
> and a coupon equal to the yield. [...] The monthly income return is assumed to be
> one-twelfth of the coupon.

which is `bond_fund.constant_maturity_fund_paths` at maturity 20, clause for clause. From 1969
the series splices in the Salomon Brothers Long-Term High-Grade Corporate Bond Index —
"nearly all Aaa- and Aa-rated bonds" — where the maturity is an empirical question rather than
a definition, and the fit below answers it: 20 to 25, not something short.

**The one difference, and it is a known defect.** Ibbotson priced S&P's High-Grade Corporate
Composite yields for 1926-45 and Salomon's own monthly yields for 1946-68; neither is public,
so this prices Moody's Aaa. Aaa is a narrower, higher-grade universe than an Aaa-and-Aa
composite, so its coupon runs light: the reproduction compounds 4.70% a year against the
published 4.80% over 1927-1987. Nothing public sits between Moody's Aaa and Baa across this
span, so closing it needs a yield series we do not have rather than a change here.

(Read against `study/trinity`, which quotes a 0.2-point figure: that one is the median
difference in REAL return across 30-year windows, and this is the nominal compound over the
whole record. Both are measured; neither is the other.)
"""

from __future__ import annotations

import asyncio
import logging
import math
import tempfile
from pathlib import Path

import numpy as np

from finance.augur.model.bond_fund import constant_maturity_fund_paths
from finance.augur.model.historical_windows import load_macro_history
from finance.augur.study.trinity.evidence_snapshot import snapshot_evidence
from finance.evidence import sources

logger = logging.getLogger(__name__)

MONTHS_PER_YEAR = 12

BOND_MATURITY_YEARS = 20.0
"""The monograph's own number for 1926-68, and what the 1969- fit independently recovers."""

# `load_macro_history` assembles one aligned record and needs all six, equity included, even
# though only the Aaa column is read here.
EVIDENCE = (
    sources.FRENCH_FACTORS,
    sources.FRED_LTGOVTBD,
    sources.FRED_GS10,
    sources.FRED_CPI_NSA,
    sources.FRED_AAA,
    sources.FRED_BAA,
)

PUBLISHED_ANNUAL_RETURN = {
    1926: +0.0740,
    1927: +0.0740,
    1928: +0.0280,
    1929: +0.0330,
    1930: +0.0800,
    1931: -0.0190,
    1932: +0.1080,
    1933: +0.1040,
    1934: +0.1380,
    1935: +0.0960,
    1936: +0.0670,
    1937: +0.0270,
    1938: +0.0610,
    1939: +0.0400,
    1940: +0.0340,
    1941: +0.0270,
    1942: +0.0265,
    1943: +0.0226,
    1944: +0.0340,
    1945: +0.0362,
    1946: +0.0138,
    1947: -0.0230,
    1948: +0.0410,
    1949: +0.0330,
    1950: +0.0210,
    1951: -0.0270,
    1952: +0.0350,
    1953: +0.0340,
    1954: +0.0540,
    1955: +0.0144,
    1956: -0.0680,
    1957: +0.0870,
    1958: -0.0230,
    1959: -0.0100,
    1960: +0.0910,
    1961: +0.0480,
    1962: +0.0790,
    1963: +0.0220,
    1964: +0.0480,
    1965: -0.0050,
    1966: -0.0163,
    1967: -0.0500,
    1968: +0.0260,
    1969: -0.0810,
    1970: +0.1840,
    1971: +0.1100,
    1972: +0.0730,
    1973: +0.0110,
    1974: -0.0310,
    1975: +0.1460,
    1976: +0.1860,
    1977: +0.0170,
    1978: -0.0010,
    1979: -0.0420,
    1980: -0.0260,
    1981: -0.0100,
    1982: +0.4380,
    1983: +0.0470,
    1984: +0.1640,
    1985: +0.3090,
    1986: +0.2175,
    1987: -0.0299,
}
"""Total return on long-term corporate bonds, from SBBI Exhibit A-3.

The exhibit tabulates compound annual rates for every holding period 1926-1987, so the annual
returns are its diagonal; the off-diagonal cells recover the years whose rows the layout wrapped.
Published to a tenth of a point, which is what bounds the tolerances below. An external
contract, so these are literals."""

PUBLISHED_SUMMARY_PERCENT = {"geometric": 4.9, "arithmetic": 5.2, "standard_deviation": 8.5}
"""The monograph's own summary statistics for the same 1926-1987 series (p. 72). Independent of
the exhibit above, so agreement with them is what says the diagonal was read correctly."""


def reproduce(evidence_dir: Path, *, maturity_years: float = BOND_MATURITY_YEARS) -> dict[int, float]:
    """The fund's December-to-December total return per calendar year, priced off Moody's Aaa."""

    history = load_macro_history(evidence_dir)
    price, distribution = constant_maturity_fund_paths(
        history.corporate_aaa_yield[None, :], maturity_years=maturity_years, initial_price_usd=100.0
    )
    # Units compound by reinvesting the distribution, so the total-return index is units x price.
    index = np.cumprod(1.0 + distribution[0] / price[0]) * price[0]
    decembers = {month.year: i for i, month in enumerate(history.months) if month.month == 12}
    return {
        year: float(index[decembers[year]] / index[decembers[year - 1]] - 1.0)
        for year in sorted(decembers)
        if year - 1 in decembers
    }


def compared_years(reproduced: dict[int, float]) -> list[int]:
    return sorted(set(reproduced) & set(PUBLISHED_ANNUAL_RETURN))


def tracking_error(reproduced: dict[int, float]) -> float:
    """Root-mean-square annual difference from the published series, in percentage points."""

    years = compared_years(reproduced)
    return 100.0 * float(np.sqrt(np.mean([(reproduced[y] - PUBLISHED_ANNUAL_RETURN[y]) ** 2 for y in years])))


def compound_percent(returns: dict[int, float], years: list[int]) -> float:
    # math.pow rather than `**`: typeshed widens float ** float to Any (a negative base with a
    # fractional exponent is complex), and here a negative cumulative growth would be a bug
    # worth raising on rather than a value worth returning.
    growth = float(np.prod([1.0 + returns[y] for y in years]))
    return 100.0 * (math.pow(growth, 1.0 / len(years)) - 1.0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        asyncio.run(snapshot_evidence(directory, EVIDENCE))
        print(f"{'maturity':>9}{'RMSE (pts)':>12}{'compounded':>12}{'published':>11}")
        for maturity in (5.0, 10.0, 15.0, 20.0, 25.0, 30.0):
            reproduced = reproduce(directory, maturity_years=maturity)
            years = compared_years(reproduced)
            print(
                f"{maturity:>9.0f}{tracking_error(reproduced):>12.2f}"
                f"{compound_percent(reproduced, years):>11.2f}%"
                f"{compound_percent(PUBLISHED_ANNUAL_RETURN, years):>10.2f}%"
            )


if __name__ == "__main__":
    main()
