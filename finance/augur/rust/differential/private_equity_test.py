"""Rust/JAX differential coverage for the typed private-equity tender protocol.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.

from decimal import Decimal
from typing import Any

import numpy as np
import polars as pl
import pytest_bazel
from jaxtyping import Float64, Int64

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.rust.differential.backend import assert_backends_agree
from finance.augur.sim.scenario import FixedAmount, InitialLot, PrivateEquityTenderPolicy
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking, taxed
from finance.augur.sim.testing.simulation_result import SimulationResult

ACME = PrivateEquityAssetKey(issuer_id=IssuerId("acme"))
HORIZON_MONTHS = 3


def _acme_lots() -> list[InitialLot]:
    """Two lots, one held past a year and one not, so a tender's holding period is decided."""

    return [
        InitialLot(
            lot_id="acme_lot_a",
            agent_id="alice",
            account_id="checking",
            asset=ACME,
            purchase_month_index=-36,
            quantity=40.0,
            cost_basis_per_unit=Decimal(10),
        ),
        InitialLot(
            lot_id="acme_lot_b",
            agent_id="alice",
            account_id="checking",
            asset=ACME,
            purchase_month_index=-12,
            quantity=60.0,
            cost_basis_per_unit=Decimal(20),
        ),
    ]


def _bundle(
    *,
    rollout_count: int,
    horizon_months: int,
    mark: Decimal,
    regime: Int64[np.ndarray, " rollout snapshot"] | None = None,
    event_kind: Int64[np.ndarray, " rollout snapshot"] | None = None,
    sale_capacity: Float64[np.ndarray, " rollout snapshot"] | None = None,
    forced_sale: Float64[np.ndarray, " rollout snapshot"] | None = None,
    liquidity_blocked: np.ndarray | None = None,
    forced_recovery: Float64[np.ndarray, " rollout snapshot"] | None = None,
) -> PrivateEquityBundle:
    """The issuer's ten channels, defaulted to a quiet path the overrides punch holes in."""

    shape = (rollout_count, horizon_months + 1)
    kinds = np.full(shape, int(PrivateEquityEventKindCode.NONE)) if event_kind is None else event_kind
    return PrivateEquityBundle.from_issuer_arrays(
        ACME.issuer_id,
        mark_usd_per_unit=np.full(shape, float(mark)),
        regime_code=np.full(shape, int(PrivateEquityRegimeCode.PRIVATE_OPERATING)) if regime is None else regime,
        event_kind_code=kinds,
        # A tender opportunity is the TENDER event kind; the bundle refuses any producer that
        # desyncs the two, so it is derived rather than stated twice.
        sale_opportunity_active=kinds == int(PrivateEquityEventKindCode.TENDER),
        sale_capacity_fraction=np.ones(shape) if sale_capacity is None else sale_capacity,
        eligible_fraction=np.ones(shape),
        forced_sale_fraction=np.zeros(shape) if forced_sale is None else forced_sale,
        liquidity_blocked=np.zeros(shape, dtype=np.bool_) if liquidity_blocked is None else liquidity_blocked,
        forced_recovery_cashout_usd=np.zeros(shape) if forced_recovery is None else forced_recovery,
        company_valuation_usd=np.zeros(shape),
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )


def private_equity_case(*, opening_cash: Decimal = Decimal(0), tendering: bool = True) -> Case:
    """Four rollouts, each taking one branch of the tender protocol.

    Rollout 0 gets a capacity-limited tender then a blocked one, rollout 1 a regime change,
    rollout 2 a forced sale, and rollout 3 a forced recovery cashout.
    """

    shape = (4, HORIZON_MONTHS + 1)
    regime = np.full(shape, int(PrivateEquityRegimeCode.PRIVATE_OPERATING))
    regime[1, 1] = int(PrivateEquityRegimeCode.PUBLIC_MARKET)
    event_kind = np.full(shape, int(PrivateEquityEventKindCode.NONE))
    event_kind[0, 1] = event_kind[0, 2] = int(PrivateEquityEventKindCode.TENDER)
    event_kind[1, 1] = int(PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN)
    event_kind[2, 1] = int(PrivateEquityEventKindCode.LEGAL_IMPAIRMENT)
    event_kind[3, 1] = int(PrivateEquityEventKindCode.FORCED_RECOVERY)
    capacity = np.ones(shape)
    capacity[0, 1] = 0.25
    blocked = np.zeros(shape, dtype=np.bool_)
    blocked[0, 2] = True
    forced_sale = np.zeros(shape)
    forced_sale[2, 1] = 0.3
    recovery = np.zeros(shape)
    recovery[3, 1] = 100.0
    return Case(
        scenario=scenario(
            checking(("alice", opening_cash)),
            horizon_months=HORIZON_MONTHS,
            tax_profiles=[],
            initial_lots=_acme_lots(),
            private_equity_tender_policies=(
                [
                    PrivateEquityTenderPolicy(
                        owner_agent_id="alice",
                        proceeds_account_id="checking",
                        liquid_net_worth_floor=FixedAmount(amount=Decimal(5_000)),
                    )
                ]
                if tendering
                else []
            ),
        ),
        rollout_count=4,
        private_equity=_bundle(
            rollout_count=4,
            horizon_months=HORIZON_MONTHS,
            mark=Decimal(100),
            regime=regime,
            event_kind=event_kind,
            sale_capacity=capacity,
            forced_sale=forced_sale,
            liquidity_blocked=blocked,
            forced_recovery=recovery,
        ),
    )


def private_equity_tax_case() -> Case:
    """One tender of a long-held lot, and the year-end tax it produces."""

    horizon_months = 12
    event_kind = np.full((1, horizon_months + 1), int(PrivateEquityEventKindCode.NONE))
    event_kind[0, 1] = int(PrivateEquityEventKindCode.TENDER)
    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=horizon_months,
            initial_lots=_acme_lots(),
            private_equity_tender_policies=[
                PrivateEquityTenderPolicy(
                    owner_agent_id="alice",
                    proceeds_account_id="checking",
                    liquid_net_worth_floor=FixedAmount(amount=Decimal(100_000)),
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=1,
        private_equity=_bundle(
            rollout_count=1, horizon_months=horizon_months, mark=Decimal(1_000), event_kind=event_kind
        ),
    )


def _final_cash(result: SimulationResult) -> dict[int, Any]:
    return {
        row["rollout_index"]: row["balance_quanta"]
        for row in result.cash.filter(pl.col("month_index") == HORIZON_MONTHS).to_dicts()
    }


def test_backends_agree_on_tender_sales_and_opportunities() -> None:
    result = assert_backends_agree(private_equity_case())

    assert _final_cash(result) == {0: 250_000, 1: 500_000, 2: 300_000, 3: 10_000}
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_that_an_issuer_without_an_owner_policy_never_tenders() -> None:
    result = assert_backends_agree(private_equity_case(tendering=False))

    assert result.events.lot_dispositions.is_empty()
    assert set(result.events.private_equity_opportunities.get_column("outcome")) == {"no_policy"}
    assert _final_cash(result) == dict.fromkeys(range(4), 0)


def test_backends_agree_that_a_satisfied_floor_suppresses_voluntary_sales() -> None:
    result = assert_backends_agree(private_equity_case(opening_cash=Decimal(6_000)))

    causes = result.events.lot_dispositions.get_column("cause_id")
    assert not any(cause.startswith(("pe_tender_", "pe_public_market_")) for cause in causes)
    assert _final_cash(result) == {0: 600_000, 1: 600_000, 2: 900_000, 3: 610_000}


def test_backends_agree_on_the_tax_facts_a_tender_disposition_produces() -> None:
    result = assert_backends_agree(private_equity_tax_case())
    breakdown = result.events.tax_breakdowns.filter(pl.col("rollout_index") == 0)

    # A tender sale of a lot held past a year is long-term, and nothing else realizes.
    assert breakdown.get_column("stcg_quanta").to_list() == [0]
    assert breakdown.get_column("ltcg_quanta").to_list() == [9_840_000]
    # There is no ordinary income here, so the standard deduction goes unused against it and
    # shelters that much of the gain instead: taxable income is 9_840_000 - 1_460_000, of
    # which 4_702_500 sits in the 0% slice and the rest is rated at 15%.
    assert breakdown.get_column("capital_gain_tax_quanta").to_list() == [551_625]


if __name__ == "__main__":
    pytest_bazel.main()
