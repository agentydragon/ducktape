"""Divergences the fuzzer found that the engines have not been made to agree on yet.

A burn-down, not an archive: an entry leaves this file when the two engines agree, taking its
case with it. Each pins what each engine answers today, so a change to either side fails here
and whoever made it decides which answer was meant — rather than the disagreement quietly
moving to a new number.

The fuzz targets also fail on everything recorded here, and deliberately: nothing below is
excused, canonicalized away, or generated around.

Both entries are the same question in two channels: what does a rollout report for the months
after it has frozen? Rust stops that rollout at the month it could not pay, so nothing later
happens in it at all; JAX keeps stepping and masks the arithmetic, so it still emits rows.
Neither is about money — the amounts agree wherever both engines report one — and each channel
could be answered on its own, which is why they are two entries and not one.
"""

from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.rust.differential.backend import run_jax, run_rust
from finance.augur.rust.differential.case import Case, scenario
from finance.augur.rust.differential.fixtures import cash_spend, checking, taxed
from finance.augur.sim.scenario import FixedAmount, InitialAccountBalance, InitialLot, PrivateEquityTenderPolicy

ACME = IssuerId("acme")

# One tax year: it closes at month 11 and is assessed at month 12.
TAX_YEAR_MONTHS = 12


def frozen_taxpayer_case(*, fail_month: int) -> Case:
    """A taxed agent whose one obligation is larger than everything they have."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("vendor", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=TAX_YEAR_MONTHS,
            scheduled_obligations=[
                cash_spend(
                    "unfundable", month=fail_month, agent_id="alice", to_agent_id="vendor", amount_due=Decimal(1)
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=1,
    )


@pytest.mark.parametrize(
    ("fail_month", "jax_assessments"),
    [(TAX_YEAR_MONTHS - 2, 0), (TAX_YEAR_MONTHS - 1, 1)],
    ids=["before the tax year closes", "in its closing month"],
)
def test_jax_assesses_a_tax_year_the_rollout_did_not_survive(fail_month: int, jax_assessments: int) -> None:
    """The year-end assessment lands the month after the year closes, and only JAX runs it.

    Nothing is owed either way, so this is not a disagreement about money: it is about whether
    a rollout that stopped in the year's closing month has a tax year at all. Rust never
    reaches December's assessment; JAX emits the row, at zero.

    Both months are pinned because what the disagreement turns on is the boundary — a rollout
    that froze a month earlier gets no assessment from either engine.
    """

    case = frozen_taxpayer_case(fail_month=fail_month)
    jax_result = run_jax(case)
    assert jax_result.rollout_status.get_column("failed_month").to_list() == [fail_month]
    assert jax_result.tax_liabilities.get_column("amount_owed_quanta").to_list() == [0] * jax_assessments
    assert run_rust(case).tax_liabilities.height == 0


def frozen_private_equity_case(*, freeze: bool) -> Case:
    """A private-equity holding whose issuer marks itself up every month after month 0.

    With `freeze`, an obligation the owner cannot fund stops the rollout at month 1, which is
    before the second and third marks the protocol still publishes.
    """

    kinds = [PrivateEquityEventKindCode.NONE] + [PrivateEquityEventKindCode.ADMIN_MARK_UPDATE] * 3
    snapshots = len(kinds)
    horizon_months = snapshots - 1
    return Case(
        scenario=scenario(
            [
                InitialAccountBalance(agent_id="pe_owner", account_id="checking", balance=Decimal(100)),
                InitialAccountBalance(agent_id="pe_owner", account_id="private", balance=Decimal(0)),
                InitialAccountBalance(agent_id="vendor", account_id="checking", balance=Decimal(0)),
            ],
            horizon_months=horizon_months,
            tax_profiles=[],
            scheduled_obligations=(
                [
                    cash_spend(
                        "unfundable", month=1, agent_id="pe_owner", to_agent_id="vendor", amount_due=Decimal(1_000)
                    )
                ]
                if freeze
                else []
            ),
            initial_lots=[
                InitialLot(
                    lot_id="pe-acme",
                    agent_id="pe_owner",
                    account_id="private",
                    asset=PrivateEquityAssetKey(issuer_id=ACME),
                    purchase_month_index=-12,
                    quantity=10.0,
                    cost_basis_per_unit=Decimal(10),
                )
            ],
            private_equity_tender_policies=[
                PrivateEquityTenderPolicy(
                    owner_agent_id="pe_owner",
                    proceeds_account_id="checking",
                    liquid_net_worth_floor=FixedAmount(amount=Decimal(0)),
                )
            ],
        ),
        rollout_count=1,
        private_equity=PrivateEquityBundle.from_issuer_arrays(
            ACME,
            mark_usd_per_unit=np.full((1, snapshots), 100.0),
            regime_code=np.full((1, snapshots), int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64),
            event_kind_code=np.asarray([[int(kind) for kind in kinds]], dtype=np.int64),
            sale_opportunity_active=np.zeros((1, snapshots), dtype=bool),
            sale_capacity_fraction=np.ones((1, snapshots)),
            eligible_fraction=np.ones((1, snapshots)),
            forced_sale_fraction=np.zeros((1, snapshots)),
            liquidity_blocked=np.zeros((1, snapshots), dtype=bool),
            forced_recovery_cashout_usd=np.zeros((1, snapshots)),
            company_valuation_usd=np.zeros((1, snapshots)),
            rollout_count=1,
            horizon_months=horizon_months,
        ),
    )


@pytest.mark.parametrize("freeze", [False, True], ids=["funded", "frozen"])
def test_jax_reports_private_equity_events_a_frozen_rollout_never_saw(freeze: bool) -> None:
    """A frozen rollout still records the issuer's protocol events in JAX and none in Rust.

    The marks are exogenous, so JAX reports them whether or not the rollout that would hold
    the position is still running; Rust's month loop has stopped, and its protocol pass is
    part of that loop. The funded case is the anchor: with the obligation payable both engines
    report the same two events, so the frozen one is the freeze and not the holding.
    """

    case = frozen_private_equity_case(freeze=freeze)
    marks = [(1, "admin_mark_update"), (2, "admin_mark_update")]
    jax_events = run_jax(case).events.private_equity_events
    assert jax_events.select(["month_index", "event_kind"]).rows() == marks
    assert run_rust(case).events.private_equity_events.select(["month_index", "event_kind"]).rows() == (
        [] if freeze else marks
    )


if __name__ == "__main__":
    pytest_bazel.main()
