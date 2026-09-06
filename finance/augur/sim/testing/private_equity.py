"""What a private-equity position does when the issuer offers, forces, or blocks a sale.

A PE holding is the one asset whose sales the holder does not initiate. The issuer's protocol
decides whether a sale is possible at all — a tender window, a public-market regime, a forced
redemption — and the holder's policy decides only whether to take it, by whether liquid net
worth has fallen through a floor. So every case here fixes the protocol channels and reads
back what the position and the cash did, which is the only way the two halves can be told
apart: a sale that should have been capacity-limited and one that should never have been
offered both end with units still held.

Stated against the channels every engine answers in, because none of this is a property of how
an engine buffers a PE lot.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import numpy as np
import numpy.typing as npt
import polars as pl
import pytest

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.scenario import (
    FixedAmount,
    InitialAccountBalance,
    InitialLot,
    PrivateEquityTenderPolicy,
    RecurringObligation,
    Scenario,
    TaxProfile,
)
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking, taxed
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

ISSUER = "acme"
ASSET_ID = "private_equity:acme"
LOT_ID = "acme_lot_a"
ACME = PrivateEquityAssetKey(issuer_id=IssuerId(ISSUER))

FloatMatrix = npt.NDArray[np.float64]
CodeMatrix = npt.NDArray[np.int64]


def floats(*, horizon_months: int, rollouts: int = 1, value: float) -> FloatMatrix:
    return np.full((rollouts, horizon_months + 1), value, dtype=np.float64)


def codes(*, horizon_months: int, rollouts: int = 1, value: int) -> CodeMatrix:
    return np.full((rollouts, horizon_months + 1), value, dtype=np.int64)


def in_month(*, horizon_months: int, month: int, value: float, default: float = 0.0) -> FloatMatrix:
    """A float channel holding `default` except at one month."""

    matrix = floats(horizon_months=horizon_months, value=default)
    matrix[:, month] = value
    return matrix


def code_in_month(*, horizon_months: int, month: int, value: int, default: int) -> CodeMatrix:
    """A code channel holding `default` except at one month."""

    matrix = codes(horizon_months=horizon_months, value=default)
    matrix[:, month] = value
    return matrix


def protocol(
    *,
    initial_mark_usd: float,
    horizon_months: int,
    tender_month: int | None = None,
    tender_mark_usd: float | None = None,
    rollout_count: int = 1,
    regime_code: CodeMatrix | None = None,
    event_kind_code: CodeMatrix | None = None,
    sale_capacity_fraction: FloatMatrix | None = None,
    eligible_fraction: FloatMatrix | None = None,
    forced_sale_fraction: FloatMatrix | None = None,
    liquidity_blocked: FloatMatrix | None = None,
    forced_recovery_cashout_usd: FloatMatrix | None = None,
) -> PrivateEquityBundle:
    """One issuer's protocol channels: a flat mark that steps at `tender_month`, and a tender
    window open only in that month. Every other channel defaults to "nothing in the way"."""

    marks = np.full((rollout_count, horizon_months + 1), initial_mark_usd, dtype=np.float64)
    open_window = np.zeros((rollout_count, horizon_months + 1), dtype=np.bool_)
    if tender_month is not None and tender_mark_usd is not None:
        marks[:, tender_month:] = tender_mark_usd
        open_window[:, tender_month] = True

    def default_float(value: float) -> FloatMatrix:
        return floats(horizon_months=horizon_months, rollouts=rollout_count, value=value)

    return PrivateEquityBundle.from_issuer_arrays(
        ISSUER,
        mark_usd_per_unit=marks,
        regime_code=regime_code
        if regime_code is not None
        else codes(
            horizon_months=horizon_months, rollouts=rollout_count, value=int(PrivateEquityRegimeCode.PRIVATE_OPERATING)
        ),
        event_kind_code=event_kind_code
        if event_kind_code is not None
        else np.where(open_window, int(PrivateEquityEventKindCode.TENDER), 0).astype(np.int64),
        sale_opportunity_active=open_window,
        sale_capacity_fraction=sale_capacity_fraction if sale_capacity_fraction is not None else default_float(1.0),
        eligible_fraction=eligible_fraction if eligible_fraction is not None else default_float(1.0),
        forced_sale_fraction=forced_sale_fraction if forced_sale_fraction is not None else default_float(0.0),
        liquidity_blocked=(liquidity_blocked >= 0.5).astype(np.bool_)
        if liquidity_blocked is not None
        else np.zeros((rollout_count, horizon_months + 1), dtype=np.bool_),
        forced_recovery_cashout_usd=forced_recovery_cashout_usd
        if forced_recovery_cashout_usd is not None
        else default_float(0.0),
        company_valuation_usd=default_float(0.0),
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )


def holder(
    *,
    initial_cash: Decimal | int,
    monthly_spend: Decimal | int,
    pe_units: float,
    pe_cost_basis_per_unit: Decimal | int,
    pe_holding_period_months: int,
    horizon_months: int,
    lnw_floor: Decimal | int,
    tax_profiles: list[TaxProfile] | None = None,
) -> Scenario:
    """Alice holds `pe_units` of acme, spends monthly, and sells PE only to hold a floor.

    No income and no other holding, so liquid net worth is her cash alone and the floor
    shortfall a tender has to close is arithmetic the case can state.
    """

    accounts = [("alice", Decimal(initial_cash)), ("spend_sink", Decimal(0))]
    if tax_profiles:
        accounts.append(("irs", Decimal(0)))
    return scenario(
        checking(*accounts),
        initial_lots=[
            InitialLot(
                lot_id=LOT_ID,
                agent_id="alice",
                account_id="checking",
                asset=ACME,
                purchase_month_index=-pe_holding_period_months,
                quantity=pe_units,
                cost_basis_per_unit=pe_cost_basis_per_unit,
            )
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                end_month=horizon_months - 1,
                obligation_id="monthly_spend",
                obligation_type="cash_spend",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="spend_sink",
                to_account_id="checking",
                amount_due=monthly_spend,
            )
        ],
        private_equity_tender_policies=[
            PrivateEquityTenderPolicy(
                owner_agent_id="alice",
                proceeds_account_id="checking",
                liquid_net_worth_floor=FixedAmount(amount=lnw_floor),
            )
        ],
        tax_profiles=tax_profiles or [],
        horizon_months=horizon_months,
    )


def units_held(result: SimulationResult, *, month: int) -> float:
    row = result.lots.filter((pl.col("lot_id") == LOT_ID) & (pl.col("month_index") == month)).row(0, named=True)
    return float(cast(int, row["remaining_quantity_quanta"])) / float(cast(int, row["quantity_scale"]))


def cash(result: SimulationResult, *, month: int, agent_id: str = "alice") -> float:
    rows = result.cash.filter((pl.col("agent_id") == agent_id) & (pl.col("month_index") == month))
    return float(cast(int, rows.get_column("balance_quanta").sum())) / 100


def opportunity(result: SimulationResult, *, month: int) -> dict[str, object]:
    return result.events.private_equity_opportunities.filter(
        (pl.col("rollout_index") == 0) & (pl.col("month_index") == month)
    ).row(0, named=True)


def dispositions(result: SimulationResult, *, month: int) -> pl.DataFrame:
    return result.events.lot_dispositions.filter((pl.col("rollout_index") == 0) & (pl.col("month_index") == month))


class PrivateEquityAcceptance:
    """One engine, against what an issuer's protocol and a floor policy jointly decide."""

    def test_a_position_with_no_opportunity_carries_through_untouched(self, backend: Backend) -> None:
        """No tender, no forced sale: a floor far above liquid net worth changes nothing.

        The floor is deliberately unreachable, so this separates "the policy wanted to sell"
        from "the protocol let it" — only the second is missing here.
        """

        horizon = 24
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=100_000,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=500_000,
                ),
                rollout_count=1,
                private_equity=protocol(initial_mark_usd=50.0, horizon_months=horizon),
            )
        )

        assert units_held(result, month=horizon) == pytest.approx(100.0)
        assert cash(result, month=horizon) == pytest.approx(100_000.0)

    def test_a_tender_below_the_floor_sells_toward_it(self, backend: Backend) -> None:
        """The whole position goes when it is worth less than the shortfall.

        Cash at the tender is 30k less six months of 1k spend = 24k, against a 50k floor, so
        the shortfall is 26k and the position is worth 100 x $60 = $6k. Selling all of it
        still leaves her under the floor, which is the point: the policy sells what it can,
        it does not fail when that is not enough.
        """

        horizon, tender_month = 12, 5
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=Decimal(30_000),
                    monthly_spend=Decimal(1_000),
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=Decimal(50_000),
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=50.0, horizon_months=horizon, tender_month=tender_month, tender_mark_usd=60.0
                ),
            )
        )

        assert units_held(result, month=tender_month + 1) == pytest.approx(0.0, abs=0.02)
        assert cash(result, month=tender_month + 1) == pytest.approx(30_000.0, abs=1.0)

        [row] = dispositions(result, month=tender_month).iter_rows(named=True)
        assert row["asset_id"] == ASSET_ID
        assert row["units_sold"] == pytest.approx(100.0, abs=0.02)
        assert row["proceeds_quanta"] / 100 == pytest.approx(6_000.0, abs=1.0)
        assert row["cause_id"] == "pe_tender_m5_acme"

        [marker] = result.events.private_equity_events.filter(
            (pl.col("rollout_index") == 0) & (pl.col("month_index") == tender_month)
        ).iter_rows(named=True)
        assert marker["event_kind"] == "tender"
        assert marker["asset_id"] == ASSET_ID

    def test_a_tender_above_the_floor_passes_without_a_sale(self, backend: Backend) -> None:
        """An open window is not a reason to sell; a shortfall is."""

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=200_000,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=50_000,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=50.0, horizon_months=horizon, tender_month=5, tender_mark_usd=60.0
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(100.0)
        assert cash(result, month=6) == pytest.approx(200_000.0)
        traced = opportunity(result, month=5)
        assert traced["outcome"] == "floor_satisfied"
        assert traced["shortfall_quanta"] == 0

    def test_a_zero_floor_never_sells(self, backend: Backend) -> None:
        """Liquid net worth is always at or above a floor of nothing."""

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=1_000,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=0,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=50.0, horizon_months=horizon, tender_month=5, tender_mark_usd=60.0
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(100.0)

    def test_a_tender_with_no_policy_is_not_taken(self, backend: Backend) -> None:
        """The opportunity is still traced, so "nobody asked" is distinguishable from "refused"."""

        horizon = 12
        case = holder(
            initial_cash=30_000,
            monthly_spend=1_000,
            pe_units=100.0,
            pe_cost_basis_per_unit=10,
            pe_holding_period_months=36,
            horizon_months=horizon,
            lnw_floor=50_000,
        )
        result = backend(
            Case(
                scenario=case.model_copy(update={"private_equity_tender_policies": []}),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=50.0, horizon_months=horizon, tender_month=5, tender_mark_usd=60.0
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(100.0)
        assert opportunity(result, month=5)["outcome"] == "no_policy"

    def test_the_issuers_capacity_caps_what_a_tender_can_sell(self, backend: Backend) -> None:
        """A quarter of the position is sellable, so a floor that wants all of it gets a quarter."""

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=0,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=1_000_000,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=100.0,
                    horizon_months=horizon,
                    tender_month=5,
                    tender_mark_usd=100.0,
                    sale_capacity_fraction=floats(horizon_months=horizon, value=0.25),
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(75.0)
        assert cash(result, month=6) == pytest.approx(2_500.0)
        traced = opportunity(result, month=5)
        assert traced["outcome"] == "sold"
        assert traced["sellable_units"] == pytest.approx(25.0)
        assert traced["target_units"] == pytest.approx(25.0)
        assert cast(int, traced["proceeds_quanta"]) / 100 == pytest.approx(2_500.0)

    def test_zero_capacity_is_traced_as_its_own_outcome(self, backend: Backend) -> None:
        """An open window with no capacity behind it is not the same as a satisfied floor."""

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=0,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=1_000_000,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=100.0,
                    horizon_months=horizon,
                    tender_month=5,
                    tender_mark_usd=100.0,
                    sale_capacity_fraction=floats(horizon_months=horizon, value=0.0),
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(100.0)
        traced = opportunity(result, month=5)
        assert traced["outcome"] == "capacity_zero"
        assert traced["sellable_units"] == pytest.approx(0.0)
        assert traced["target_units"] == pytest.approx(0.0)

    def test_the_eligible_fraction_caps_what_a_tender_can_sell(self, backend: Backend) -> None:
        """Eligibility bites the same way capacity does, on a different channel."""

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=0,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=1_000_000,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=100.0,
                    horizon_months=horizon,
                    tender_month=5,
                    tender_mark_usd=100.0,
                    eligible_fraction=floats(horizon_months=horizon, value=0.4),
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(60.0)
        assert cash(result, month=6) == pytest.approx(4_000.0)

    def test_a_liquidity_block_prevents_the_sale_entirely(self, backend: Backend) -> None:
        """Blocked is its own outcome, and it zeroes the target rather than the proceeds."""

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=0,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=1_000_000,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=100.0,
                    horizon_months=horizon,
                    tender_month=5,
                    tender_mark_usd=100.0,
                    liquidity_blocked=floats(horizon_months=horizon, value=1.0),
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(100.0)
        assert cash(result, month=6) == pytest.approx(0.0)
        traced = opportunity(result, month=5)
        assert traced["outcome"] == "liquidity_blocked"
        assert traced["liquidity_blocked"] is True
        assert traced["target_units"] == pytest.approx(0.0)

    def test_a_public_market_regime_lets_the_floor_sell_with_no_tender(self, backend: Backend) -> None:
        """Once the issuer trades publicly the holder no longer needs a window offered to them.

        A $5k floor against no cash sells exactly the 50 units that closes it, and the cause
        names the regime rather than a tender.
        """

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=0,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=5_000,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=100.0,
                    horizon_months=horizon,
                    regime_code=code_in_month(
                        horizon_months=horizon,
                        month=5,
                        value=int(PrivateEquityRegimeCode.PUBLIC_MARKET),
                        default=int(PrivateEquityRegimeCode.PRIVATE_OPERATING),
                    ),
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(50.0)
        assert cash(result, month=6) == pytest.approx(5_000.0)
        [row] = dispositions(result, month=5).iter_rows(named=True)
        assert row["cause_id"] == "pe_public_market_m5_acme"

    def test_a_forced_sale_happens_with_no_window_and_no_shortfall(self, backend: Backend) -> None:
        """The issuer can redeem against the holder's wishes: floor satisfied, no tender open."""

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=10_000,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=0,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=100.0,
                    horizon_months=horizon,
                    forced_sale_fraction=in_month(horizon_months=horizon, month=5, value=0.3),
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(70.0)
        assert cash(result, month=6) == pytest.approx(13_000.0)
        [row] = dispositions(result, month=5).iter_rows(named=True)
        assert row["cause_id"] == "pe_forced_sale_m5_acme"

    def test_a_forced_sale_still_books_its_capital_gain(self, backend: Backend) -> None:
        """A sale the holder did not choose is taxed like one they did.

        30 units at a $100 mark against a $10 basis is $2,700 of gain, long-term on a lot held
        36 months. Worth pinning separately because a forced sale takes a different path
        through the engine than a tender does, and tax state is what that path could drop.
        """

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=0,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=0,
                    tax_profiles=[taxed("alice", "federal_us")],
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=100.0,
                    horizon_months=horizon,
                    forced_sale_fraction=in_month(horizon_months=horizon, month=5, value=0.3),
                ),
            )
        )

        [gain] = result.capital_gains.filter(
            (pl.col("month_index") == 6) & (pl.col("agent_id") == "alice") & (pl.col("classification") == "ltcg")
        ).iter_rows(named=True)
        assert gain["gain_quanta"] / 100 == pytest.approx(2_700.0)

    def test_proceeds_land_in_the_account_the_policy_names(self, backend: Backend) -> None:
        """An owner with two accounts: the policy's account takes the proceeds, the other is untouched."""

        horizon = 12
        base = holder(
            initial_cash=0,
            monthly_spend=0,
            pe_units=100.0,
            pe_cost_basis_per_unit=10,
            pe_holding_period_months=36,
            horizon_months=horizon,
            lnw_floor=0,
        )
        result = backend(
            Case(
                scenario=base.model_copy(
                    update={
                        "initial_cash": [
                            InitialAccountBalance(agent_id="alice", account_id="savings", balance=123),
                            InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
                            InitialAccountBalance(agent_id="spend_sink", account_id="checking", balance=0),
                        ]
                    }
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=100.0,
                    horizon_months=horizon,
                    forced_sale_fraction=in_month(horizon_months=horizon, month=5, value=0.3),
                ),
            )
        )

        balances = {
            row["account_id"]: row["balance_quanta"]
            for row in result.cash.filter((pl.col("month_index") == 6) & (pl.col("agent_id") == "alice")).iter_rows(
                named=True
            )
        }
        assert balances == {"checking": 300_000, "savings": 12_300}
        [row] = result.events.lot_dispositions.filter(pl.col("cause_id") == "pe_forced_sale_m5_acme").iter_rows(
            named=True
        )
        assert row["proceeds_account_id"] == "checking"

    def test_a_recovery_cashout_takes_the_rest_of_the_position_for_a_stated_amount(self, backend: Backend) -> None:
        """A wind-up pays what it pays: all remaining units go for $100 regardless of the mark.

        The position is marked at $100/unit x 100 units = $10,000, and the holder receives
        $100. That gap is the point — a recovery is not priced off the mark.
        """

        horizon = 12
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=0,
                    monthly_spend=0,
                    pe_units=100.0,
                    pe_cost_basis_per_unit=10,
                    pe_holding_period_months=36,
                    horizon_months=horizon,
                    lnw_floor=0,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=100.0,
                    horizon_months=horizon,
                    forced_recovery_cashout_usd=in_month(horizon_months=horizon, month=5, value=100.0),
                ),
            )
        )

        assert units_held(result, month=6) == pytest.approx(0.0)
        assert cash(result, month=6) == pytest.approx(100.0)
        [row] = dispositions(result, month=5).iter_rows(named=True)
        assert row["units_sold"] == pytest.approx(100.0)
        assert row["proceeds_quanta"] / 100 == pytest.approx(100.0)
        assert row["cause_id"] == "pe_forced_recovery_m5_acme"

    def test_a_disposition_carries_the_lot_it_consumed(self, backend: Backend) -> None:
        """All 200 units at an $80 mark against a $20 basis: $16,000 out, $4,000 of basis gone."""

        horizon, tender_month = 12, 3
        result = backend(
            Case(
                scenario=holder(
                    initial_cash=10_000,
                    monthly_spend=0,
                    pe_units=200.0,
                    pe_cost_basis_per_unit=20,
                    pe_holding_period_months=24,
                    horizon_months=horizon,
                    lnw_floor=100_000,
                ),
                rollout_count=1,
                private_equity=protocol(
                    initial_mark_usd=50.0, horizon_months=horizon, tender_month=tender_month, tender_mark_usd=80.0
                ),
            )
        )

        [row] = dispositions(result, month=tender_month).iter_rows(named=True)
        assert row["asset_id"] == ASSET_ID
        assert row["lot_id"] == LOT_ID
        assert row["agent_id"] == "alice"
        assert row["units_sold"] == pytest.approx(200.0, abs=0.02)
        assert row["cost_basis_consumed_quanta"] / 100 == pytest.approx(4_000.0, abs=1.0)

    def test_a_pe_lot_with_no_protocol_for_its_issuer_is_refused(self, backend: Backend) -> None:
        """A position whose issuer has no channels cannot be priced, marked, or sold.

        Answering it would mean inventing a mark, so the compile fails instead. An empty
        bundle and an absent one are the same object here, which is why this is one case.
        """

        horizon = 12
        case = Case(
            scenario=holder(
                initial_cash=0,
                monthly_spend=0,
                pe_units=100.0,
                pe_cost_basis_per_unit=10,
                pe_holding_period_months=36,
                horizon_months=horizon,
                lnw_floor=1_000_000,
            ),
            rollout_count=1,
            private_equity=PrivateEquityBundle.empty(),
        )
        with pytest.raises(ValueError, match=r"private-equity bundle missing required issuer 'acme'"):
            backend(case)
