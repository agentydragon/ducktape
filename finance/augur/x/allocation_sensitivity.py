"""Does the stock/bond split actually move a FIRE outcome, and can the evidence resolve it?

Plan 0 of the allocation program: measure the sensitivity BEFORE building anything. Every
improvement on the roadmap — a joint equity/rates fit (#5487), a longer fitting record (#5509),
a resampling sampler (#5510), a tiered spending ladder (#5480) — is worth its cost only if the
answer it sharpens is one the decision is actually sensitive to. This prints the surface and
its error bars so that is a measurement rather than an assumption.

Three things it is built to expose:

- **Whether the two samplers rank allocations the same way.** They bracket the truth from
  opposite sides: the replay has real fat tails and real equity/inflation coupling with about
  three independent observations, the fitted VAR has unlimited draws, Gaussian tails, and
  `rate_beta` fitted to zero — so no bond/equity coupling at all (`model/SPEC.md` gap 2, which
  says outright that a question turning on that correlation is not answered there). If they
  agree on the ranking, the missing correlation does not decide this. If they disagree, it does.
- **Whether any of the differences clear the noise.** A survival rate read off ~3 independent
  30-year windows has a standard error of roughly ten points. An allocation that "wins" by less
  than that has not won.
- **What a FIRE horizon costs in evidence.** Retiring early means 40-50 years, not 30, and the
  record holds fewer independent windows the longer the horizon — so the case with the most at
  stake is the one the replay can say least about, and the one that leans hardest on the fitted
  model's missing coupling.

Deliberately no taxes, no policy, no cash band: a monthly-rebalanced two-sleeve portfolio on
the raw exogenous paths, with a CPI-indexed monthly withdrawal. Everything the portfolio
machinery adds is common to both arms, so leaving it out isolates what differs — the economy
each model believes in. That also makes every level here a lower bound on what the real plan
faces; read the SHAPE of the surface, not the levels.

    bbr run //finance/augur/x:allocation_sensitivity_bin
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from finance.augur.model.historical_windows import MACRO_HISTORY_SOURCES, HistoricalWindowsProviderConfig
from finance.augur.model.series import InflationKey, LevelSeriesKey, SecurityDistributionKey, SecurityKey
from finance.augur.model.structural_macro import EquitySpec, InstrumentSpec, StructuralMacroProviderConfig
from finance.augur.study.trinity.evidence_snapshot import snapshot_evidence

logger = logging.getLogger(__name__)

MONTHS_PER_YEAR = 12

EQUITY = EquitySpec(symbol="VOO", initial_price_usd=520.0)

# Two bond sleeves differing only in yield, because the gap between them turns out to move the
# answer more than the choice of sampler does. 5.5 years keeps both inside the curve's
# interpolated range, so SPEC gap 8's flat long end does not contaminate this — duration is a
# separate axis and a longer question.
#
# The muni sleeve is what `replay_vs_fitted` prices. Its -120bp is the pre-tax discount munis
# trade at BECAUSE their coupons are exempt, and this harness models no tax, so that arm pays
# the discount without being credited the exemption. Read it as a lower bound on the bond end,
# not as a muni portfolio.
BOND_SLEEVES = (
    ("taxable", InstrumentSpec(symbol="CMF", maturity_years=5.5, initial_price_usd=56.0)),
    ("muni, -120bp", InstrumentSpec(symbol="CMF", maturity_years=5.5, initial_price_usd=56.0, spread=-0.012)),
)

EQUITY_WEIGHTS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
PAYOUT_YEARS = (30, 40, 50)
WITHDRAWAL_RATES = (0.03, 0.035, 0.04)


@dataclass(frozen=True)
class Paths:
    """One sampler's exogenous draws, reduced to what a two-sleeve withdrawal needs."""

    equity_return: np.ndarray
    bond_return: np.ndarray
    inflation: np.ndarray
    # How many of the rollouts are independent observations. Unlimited draws from a fitted
    # process are all independent; overlapping windows cut from one record are not, and that
    # is the whole reason both arms are here.
    independent_rollouts: float


def _total_return_index(price: np.ndarray, distribution: np.ndarray) -> np.ndarray:
    """Units compound by `distribution / price`, so the index is units times price."""

    return np.asarray(np.cumprod(1.0 + distribution / price, axis=1) * price)


def _paths(
    bundle: SampledExogenousBundle, *, bonds: InstrumentSpec, rollouts: int, horizon_months: int, independent: float
) -> Paths:
    def matrix(key: LevelSeriesKey) -> np.ndarray:
        return bundle.level_matrix(key, rollout_count=rollouts, horizon_months=horizon_months)

    equity = matrix(SecurityKey(symbol=EQUITY.symbol))
    bond_index = _total_return_index(
        matrix(SecurityKey(symbol=bonds.symbol)), matrix(SecurityDistributionKey(symbol=bonds.symbol))
    )
    return Paths(
        equity_return=equity[:, 1:] / equity[:, :-1],
        bond_return=bond_index[:, 1:] / bond_index[:, :-1],
        inflation=matrix(InflationKey()),
        independent_rollouts=independent,
    )


def survival_and_terminal(paths: Paths, *, equity_weight: float, withdrawal_rate: float) -> tuple[float, float]:
    """Fraction of rollouts funding every withdrawal, and the median terminal real multiple.

    Monthly rebalancing to the target is a weighted sum of the sleeves' monthly returns, which
    is what holding the weights through the month means. The withdrawal is a twelfth of the
    annual rate, indexed to CPI, taken before the month's return is earned on the remainder.
    """

    blended = equity_weight * paths.equity_return + (1.0 - equity_weight) * paths.bond_return
    monthly = withdrawal_rate / MONTHS_PER_YEAR
    price_ratio = paths.inflation / paths.inflation[:, :1]

    balance = np.ones(blended.shape[0])
    alive = np.ones(blended.shape[0], dtype=bool)
    for month in range(blended.shape[1]):
        due = monthly * price_ratio[:, month]
        alive &= due <= balance
        balance = np.maximum(balance - due, 0.0) * blended[:, month]

    real = balance / price_ratio[:, -1]
    return float(np.mean(alive)), float(np.median(real[alive])) if alive.any() else 0.0


def standard_error_points(rate: float, independent_rollouts: float) -> float:
    """Sampling error of a survival rate, in percentage points, at the sampler's EFFECTIVE n.

    Overlapping windows do not each carry a fresh observation, so dividing by the window count
    would understate this by an order of magnitude. That gap is the point of printing it.
    """

    return 100.0 * float(np.sqrt(max(rate * (1.0 - rate), 0.0) / independent_rollouts))


def _sample(evidence_dir: Path, *, payout_years: int, bonds: InstrumentSpec) -> dict[str, Paths]:
    horizon_months = payout_years * MONTHS_PER_YEAR
    replay = HistoricalWindowsProviderConfig(
        evidence_dir=evidence_dir, equity=EQUITY, instruments=(bonds,)
    ).realize_model()
    rollouts = replay.window_count(horizon_months)
    independent = replay.independent_window_estimate(horizon_months)
    request = ExogenousSamplingRequest(
        horizon_months=horizon_months,
        rollout_seeds=tuple(range(rollouts)),
        required_asset_prices=frozenset({SecurityKey(symbol=s) for s in (EQUITY.symbol, bonds.symbol)}),
        required_security_distributions=frozenset({SecurityDistributionKey(symbol=bonds.symbol)}),
        required_index_series=frozenset({InflationKey()}),
    )
    fitted = StructuralMacroProviderConfig(equity=EQUITY, instruments=(bonds,)).realize_model()
    logger.info("%dy: %d overlapping windows, ~%.1f independent", payout_years, rollouts, independent)
    return {
        "replay": _paths(
            replay.sample(request),
            bonds=bonds,
            rollouts=rollouts,
            horizon_months=horizon_months,
            independent=independent,
        ),
        # Synthetic draws are independent by construction, so the same count carries far more
        # information here — which is exactly the trade the two arms exist to show.
        "fitted": _paths(
            fitted.sample(request),
            bonds=bonds,
            rollouts=rollouts,
            horizon_months=horizon_months,
            independent=float(rollouts),
        ),
    }


def _report(payout_years: int, sleeve_name: str, replay: Paths, fitted: Paths) -> None:
    print(
        f"\n{'=' * 96}\n{payout_years}-year payout, {sleeve_name} bond sleeve — "
        f"{replay.equity_return.shape[0]} windows, ~{replay.independent_rollouts:.1f} independent"
    )
    for rate in WITHDRAWAL_RATES:
        print(f"\n  {rate:.1%} withdrawal, CPI-indexed monthly")
        print(f"  {'equity':>7}{'replay survival':>26}{'fitted':>10}{'median real mult':>26}")
        rows = []
        for weight in EQUITY_WEIGHTS:
            replay_cell = survival_and_terminal(replay, equity_weight=weight, withdrawal_rate=rate)
            fitted_cell = survival_and_terminal(fitted, equity_weight=weight, withdrawal_rate=rate)
            rows.append((weight, replay_cell, fitted_cell))
            error = standard_error_points(replay_cell[0], replay.independent_rollouts)
            print(
                f"  {weight:>6.0%}{100 * replay_cell[0]:>18.0f}% +/-{error:<4.0f}"
                f"{100 * fitted_cell[0]:>8.0f}%{replay_cell[1]:>16.1f}x{fitted_cell[1]:>9.1f}x"
            )

        best_replay = max(rows, key=lambda row: row[1][0])
        best_fitted = max(rows, key=lambda row: row[2][0])
        # Two different questions, and only the second is the one being asked. Whether the
        # samplers pick the same winner, and whether ANY winner is separable from the field once
        # the replay's own sampling error is admitted. A "best" inside the noise is not a choice.
        agree = "AGREE" if best_replay[0] == best_fitted[0] else "DISAGREE"
        runner_up = max((row for row in rows if row[0] != best_replay[0]), key=lambda row: row[1][0])
        margin = 100 * (best_replay[1][0] - runner_up[1][0])
        noise = standard_error_points(best_replay[1][0], replay.independent_rollouts)
        print(
            f"  best: replay {best_replay[0]:.0%} equity, fitted {best_fitted[0]:.0%}  ({agree})"
            f"   it beats the runner-up by {margin:.0f} pts against +/-{noise:.0f} of noise"
            f"  -> {'separable' if margin > 2 * noise else 'NOT separable'}"
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        asyncio.run(snapshot_evidence(directory, MACRO_HISTORY_SOURCES))

        for payout_years in PAYOUT_YEARS:
            for sleeve_name, bonds in BOND_SLEEVES:
                arms = _sample(directory, payout_years=payout_years, bonds=bonds)
                _report(payout_years, sleeve_name, arms["replay"], arms["fitted"])


if __name__ == "__main__":
    main()
