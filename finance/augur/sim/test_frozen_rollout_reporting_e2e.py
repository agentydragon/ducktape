"""A rollout that runs out of cash stops there, and reports nothing later.

The engine cannot leave a vectorized scan early, so it keeps stepping a frozen rollout under
a mask. Those masked steps are not months that went wrong; they are months that did not
happen, and the read model should not surface them — an assessment for a tax year the rollout
did not survive, exogenous marks it was never around to see.

The cases below are the two shapes that rule takes, because events and state are not the same
claim: an event after the freeze never happened, while a liability already assessed stays on
the books and its later months legitimately read zero.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest_bazel

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.scenario import (
    Agent,
    FixedAmount,
    InitialAccountBalance,
    InitialLot,
    ObligationType,
    PrivateEquityTenderPolicy,
    Scenario,
    ScheduledObligation,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate, simulate_with_external_series
from finance.augur.sim.testing.state_helpers import rollout_status, tax_liabilities

_TAX_YEAR_MONTHS = 12
# The year closes at month 11 and is assessed at month 12: freezing in the closing month is
# the boundary, where the rollout reaches the end of the year but never the assessment.
_FAIL_MONTH = _TAX_YEAR_MONTHS - 1


def _case(*, horizon_months: int) -> Scenario:
    """A taxed agent whose one obligation is larger than everything they have."""

    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="vendor"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=Decimal(0))
            for agent_id in ("alice", "vendor", "irs")
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=_FAIL_MONTH,
                obligation_id="unfundable",
                obligation_type=ObligationType.CASH_SPEND,
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="vendor",
                to_account_id="checking",
                amount_due=FixedAmount(amount=Decimal(1)),
            )
        ],
        tax_profiles=[TaxProfile(agent_id="alice", jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs")],
        horizon_months=horizon_months,
    )


def _run(*, horizon_months: int) -> SimulationRun:
    return simulate(_case(horizon_months=horizon_months), rollout_count=1, locations={})


def test_the_rollout_really_does_freeze_where_the_case_says() -> None:
    """The premise: without it, "reports nothing later" could hold for the wrong reason."""

    status = rollout_status(_run(horizon_months=_TAX_YEAR_MONTHS))
    assert status.get_column("failed_month").to_list() == [_FAIL_MONTH]


_ACME = IssuerId("acme")
_PE_MARK_MONTHS = 3


def _private_equity_case(*, freeze: bool) -> tuple[Scenario, ExternalSeriesContext]:
    """A holding whose issuer marks itself up every month, and an owner who may go broke.

    The marks are exogenous: they are decoded from the compiled plan, not from what the run
    produced, so they are the one event frame a frozen rollout can still report. That is
    exactly why this case is here and the plain one above is not enough.
    """

    kinds = [PrivateEquityEventKindCode.NONE] + [PrivateEquityEventKindCode.ADMIN_MARK_UPDATE] * _PE_MARK_MONTHS
    snapshots = len(kinds)
    horizon_months = snapshots - 1
    scenario = Scenario(
        agents=[Agent(agent_id="pe_owner"), Agent(agent_id="vendor")],
        initial_cash=[
            InitialAccountBalance(agent_id="pe_owner", account_id="checking", balance=Decimal(100)),
            InitialAccountBalance(agent_id="pe_owner", account_id="private", balance=Decimal(0)),
            InitialAccountBalance(agent_id="vendor", account_id="checking", balance=Decimal(0)),
        ],
        initial_lots=[
            InitialLot(
                lot_id="pe-acme",
                agent_id="pe_owner",
                account_id="private",
                asset=PrivateEquityAssetKey(issuer_id=_ACME),
                purchase_month_index=-12,
                quantity=10.0,
                cost_basis_per_unit=Decimal(10),
            )
        ],
        scheduled_obligations=(
            [
                ScheduledObligation(
                    month=1,
                    obligation_id="unfundable",
                    obligation_type=ObligationType.CASH_SPEND,
                    agent_id="pe_owner",
                    from_account_id="checking",
                    to_agent_id="vendor",
                    to_account_id="checking",
                    amount_due=FixedAmount(amount=Decimal(1_000)),
                )
            ]
            if freeze
            else []
        ),
        private_equity_tender_policies=[
            PrivateEquityTenderPolicy(
                owner_agent_id="pe_owner",
                proceeds_account_id="checking",
                liquid_net_worth_floor=FixedAmount(amount=Decimal(0)),
            )
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )
    bundle = PrivateEquityBundle.from_issuer_arrays(
        _ACME,
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
    )
    context = ExternalSeriesContext.from_level_blocks(
        [], rollout_count=1, horizon_months=horizon_months, private_equity=bundle
    )
    return scenario, context


def _private_equity_mark_months(*, freeze: bool) -> list[int]:
    scenario, context = _private_equity_case(freeze=freeze)
    run = simulate_with_external_series(scenario, rollout_count=1, external_series=context, locations={})
    months: list[int] = run.events_log.private_equity_events.get_column("month_index").to_list()
    return months


def test_the_issuer_publishes_every_month_when_the_owner_can_pay() -> None:
    """The anchor: without it, an empty frozen result could mean the marks never existed."""

    # The last kind sits at the snapshot past the horizon, so two of the three land in range.
    assert _private_equity_mark_months(freeze=False) == [1, 2]


def test_no_exogenous_mark_is_reported_after_the_month_the_rollout_froze() -> None:
    """The issuer keeps marking; the rollout that would have held the position does not.

    These come off the compiled plan rather than the run, so nothing about the freeze reaches
    them on its own -- they are reported for months the rollout was no longer around to see
    unless the read model drops them.
    """

    assert _private_equity_mark_months(freeze=True) == [1]


def test_a_tax_year_the_rollout_did_not_survive_is_not_assessed() -> None:
    """The year closes at month 11 and is assessed at month 12; this rollout froze at month 11.

    Not a zero assessment — no assessment. A liability that was never created reports nothing,
    which is what an engine that simply never reaches the month does.
    """

    assert tax_liabilities(_run(horizon_months=_TAX_YEAR_MONTHS + 1)).is_empty()


if __name__ == "__main__":
    pytest_bazel.main()
