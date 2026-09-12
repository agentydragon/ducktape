"""Independent cash/unit controls and the documented offline study entry point."""

import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey, LevelSeriesKey, SecurityDistributionKey, SecurityKey
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_to_quanta
from finance.augur.sim.results import Finished, RejectedAction, Rollout
from finance.augur.study.trinity.replay import (
    BONDS,
    EQUITY,
    HORIZON_MONTHS,
    INITIAL_PORTFOLIO,
    QUANTUM,
    compose,
    drive,
    opening_lots,
    situation,
    sleeve_targets,
)
from util.bazel.runfiles import get_required_path, own_repo_rlocation


def control(
    *,
    equity_share: float,
    quantity: float,
    annual: Decimal,
    horizon: int,
    blocks: list[tuple[LevelSeriesKey, np.ndarray]],
    cost_basis: Decimal = INITIAL_PORTFOLIO,
) -> Rollout:
    """One window on stipulated levels: the cell's single sleeve at `quantity` units, drawn down by `annual`."""
    case = situation(
        ExternalSeriesContext.from_level_blocks(blocks, rollout_count=1, horizon_months=horizon),
        rollout_count=1,
        horizon_months=horizon,
    )
    lot = opening_lots(equity_share)[0]
    lot = replace(
        lot,
        units=int(quantity_to_quanta(quantity, scale=lot.quantity_scale)),
        basis=int(currency_amount_to_quanta(cost_basis, quantum=QUANTUM)),
    )
    world = compose(case, 0, lots=(lot,), annual_withdrawal=annual)
    return drive({0: world}, targets=sleeve_targets(equity_share), capture="forensic")[0]


def test_indexed_claims_use_original_base_not_previous_rounded_withdrawal() -> None:
    cpi = np.full((1, 26), 3.0)
    cpi[:, 12:24] = 4
    cpi[:, 24:] = 5
    result = control(
        equity_share=1,
        quantity=1,
        annual=Decimal("0.01"),
        horizon=25,
        blocks=[(SecurityKey(symbol=EQUITY), np.full((1, 26), 100.0)), (InflationKey(), cpi)],
    )
    payments = result.summary.payments
    assert [(row.month, row.receipt.amount_paid) for row in payments] == [(0, 1), (12, 1), (24, 2)]
    assert result.stop is None


def test_coupons_precede_claims_surplus_stays_cash_and_final_snapshot_does_not_pay() -> None:
    result = control(
        equity_share=0,
        quantity=2,
        annual=Decimal(4),
        horizon=2,
        blocks=[
            (SecurityKey(symbol=BONDS), np.array([[100.0, 120.0, 110.0]])),
            (SecurityDistributionKey(symbol=BONDS), np.array([[5.0, 2.0, 999.0]])),
            (InflationKey(), np.ones((1, 3))),
        ],
    )
    assert result.stop is None
    assert result.summary.cash[0].values == [0, 600, 1000]
    assert result.summary.public_holdings[0].values == [20_000, 24_000, 22_000]
    financial = result.trace
    assert financial is not None
    assert financial.events.lot_dispositions.is_empty()
    assert [row.action.kind for row in financial.receipts] == ["PayClaim"]
    assert financial.books[-1].lots[0].units_remaining == 2_000_000


def test_nondivisible_sale_uses_quantity_ceiling_and_canonical_basis() -> None:
    result = control(
        equity_share=1,
        quantity=1,
        annual=Decimal(1),
        horizon=1,
        blocks=[(SecurityKey(symbol=EQUITY), np.full((1, 2), 3.0)), (InflationKey(), np.ones((1, 2)))],
        cost_basis=Decimal(1),
    )
    assert result.stop is None
    financial = result.trace
    assert financial is not None
    assert financial.events.lot_dispositions.select("proceeds_quanta", "cost_basis_consumed_quanta").row(0) == (100, 33)
    assert financial.events.lot_dispositions.get_column("realized_gain_quanta").to_list() == [67]
    lot = financial.books[-1].lots[0]
    assert (lot.units_remaining, lot.basis_remaining) == (666_666, 67)
    assert result.summary.cash[0].values == [0, 0]


@pytest.mark.parametrize(("portfolio_dollars", "success"), [(29, False), (30, True), (31, True)])
def test_final_exact_depletion_is_success_but_an_unpaid_final_withdrawal_is_not(
    portfolio_dollars: int, success: bool
) -> None:
    result = control(
        equity_share=1,
        quantity=portfolio_dollars / 100,
        annual=Decimal(1),
        horizon=HORIZON_MONTHS,
        blocks=[
            (SecurityKey(symbol=EQUITY), np.full((1, HORIZON_MONTHS + 1), 100.0)),
            (InflationKey(), np.ones((1, HORIZON_MONTHS + 1))),
        ],
    )
    assert (result.stop is None) == success
    payments = result.summary.payments
    assert [(row.month, row.receipt.amount_paid) for row in payments] == [
        (year * 12, 100 if year < portfolio_dollars else 0) for year in range(30)
    ]
    summary = result.summary
    assert summary.ending_mark_month == (360 if success else 348)
    assert summary.public_holdings[0].values[-1] == max(0, portfolio_dollars - 30) * 100
    assert summary.cash[0].values[-1] == 0
    assert summary.tax_accruals == []
    assert summary.tax_payments == []
    if not success:
        assert len(summary.cash[0].values) == 350
        assert result.stop == RejectedAction(month=348, action_index=0)


@pytest.mark.parametrize("share", [0.0, 0.5, 1.0])
def test_offline_cli_replays_selected_original_windows(tmp_path: Path, share: float) -> None:
    output = tmp_path / "study"
    subprocess.run(
        [
            get_required_path(own_repo_rlocation("finance/augur/study/trinity/replay_bin")),
            "--synthetic",
            "--equity-share",
            str(share),
            "--withdrawal-rate",
            "0.04",
            "--output-dir",
            output,
            "--trace-rollout",
            "2",
            "--trace-rollout",
            "0",
        ],
        check=True,
    )
    study = json.loads((output / "study.json").read_text())
    assert study["source"] == "synthetic placeholder history"
    assert study["window_starts"] == [f"1930-0{month}-01" for month in range(1, 5)]
    outcomes = Finished.model_validate_json((output / "outcomes.json").read_text()).rollouts
    traces = Finished.model_validate_json((output / "traces.json").read_text()).rollouts
    assert [row.rollout_id for row in outcomes] == [0, 1, 2, 3]
    assert [row.rollout_id for row in traces] == [2, 0]
    assert study["success_rate"] == sum(row.stop is None for row in outcomes) / 4
    for trace in traces:
        assert outcomes[trace.rollout_id].summary == trace.summary
        assert trace.trace is not None
        assert {row.action.kind for row in trace.trace.receipts} <= {"Sell", "PayClaim"}


if __name__ == "__main__":
    pytest_bazel.main()
