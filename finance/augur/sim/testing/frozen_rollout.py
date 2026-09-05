"""A rollout that runs out of cash stops there, and reports nothing later.

An engine that cannot leave a vectorized scan early keeps stepping a frozen rollout under a
mask. Those masked steps are not months that went wrong; they are months that did not happen,
and the read model should not surface them — an assessment for a tax year the rollout did not
survive, exogenous marks it was never around to see.

Stated against a `SimulationResult` rather than one engine's output, because "a frozen rollout
reports nothing later" is a claim about what a simulator is.

**What is deliberately not here:** whether a mark published *during* the failure month itself is
reported. The engines disagree about that and the question is open — JAX reports the whole
failure month, Rust stops at the phase that could not pay — so it is pinned per engine in
`rust/differential/known_divergence_test.py` instead. Every case below is about months strictly
after the freeze, which is settled.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
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
from finance.augur.sim.testing.case import Case
from finance.augur.sim.testing.simulation_result import Backend

TAX_YEAR_MONTHS = 12
# The year closes at month 11 and is assessed at month 12: freezing in the closing month is
# the boundary, where the rollout reaches the end of the year but never the assessment.
FAIL_MONTH = TAX_YEAR_MONTHS - 1


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
                month=FAIL_MONTH,
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


_ACME = IssuerId("acme")
_PE_MARK_MONTHS = 3


def _private_equity_case(*, freeze: bool) -> tuple[Scenario, PrivateEquityBundle]:
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
    return scenario, bundle


def frozen_case(*, horizon_months: int) -> Case:
    return Case(scenario=_case(horizon_months=horizon_months), rollout_count=1)


def private_equity_case(*, freeze: bool) -> Case:
    scenario, bundle = _private_equity_case(freeze=freeze)
    return Case(scenario=scenario, rollout_count=1, private_equity=bundle)


class FrozenRolloutAcceptance:
    """Inherit and supply `backend`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def backend(self) -> Backend:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def test_the_rollout_really_does_freeze_where_the_case_says(self, backend: Backend) -> None:
        """The premise: without it, "reports nothing later" could hold for the wrong reason."""

        status = backend(frozen_case(horizon_months=TAX_YEAR_MONTHS)).rollout_status
        assert status.get_column("failed_month").to_list() == [FAIL_MONTH]

    def test_the_issuer_publishes_every_month_when_the_owner_can_pay(self, backend: Backend) -> None:
        """The anchor: without it, an empty frozen result could mean the marks never existed."""

        events = backend(private_equity_case(freeze=False)).events.private_equity_events
        # The last kind sits at the snapshot past the horizon, so two of the three land in range.
        assert events.get_column("month_index").to_list() == [1, 2]

    def test_nothing_is_marked_after_the_month_the_rollout_froze(self, backend: Backend) -> None:
        """The issuer keeps marking; the rollout that would have held the position does not.

        These come off the compiled plan rather than the run, so nothing about the freeze
        reaches them on its own — they would be reported for months the rollout was no longer
        around to see unless the read model drops them. The rollout freezes at month 1, so
        month 2 is the one this rules out; whether month 1 itself is reported is the open
        question named in the module docstring, and is not asserted here.
        """

        events = backend(private_equity_case(freeze=True)).events.private_equity_events
        assert [month for month in events.get_column("month_index").to_list() if month > 1] == []

    def test_a_tax_year_the_rollout_did_not_survive_is_not_assessed(self, backend: Backend) -> None:
        """The year closes at month 11 and is assessed at month 12; this rollout froze at 11.

        Not a zero assessment — no assessment. A liability that was never created reports
        nothing, which is what an engine that simply never reaches the month does.
        """

        assert backend(frozen_case(horizon_months=TAX_YEAR_MONTHS + 1)).tax_liabilities.is_empty()
