"""The target-allocation policy, run by an engine rather than computed by the policy.

`target_allocation_test.py` proves the policy's arithmetic against its own inputs. This
proves an engine runs it: that the observation it builds is the agent's real state, that the
orders come back and execute against real lots, and that both legs of the money move.

Everything below reads the channels every engine answers in, so the claims are about what a
simulator does with an (s,S) cash band and not about how either one computes it. Prices are
authored flat rather than sampled, so every number is exact.

Two properties in the JAX suite this came from are deliberately not here. Cash conservation
is stated over JAX's cash tensor including its external contra row, which Rust has no
counterpart for — its double-entry ledger validates the same thing per journal entry. And
"sweeping sleeve weights does not recompile" is a claim about JAX's compile cache.
"""

from __future__ import annotations

from decimal import Decimal

import polars as pl
import pytest

from finance.augur.sim.scenario import (
    FixedAmount,
    InitialLot,
    RecurringObligation,
    Scenario,
    SleeveTarget,
    TargetAllocationPolicy,
)
from finance.augur.sim.testing.case import Case, flat, scenario
from finance.augur.sim.testing.fixtures import BND, VTI, checking
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

HORIZON = 4
PRICE = Decimal(100)
# Weights default equal against a 9:1 holding, so stock is the overweight sleeve and every
# raise has to come out of it first.
STOCK_UNITS, BOND_UNITS = 900.0, 100.0
QUANTA_PER_UNIT = 100


def cash_band_case(
    *,
    opening_cash: Decimal | int,
    floor: Decimal | int,
    ceiling: Decimal | int,
    stock_units: float = STOCK_UNITS,
    bond_units: float = BOND_UNITS,
    rent: Decimal | int = 0,
    income: Decimal | int = 0,
    rent_months: tuple[int, int | None] = (1, None),
    purchase_slots: int = 0,
    rebalance_tolerance: float | None = None,
    income_end_month: int | None = None,
    weights: tuple[int, int] = (1, 1),
) -> Case:
    """One agent holding two sleeves against an (s,S) cash band.

    `rent` is an outflow the band must fund and `income` an inflow it must invest; both are
    recurring obligations rather than transfers, because the band is measured against the
    month's obligations and only an obligation is in that projection.
    """

    return Case(
        scenario=cash_band_scenario(
            opening_cash=opening_cash,
            floor=floor,
            ceiling=ceiling,
            stock_units=stock_units,
            bond_units=bond_units,
            rent=rent,
            rent_months=rent_months,
            income=income,
            purchase_slots=purchase_slots,
            rebalance_tolerance=rebalance_tolerance,
            income_end_month=income_end_month,
            weights=weights,
        ),
        rollout_count=1,
        series={
            VTI: flat(PRICE, rollout_count=1, horizon_months=HORIZON),
            BND: flat(PRICE, rollout_count=1, horizon_months=HORIZON),
        },
    )


def cash_band_scenario(
    *,
    opening_cash: Decimal | int,
    floor: Decimal | int,
    ceiling: Decimal | int,
    stock_units: float = STOCK_UNITS,
    bond_units: float = BOND_UNITS,
    rent: Decimal | int = 0,
    income: Decimal | int = 0,
    rent_months: tuple[int, int | None] = (1, None),
    purchase_slots: int = 0,
    rebalance_tolerance: float | None = None,
    income_end_month: int | None = None,
    weights: tuple[int, int] = (1, 1),
) -> Scenario:
    """The scenario alone, for the checks that are about authoring it rather than running it."""

    return scenario(
        checking(
            ("alice", Decimal(opening_cash)),
            # Funded only for what it owes, so an unfunded payer can never fail a rollout.
            ("landlord", Decimal(income) * (HORIZON + 1)),
        ),
        initial_lots=[
            InitialLot(
                lot_id="stock",
                agent_id="alice",
                account_id="checking",
                asset=VTI,
                quantity=stock_units,
                cost_basis_per_unit=PRICE,
                purchase_month_index=0,
            ),
            InitialLot(
                lot_id="bond",
                agent_id="alice",
                account_id="checking",
                asset=BND,
                quantity=bond_units,
                cost_basis_per_unit=PRICE,
                purchase_month_index=0,
            ),
        ],
        recurring_obligations=[
            *([_rent(rent, months=rent_months)] if rent else []),
            # An inflow, so it is not in alice's projected outflow and the band only sees it
            # the month AFTER it lands. That is what makes the buy side fire more than once.
            *([_income(income, end_month=income_end_month)] if income else []),
        ],
        target_allocation_policies=[
            TargetAllocationPolicy(
                agent_id="alice",
                account_id="checking",
                sleeves=[SleeveTarget(asset=VTI, weight=weights[0]), SleeveTarget(asset=BND, weight=weights[1])],
                cash_floor=floor,
                cash_ceiling=ceiling,
                purchase_slots_per_sleeve=purchase_slots,
                rebalance_tolerance=rebalance_tolerance,
            )
        ],
        tax_profiles=[],
        horizon_months=HORIZON,
    )


def _rent(amount: Decimal | int, *, months: tuple[int, int | None]) -> RecurringObligation:
    start_month, end_month = months
    return RecurringObligation(
        start_month=start_month,
        end_month=end_month,
        obligation_id="rent",
        obligation_type="rent",
        agent_id="alice",
        from_account_id="checking",
        to_agent_id="landlord",
        to_account_id="checking",
        amount_due=FixedAmount(amount=amount),
    )


def _income(amount: Decimal | int, *, end_month: int | None = None) -> RecurringObligation:
    return RecurringObligation(
        start_month=1,
        end_month=end_month,
        obligation_id="income",
        obligation_type="cash_spend",
        agent_id="landlord",
        from_account_id="checking",
        to_agent_id="alice",
        to_account_id="checking",
        amount_due=FixedAmount(amount=amount),
    )


def _units(result: SimulationResult, *, month: int) -> dict[str, float]:
    """Every lot's remaining units as of one snapshot, by lot id."""

    rows = result.lots.filter(pl.col("month_index") == month).to_dicts()
    return {row["lot_id"]: row["remaining_quantity_quanta"] / row["quantity_scale"] for row in rows}


def _alice_cash(result: SimulationResult) -> list[int]:
    return result.cash.filter(pl.col("agent_id") == "alice").sort("month_index").get_column("balance_quanta").to_list()


def _slots(units: dict[str, float]) -> dict[str, float]:
    return {lot_id: quantity for lot_id, quantity in units.items() if lot_id.startswith("allocation_sale_buy")}


class TargetAllocationAcceptance:
    """Inherit and supply `backend`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def backend(self) -> Backend:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def test_a_month_inside_the_band_sells_nothing(self, backend: Backend) -> None:
        """Drift alone never triggers a trade. The portfolio is 9:1 against a 1:1 target — as
        far from target as this scenario gets — and the policy still does nothing while cash
        sits inside the band. Rebalancing rides cashflow only."""

        result = backend(cash_band_case(opening_cash=50_000, floor=10_000, ceiling=90_000))

        assert _units(result, month=HORIZON) == {"stock": STOCK_UNITS, "bond": BOND_UNITS}
        assert _alice_cash(result)[-1] == 5_000_000

    def test_crossing_the_floor_refills_to_the_ceiling(self, backend: Backend) -> None:
        """(s,S), through the engine. Cash below the floor is raised to the CEILING, not back
        to the floor — refilling to the floor would put the agent back at its trigger next
        month, making it a forced seller into every dip.

        $5,000 with a $10,000 floor and a $40,000 ceiling raises $35,000, which at $100/unit is
        350 units out of the overweight stock sleeve.
        """

        result = backend(cash_band_case(opening_cash=5_000, floor=10_000, ceiling=40_000))

        assert _alice_cash(result)[1] == 4_000_000
        assert _units(result, month=1) == {"stock": 550.0, "bond": BOND_UNITS}

    def test_the_raise_comes_out_of_the_overweight_sleeve(self, backend: Backend) -> None:
        """Water-filling, observed end to end. Stock is worth $90,000 and bonds $10,000 against
        equal weights, so the first $80,000 of any raise comes entirely from stock — the level
        where the two sleeves meet. The bond sleeve is untouched, which is what "don't sell the
        underweight sleeve" means when it is not a slogan."""

        result = backend(cash_band_case(opening_cash=0, floor=1_000, ceiling=30_000))

        assert _units(result, month=1) == {"stock": 600.0, "bond": BOND_UNITS}

    def test_the_band_is_measured_after_the_months_obligations(self, backend: Backend) -> None:
        """The decision is made against the balance the month will END at, not the balance
        sitting there before the bills — which is what lets funding happen once a month like a
        person.

        Month 0 has no rent and $12,000 sits inside the band, so nothing happens. Month 1
        brings $5,000 of rent: a policy reading the CURRENT balance sees $12,000, above the
        $10,000 floor, and sells nothing — leaving $7,000 after the rent, below the floor it
        was supposed to hold. Reading the PROJECTED balance sees $7,000 and raises to the
        $30,000 ceiling, so $23,000 is sold (230 units of the overweight stock) and the rent
        settles out of the refilled account.

        Asserted exactly rather than as "sold something": the wrong reading also sells on later
        months, so an inequality would pass against the defect this test exists to catch.
        """

        result = backend(cash_band_case(opening_cash=12_000, floor=10_000, ceiling=30_000, rent=5_000))

        # Index N is the state ENTERING month N, so month 1's sale shows at index 2.
        assert _units(result, month=1) == {"stock": STOCK_UNITS, "bond": BOND_UNITS}
        assert _units(result, month=2) == {"stock": 670.0, "bond": BOND_UNITS}
        assert _alice_cash(result)[2] == 3_000_000
        assert result.rollout_status.get_column("status").to_list() == ["active"]

    def test_a_raise_the_sleeves_cannot_cover_drains_them_and_stops(self, backend: Backend) -> None:
        """Asking for more than the portfolio holds sells all of it and does not go negative."""

        result = backend(cash_band_case(opening_cash=0, floor=1_000, ceiling=10_000_000))

        assert _units(result, month=1) == {"stock": 0.0, "bond": 0.0}
        assert _alice_cash(result)[1] == (STOCK_UNITS + BOND_UNITS) * int(PRICE) * QUANTA_PER_UNIT

    def test_a_sale_shows_up_as_a_lot_disposition(self, backend: Backend) -> None:
        """A sale the ledger records but the disposition frame does not is a sale nobody can
        audit: cash and lots move, and the row explaining WHY is missing. This asserts the row
        exists, is attributed to the selling agent and the sleeve's asset, and reconciles
        against the lots the run actually gave up.
        """

        result = backend(cash_band_case(opening_cash=5_000, floor=10_000, ceiling=40_000))
        rows = result.events.lot_dispositions.filter(pl.col("agent_id") == "alice").to_dicts()

        assert [row["lot_id"] for row in rows] == ["stock"]
        assert rows[0]["asset_id"] == "security:vti"
        assert rows[0]["units_sold"] == 350.0
        assert rows[0]["proceeds_quanta"] == 3_500_000
        assert rows[0]["cost_basis_consumed_quanta"] == 3_500_000
        # The bond sleeve was never touched, so it must not appear at all — an over-broad
        # decode would emit a zero-unit row for it, and the equality above refuses that.

    def test_configuring_purchase_slots_changes_nothing_until_they_are_filled(self, backend: Backend) -> None:
        """Slots are capacity, not behaviour. A policy given room to buy still holds only what
        it started with until something fills them, and the empty slots must not disturb the
        sale side: they join the same FIFO pool as the sleeve's real lots, so a slot that
        counted as a lot would shift what a sale reaches for.
        """

        without = backend(cash_band_case(opening_cash=5_000, floor=10_000, ceiling=40_000))
        with_slots = backend(cash_band_case(opening_cash=5_000, floor=10_000, ceiling=40_000, purchase_slots=3))

        units = _units(with_slots, month=1)

        assert {lot_id: q for lot_id, q in units.items() if not lot_id.startswith("allocation_sale_buy")} == _units(
            without, month=1
        )
        # Six slots, two sleeves by three, and every one of them still empty.
        assert sorted(_slots(units)) == [
            f"allocation_sale_buy_p0_s{sleeve}_{index}" for sleeve in (0, 1) for index in (0, 1, 2)
        ]
        assert set(_slots(units).values()) == {0.0}
        assert _alice_cash(with_slots) == _alice_cash(without)

    def test_surplus_above_the_ceiling_is_invested_into_the_underweight_sleeve(self, backend: Backend) -> None:
        """The buy side, end to end. $100,000 against a $10,000 floor and a $20,000 ceiling
        invests $90,000 — down to the FLOOR, not to the ceiling, for the same (s,S) reason a
        raise goes to the far edge.

        Where it goes is water-filling in reverse: stock is worth $90,000 and bonds $10,000
        against equal weights, so the deposit levels them at $95,000 each — $85,000 into bonds
        and $5,000 into stock. That is the asymmetry with the sale side made visible: a raise
        came entirely out of stock, and the deposit goes overwhelmingly the other way.
        """

        units = _units(
            backend(cash_band_case(opening_cash=100_000, floor=10_000, ceiling=20_000, purchase_slots=1)), month=1
        )

        assert units["allocation_sale_buy_p0_s0_0"] == 50.0
        assert units["allocation_sale_buy_p0_s1_0"] == 850.0
        # The holdings it started with are untouched: this month bought, it did not rebalance.
        assert units["stock"] == STOCK_UNITS
        assert units["bond"] == BOND_UNITS

    def test_a_purchase_leaves_exactly_the_floor(self, backend: Backend) -> None:
        """A quantum of overshoot would show as the floor minus the overshoot, which is the
        band spending money it promised to keep."""

        result = backend(cash_band_case(opening_cash=100_000, floor=10_000, ceiling=20_000, purchase_slots=1))

        assert _alice_cash(result)[1] == 1_000_000

    def test_a_purchase_records_the_price_its_rollout_paid(self, backend: Backend) -> None:
        """Basis comes from the purchase, and it is not knowable at compile time: the slot
        carries whatever its own rollout paid the month it crossed the band. Reading a static
        column would report 0, making the whole proceeds a gain on the eventual sale."""

        result = backend(cash_band_case(opening_cash=100_000, floor=10_000, ceiling=20_000, purchase_slots=1))
        bought = result.lots.filter(
            (pl.col("lot_id") == "allocation_sale_buy_p0_s1_0") & (pl.col("month_index") == 1)
        ).to_dicts()[0]

        assert bought["cost_basis_per_unit_quanta"] == int(PRICE) * QUANTA_PER_UNIT

    def test_successive_purchases_fill_successive_slots(self, backend: Backend) -> None:
        """The cursor. Each month's buy takes the next free slot, so two purchases are two lots
        with two purchase months — which is the whole reason a purchase cannot share a slot:
        they have different holding periods and would net to one wrong basis.

        Income arrives as an inflow, so the band only sees it the month AFTER it lands and the
        policy invests in months 2 and 3. Both go to bonds: at $10,000 against stock's $90,000,
        the bond sleeve is still underweight after both deposits.
        """

        result = backend(cash_band_case(opening_cash=0, floor=0, ceiling=1_000, income=30_000, purchase_slots=2))
        rows = (
            result.lots.filter(
                pl.col("lot_id").str.starts_with("allocation_sale_buy_p0_s1_") & (pl.col("month_index") == HORIZON)
            )
            .sort("lot_id")
            .to_dicts()
        )

        assert [row["remaining_quantity_quanta"] / row["quantity_scale"] for row in rows] == [300.0, 300.0]
        assert [row["purchase_month_index"] for row in rows] == [2, 3]

    def test_a_runtime_purchase_keeps_its_month_when_later_sold(self, backend: Backend) -> None:
        """Disposition metadata comes from runtime lot state, not the slot's compile-time
        placeholder."""

        # One unit in each starting sleeve rather than none, which the strict fixture refuses.
        # They are month 0 and sell first, so the raise has to reach past them into the slot.
        result = backend(
            cash_band_case(
                opening_cash=0,
                floor=0,
                ceiling=1_000,
                stock_units=1.0,
                bond_units=1.0,
                income=30_000,
                income_end_month=1,
                rent=10_000,
                rent_months=(3, 3),
                purchase_slots=1,
            )
        )
        rows = result.events.lot_dispositions.filter(
            pl.col("lot_id").str.starts_with("allocation_sale_buy_")
        ).to_dicts()

        assert rows
        assert {row["purchase_month_index"] for row in rows} == {2}

    def test_running_out_of_purchase_slots_aborts_the_run(self, backend: Backend) -> None:
        """Aborting, not dropping the surplus purchase — and aborting the RUN, not failing the
        rollouts that hit the wall. Dropping it is a policy that silently stops investing
        partway through the horizon; failing only the affected rollouts drops exactly the paths
        that traded most, and since trading tracks volatility that biases what survives toward
        calm.

        The scenario successive purchases fill, with one slot instead of two, so the second
        purchase has nowhere to go.
        """

        with pytest.raises(ValueError, match="ran out of purchase slots: 1 configured, 2 needed"):
            backend(cash_band_case(opening_cash=0, floor=0, ceiling=1_000, income=30_000, purchase_slots=1))

    def test_a_drifted_portfolio_is_rebalanced_in_a_quiet_month(self, backend: Backend) -> None:
        """The mechanism neither side of the band can express. Cash sits at $50,000 inside a
        [$10,000, $90,000] band, so nothing is being funded and nothing is being invested — and
        yet the portfolio is 9:1 against a 1:1 target.

        Without a tolerance this is exactly `test_a_month_inside_the_band_sells_nothing`. With
        one, $40,000 crosses: 400 units of stock sold and 400 units of bonds bought, landing
        both sleeves on $50,000. The sale and the purchase are two independent legs — the sell
        runs before settlement and the buy after — so this also pins that they meet in the same
        month.
        """

        result = backend(
            cash_band_case(
                opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1, rebalance_tolerance=0.25
            )
        )
        units = _units(result, month=1)

        assert units["stock"] == 500.0
        assert units["allocation_sale_buy_p0_s1_0"] == 400.0
        # Untouched: the bond sleeve was the underweight one, so the trim never reaches it.
        assert units["bond"] == BOND_UNITS
        assert units["allocation_sale_buy_p0_s0_0"] == 0.0
        # Cash-neutral to the cent. A rebalance is a portfolio operation, not a funding one.
        assert _alice_cash(result)[1] == 5_000_000

    def test_a_rebalanced_portfolio_then_sits_still(self, backend: Backend) -> None:
        """One trigger, not one per month. Once both sleeves are on target the drift is zero,
        so a flat price path produces exactly one rebalance over the horizon — which is why a
        single purchase slot per sleeve is enough here, and why a policy that re-triggered
        every month would exhaust its slots and abort instead of quietly churning."""

        result = backend(
            cash_band_case(
                opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1, rebalance_tolerance=0.25
            )
        )
        trades = result.events.lot_dispositions.filter(pl.col("agent_id") == "alice").to_dicts()

        assert [(row["lot_id"], row["units_sold"]) for row in trades] == [("stock", 400.0)]
        assert _units(result, month=HORIZON) == _units(result, month=1)

    def test_a_tolerance_wider_than_the_drift_changes_nothing(self, backend: Backend) -> None:
        """Configuring a rebalance is not asking for one. The fixture is 80% off target, so a
        100% tolerance leaves it exactly where an unconfigured policy would."""

        with_tolerance = backend(
            cash_band_case(opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1, rebalance_tolerance=1.0)
        )
        without = backend(cash_band_case(opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1))

        assert _units(with_tolerance, month=HORIZON) == _units(without, month=HORIZON)
        assert _alice_cash(with_tolerance) == _alice_cash(without)
