"""A fund's monthly payout: how much reaches cash, and whose interest it is.

The payout is a primitive — dollars per unit held, sampled like a price — so nothing in an
engine divides or annualizes to compute it. What makes it more than a transfer is the tax
character: a fund is a basket, and its distribution carries the issuers of what is inside it
in proportion. That is why the character is a vector of slices rather than one tag, and why
a mixed fund owes a state something strictly between what an all-Treasury and an
all-corporate fund owe. No single tag can produce that number.

Each slice is paid, attributed and exempted independently, and they share one destination
account — which is also what catches a scatter that overwrote instead of accumulating.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import polars as pl
import pytest

from finance.augur.model.series import SecurityDistributionKey, SecurityKey, SecuritySymbol
from finance.augur.sim.scenario import DistributionTaxSlice, InitialLot, SecurityDistribution
from finance.augur.sim.testing.case import Case, flat, scenario
from finance.augur.sim.testing.fixtures import checking, taxed
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

HORIZON = 13
SYMBOL = SecuritySymbol("bnd")
FUND = SecurityKey(symbol=SYMBOL)
UNITS = 10_000.0
PRICE = Decimal(73)
# A round monthly payout per unit, so `units x per unit` is exact at every split.
PER_UNIT = Decimal("0.20")
# Below half a cent a unit: what a bond fund at a low unit price pays at a low yield, and what
# rounding the RATE to the currency quantum turned into a literal zero (#5832).
SUB_QUANTUM_PER_UNIT = Decimal("0.0004")
# Between one and two cents a unit, where quantizing the rate to whole cents does not zero the
# payout but still loses a fifth of it.
LOSSY_PER_UNIT = Decimal("0.0123")


def payout_quanta(per_unit: Decimal) -> int:
    return int(Decimal(str(UNITS)) * per_unit * 100)


MONTHLY_PAYOUT_QUANTA = payout_quanta(PER_UNIT)

TREASURY = (DistributionTaxSlice(fraction=1.0, issuer_jurisdiction_id="federal_us"),)
CALIFORNIA_MUNI = (DistributionTaxSlice(fraction=1.0, issuer_jurisdiction_id="california"),)
CORPORATE = (DistributionTaxSlice(fraction=1.0),)
# An aggregate fund: part Treasury, part corporate. The case a single tag cannot express.
AGGREGATE = (
    DistributionTaxSlice(fraction=0.4, issuer_jurisdiction_id="federal_us"),
    DistributionTaxSlice(fraction=0.6),
)
TREASURY_SHARE, CORPORATE_SHARE = Decimal("0.4"), Decimal("0.6")
# Snapshot `m` opens month `m`, so month 11 has months 0..10 behind it: eleven payouts.
YEAR_END, PAYOUTS_BY_YEAR_END = 11, 11


def distribution_case(
    *,
    tax_character: tuple[DistributionTaxSlice, ...] = TREASURY,
    is_taxed: bool = True,
    distributes: bool = True,
    holding_account_id: str = "brokerage",
    pays_a_series: bool = True,
    per_unit: Decimal = PER_UNIT,
) -> Case:
    """Alice holds one fund in a brokerage account and its payout lands in checking.

    `is_taxed=False` leaves the payout standing alone in the cash channel, which the
    cashflow cases want: with a tax profile the year-end settlement lands in the same months.
    """

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(50_000)), ("irs", Decimal(0))),
            initial_lots=[
                InitialLot(
                    lot_id="bnd-lot",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=FUND,
                    purchase_month_index=-24,
                    quantity=UNITS,
                    cost_basis_per_unit=PRICE,
                )
            ],
            security_distributions=[
                SecurityDistribution(
                    asset=FUND,
                    agent_id="alice",
                    holding_account_id=holding_account_id,
                    to_account_id="checking",
                    tax_character=tax_character,
                )
            ]
            if distributes
            else [],
            tax_profiles=[taxed("alice", "federal_us", "california")] if is_taxed else [],
            horizon_months=HORIZON,
        ),
        rollout_count=1,
        series={
            FUND: flat(PRICE, rollout_count=1, horizon_months=HORIZON),
            # The two series have the same shape and units, which is the point of the payout
            # being a primitive rather than a rate.
            **(
                {SecurityDistributionKey(symbol=SYMBOL): flat(per_unit, rollout_count=1, horizon_months=HORIZON)}
                if pays_a_series
                else {}
            ),
        },
    )


def _cash_by_month(result: SimulationResult) -> dict[int, int]:
    balances = (
        result.cash.filter((pl.col("agent_id") == "alice") & (pl.col("account_id") == "checking"))
        .sort("month_index")
        .get_column("balance_quanta")
        .to_list()
    )
    return {month: after - before for month, (before, after) in enumerate(pairwise(balances))}


def _tax_by_jurisdiction(result: SimulationResult) -> dict[str, int]:
    accruals = result.events.tax_accruals.group_by("jurisdiction_id").agg(pl.col("amount_quanta").sum())
    return dict(accruals.iter_rows())


class SecurityDistributionAcceptance:
    """Inherit and supply `backend`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def backend(self) -> Backend:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def test_the_payout_is_units_times_dollars_per_unit_every_month(self, backend: Backend) -> None:
        """Monthly, unlike a semiannual coupon, and sized off units held rather than a rate on
        market value — which is why nothing in an engine needs the price to compute it."""

        assert _cash_by_month(backend(distribution_case(is_taxed=False))) == dict.fromkeys(
            range(HORIZON), MONTHLY_PAYOUT_QUANTA
        )

    def test_zero_payout_months_move_no_cash_between_actual_payments(self, backend: Backend) -> None:
        case = distribution_case(is_taxed=False, per_unit=Decimal(0))
        case.series[SecurityDistributionKey(symbol=SYMBOL)][0, 6] = float(PER_UNIT)
        expected = dict.fromkeys(range(HORIZON), 0)
        expected[6] = MONTHLY_PAYOUT_QUANTA
        assert _cash_by_month(backend(case)) == expected

    def test_a_payout_below_one_quantum_per_unit_still_reaches_cash(self, backend: Backend) -> None:
        """#5832. A per-unit figure is a RATE, and a rate has no reason to be a whole number of
        cents: 10,000 units at $0.0004 each is an ordinary $4.00 payment. Rounding the rate to
        the currency quantum before multiplying by the position sent it to zero per unit, and
        the engine then rejected the whole series as non-positive rather than paying $0.00 —
        which is the only reason anyone noticed.

        This is about the SIZE of the per-unit payout, not the length of the run: a fund at a
        low unit price reaches it at any horizon, so a thirteen-month case pins it.
        """

        case = distribution_case(is_taxed=False, per_unit=SUB_QUANTUM_PER_UNIT)
        assert _cash_by_month(backend(case)) == dict.fromkeys(range(HORIZON), payout_quanta(SUB_QUANTUM_PER_UNIT))

    def test_a_payout_of_a_fractional_quantum_per_unit_is_not_rounded_away(self, backend: Backend) -> None:
        """The half of #5832 that never raised: quantizing the rate first loses up to half a
        quantum PER UNIT, scaled up by the whole position.

        10,000 units at $0.0123 is $123.00. Rounded to whole cents per unit first it is
        $100.00 — a fifth of the payout gone, silently, on a series that validates fine. The
        engine multiplies before it divides, so there is nothing to round until the amount is
        money.
        """

        case = distribution_case(is_taxed=False, per_unit=LOSSY_PER_UNIT)
        assert _cash_by_month(backend(case)) == dict.fromkeys(range(HORIZON), payout_quanta(LOSSY_PER_UNIT))

    def test_splitting_the_tax_character_does_not_change_what_reaches_cash(self, backend: Backend) -> None:
        """The slices are a tax decomposition, not separate payouts. They share one
        destination account, so this also catches a scatter that overwrote instead of
        accumulating."""

        split = backend(distribution_case(tax_character=AGGREGATE, is_taxed=False))
        whole = backend(distribution_case(tax_character=TREASURY, is_taxed=False))

        assert _cash_by_month(split) == _cash_by_month(whole)

    def test_a_holding_with_no_declared_distribution_pays_nothing(self, backend: Backend) -> None:
        """Guards the cases above against passing for some unrelated reason: the same lot, the
        same sampled series, no payout declared, no cash."""

        assert set(_cash_by_month(backend(distribution_case(is_taxed=False, distributes=False))).values()) == {0}

    def test_a_treasury_funds_distribution_is_federally_taxed_and_california_exempt(self, backend: Backend) -> None:
        tax = _tax_by_jurisdiction(backend(distribution_case(tax_character=TREASURY)))

        assert tax["federal_us"] > 0
        assert tax["california"] == 0

    def test_an_in_state_muni_funds_distribution_is_exempt_everywhere(self, backend: Backend) -> None:
        tax = _tax_by_jurisdiction(backend(distribution_case(tax_character=CALIFORNIA_MUNI)))

        assert tax["federal_us"] == 0
        assert tax["california"] == 0

    def test_a_mixed_fund_is_exempt_only_on_its_treasury_slice(self, backend: Backend) -> None:
        """The reason the tax character is a vector. California taxes the corporate 60% and
        not the Treasury 40%, so a mixed fund owes strictly between the all-Treasury and
        all-corporate cases — a number neither single tag can produce."""

        mixed = _tax_by_jurisdiction(backend(distribution_case(tax_character=AGGREGATE)))
        treasury = _tax_by_jurisdiction(backend(distribution_case(tax_character=TREASURY)))
        corporate = _tax_by_jurisdiction(backend(distribution_case(tax_character=CORPORATE)))

        assert treasury["california"] < mixed["california"] < corporate["california"]
        # Federal taxes both slices, so the split changes nothing there.
        assert mixed["federal_us"] == treasury["federal_us"] == corporate["federal_us"]

    def test_the_payout_accrues_as_interest_per_issuer_and_not_as_one_lump(self, backend: Backend) -> None:
        """The slices land in their own income rows rather than summing into one, which is
        what makes the per-jurisdiction exemption computable at all."""

        december = (
            backend(distribution_case(tax_character=AGGREGATE))
            .income.filter((pl.col("month_index") == YEAR_END) & (pl.col("income_quanta") > 0))
            .sort("income_source")
        )
        paid = PAYOUTS_BY_YEAR_END * MONTHLY_PAYOUT_QUANTA

        assert december.get_column("income_source").to_list() == ["interest:corporate", "interest:federal_us"]
        assert december.get_column("income_quanta").to_list() == [
            int(CORPORATE_SHARE * paid),
            int(TREASURY_SHARE * paid),
        ]

    def test_a_distribution_on_a_pool_with_no_lots_is_rejected(self, backend: Backend) -> None:
        """Nothing can ever fill it, so the payout would be zero for the whole horizon — which
        looks exactly like a fund that does not distribute."""

        with pytest.raises(ValueError, match="references no lots for alice:ira:bnd"):
            backend(distribution_case(holding_account_id="ira"))

    def test_a_declared_distribution_with_no_sampled_payout_series_is_rejected(self, backend: Backend) -> None:
        """Named by the missing series rather than surfacing later as a non-finite payout."""

        with pytest.raises(ValueError, match="security_distribution:bnd"):
            backend(distribution_case(pays_a_series=False))
