"""Run the historical replay beside the fitted structural-macro model and compare.

The two samplers answer the same question from opposite directions. `structural_macro`'s
shipped fit has unlimited synthetic 30-year paths drawn from a VAR(1) fitted on 1955-2026; the
replay has the actual past, with its fat tails and its equity/inflation coupling, and only ~3
INDEPENDENT 30-year windows. Neither is trustworthy alone. Where they disagree is the finding,
so this prints them side by side rather than picking one.

Deliberately no spending, no taxes, no policy: a monthly-rebalanced buy-and-hold on the raw
exogenous paths. Everything the portfolio machinery adds is common to both arms, so leaving it
out isolates what actually differs — the economy each model believes in.

Runs on a BuildBuddy runner, which has the network this repo's sandboxes do not:

    bbr run //finance/augur/x:replay_vs_fitted_bin
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import numpy as np

from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from finance.augur.model.historical_windows import HistoricalWindowsProviderConfig
from finance.augur.model.series import InflationKey, LevelSeriesKey, SecurityDistributionKey, SecurityKey
from finance.augur.model.structural_macro import EquitySpec, InstrumentSpec, StructuralMacroProviderConfig
from finance.evidence import sources
from finance.scraper import http_fetch

logger = logging.getLogger(__name__)

HORIZON_MONTHS = 360
EQUITY = EquitySpec(symbol="VOO", initial_price_usd=520.0)
BONDS = InstrumentSpec(symbol="CMF", duration_years=5.5, initial_price_usd=56.0, spread=-0.012)
# The record's own start decides how many windows exist; the fitted arm is given the same count
# so the two percentile tables are read off the same number of paths.
EQUITY_WEIGHTS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
PERCENTILES = (5, 25, 50, 75, 95)

# What `load_macro_history` reads. Fetched here rather than read from an augur-evidence checkout
# because this session has no read credential for that repo — same source specs, same bytes.
NEEDED = (sources.FRENCH_FACTORS, sources.FRED_LTGOVTBD, sources.FRED_GS10, sources.FRED_CPI_NSA)


async def _materialize_evidence(directory: Path) -> None:
    """Fetch each source into `directory` under the filename its loader reads back."""

    async def one(source: sources.EvidenceSource) -> None:
        (directory / source.output_filename).write_bytes(
            await http_fetch.http_get(source.upstream_url, source.user_agent)
        )
        logger.info("fetched %s", source.provenance_label)

    await asyncio.gather(*(one(source) for source in NEEDED))


def _total_return_index(price: np.ndarray, distribution: np.ndarray) -> np.ndarray:
    """Value of one unit bought at month 0 with every distribution reinvested at that month's price.

    Units compound by `distribution / price`; the index is units times price. This is what makes a
    bond sleeve comparable to equity at all — a fund's price alone omits the coupon, which over
    30 years is most of its return.
    """
    units = np.cumprod(1.0 + distribution / price, axis=1)
    return np.asarray(units * price)


def _real_terminal_multiples(bundle: SampledExogenousBundle, weight: float, *, rollouts: int) -> np.ndarray:
    """Terminal real wealth multiple per rollout for a monthly-rebalanced `weight`/rest mix."""

    def matrix(key: LevelSeriesKey) -> np.ndarray:
        return bundle.level_matrix(key, rollout_count=rollouts, horizon_months=HORIZON_MONTHS)

    equity = matrix(SecurityKey(symbol=EQUITY.symbol))
    bond_index = _total_return_index(
        matrix(SecurityKey(symbol=BONDS.symbol)), matrix(SecurityDistributionKey(symbol=BONDS.symbol))
    )
    inflation = matrix(InflationKey())
    # Monthly rebalancing is a weighted sum of the two sleeves' monthly returns, which is exactly
    # what holding the target weights through each month means.
    blended = weight * (equity[:, 1:] / equity[:, :-1]) + (1.0 - weight) * (bond_index[:, 1:] / bond_index[:, :-1])
    nominal = np.prod(blended, axis=1)
    return np.asarray(nominal / (inflation[:, -1] / inflation[:, 0]))


def _report(name: str, bundle: SampledExogenousBundle, rollouts: int) -> None:
    print(f"\n{name}  ({rollouts} rollouts, {HORIZON_MONTHS // 12}y)")
    print("  equity%  " + "  ".join(f"p{p:<5}" for p in PERCENTILES) + "   P[real loss]")
    for weight in EQUITY_WEIGHTS:
        multiples = _real_terminal_multiples(bundle, weight, rollouts=rollouts)
        cells = "  ".join(f"{np.percentile(multiples, p):6.2f}" for p in PERCENTILES)
        print(f"  {weight * 100:5.0f}%   {cells}      {float(np.mean(multiples < 1.0)):.1%}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        asyncio.run(_materialize_evidence(directory))

        replay = HistoricalWindowsProviderConfig(
            evidence_dir=directory, equity=EQUITY, instruments=(BONDS,)
        ).realize_model()
        rollouts = replay.window_count(HORIZON_MONTHS)
        print(
            f"record: {replay.history.months} months, {rollouts} overlapping {HORIZON_MONTHS // 12}y windows "
            f"(~{replay.independent_window_estimate(HORIZON_MONTHS):.1f} independent)"
        )

        request = ExogenousSamplingRequest(
            horizon_months=HORIZON_MONTHS,
            rollout_seeds=tuple(range(rollouts)),
            required_asset_prices=frozenset({SecurityKey(symbol=s) for s in (EQUITY.symbol, BONDS.symbol)}),
            required_security_distributions=frozenset({SecurityDistributionKey(symbol=BONDS.symbol)}),
            required_index_series=frozenset({InflationKey()}),
        )

        fitted = StructuralMacroProviderConfig(equity=EQUITY, instruments=(BONDS,)).realize_model()
        _report("fitted structural macro (VAR(1), synthetic draws)", fitted.sample(request), rollouts)
        _report("historical replay (overlapping windows)", replay.sample(request), rollouts)


if __name__ == "__main__":
    main()
