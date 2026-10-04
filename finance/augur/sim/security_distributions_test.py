"""Independent public-distribution controls through the common Python action session."""

from collections.abc import Mapping
from decimal import Decimal
from itertools import pairwise

import numpy as np
import pytest_bazel

from finance.augur.model.series import LevelSeriesKey, SecurityDistributionKey
from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.claims import ObligationType
from finance.augur.sim.fixed_point import (
    currency_amount_to_quanta,
    quantity_scale_for_asset,
    quantity_to_quanta,
    rate_to_ppb,
)
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId, LotId
from finance.augur.sim.income import ORDINARY_INCOME, TransferIncomeCategory, income_source_sort_key
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath, Series
from finance.augur.sim.money import USD
from finance.augur.sim.results import Finished, Paid, Rollout
from finance.augur.sim.schedule import Once
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import TaxProfile, compile_profile
from finance.augur.sim.testing.rollouts import tax_by_jurisdiction
from finance.augur.sim.testing.security_distributions import (
    AGGREGATE,
    CALIFORNIA_MUNI,
    CORPORATE_SHARE,
    FUND,
    HORIZON,
    LOSSY_PER_UNIT,
    MONTHLY_PAYOUT_QUANTA,
    PAYOUTS_BY_YEAR_END,
    PER_UNIT,
    PRICE,
    QUALIFIED_DIVIDENDS,
    SUB_QUANTUM_PER_UNIT,
    SYMBOL,
    TAXABLE,
    TREASURY,
    TREASURY_SHARE,
    UNITS,
    YEAR_END,
    payout_quanta,
)
from finance.augur.sim.testing.series import level_series
from finance.augur.sim.testing.session import each, finish
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE = AgentId("alice")
IRS = AgentId("irs")
CHECKING = AccountId("checking")
BROKERAGE = AccountId("brokerage")
FILED_IN = (JurisdictionId("federal_us"), JurisdictionId("california"))


def payout_levels(per_unit: Decimal) -> np.ndarray:
    """The fund's per-unit payout, flat across the horizon on the single rollout."""

    return np.full((1, HORIZON + 1), float(per_unit))


# The payout every case runs on; composition reads it and never writes it.
PAYOUT = payout_levels(PER_UNIT)


def _paths(payout: np.ndarray) -> tuple[Series, ...]:
    """The fund's price and its per-unit payout.

    The two series have the same shape and units, which is the point of the payout being a
    primitive rather than a rate.
    """

    blocks: dict[LevelSeriesKey, np.ndarray] = {
        FUND: np.full((1, HORIZON + 1), float(PRICE)),
        SecurityDistributionKey(symbol=SYMBOL): payout,
    }
    return level_series(blocks, rollout_count=1, horizon_months=HORIZON)


def _account(world: World, agent_id: AgentId, balance: Decimal) -> None:
    world.declare_account(
        account=AccountRef(agent_id=agent_id, account_id=CHECKING),
        opening_balance=int(currency_amount_to_quanta(balance, quantum=QUANTUM)),
    )


def compose(
    *,
    tax_character: Mapping[TransferIncomeCategory, int] = TREASURY,
    is_taxed: bool = True,
    distributes: bool = True,
    holding_account_id: AccountId = BROKERAGE,
    payout: np.ndarray = PAYOUT,
    opening_cash: Decimal = Decimal(50_000),
    bill: Decimal | None = None,
) -> World:
    """Alice holds one fund in a brokerage account and its payout lands in checking; `bill` is a one-off
    month-zero payment she owes the tax authority's cash account.

    `is_taxed=False` leaves the payout standing alone in the cash channel, which the
    cashflow cases want: with a tax authority the year-end settlement lands in the same months.
    """

    # The vocabulary a taxpayer shares: what the fund's slices name.
    categories = tuple(tax_character) if distributes else ()
    world = World(
        MarketPath(_paths(payout), 0, rollout_count=1),
        horizon_months=HORIZON,
        income_sources=tuple(sorted({ORDINARY_INCOME, *categories}, key=income_source_sort_key)),
    )
    _account(world, ALICE, opening_cash)
    _account(world, IRS, Decimal(0))
    if is_taxed:
        # One single filer paying from checking to the irs agent; the rates, brackets and
        # exemptions come from the deployment's own jurisdiction records.
        world.track(
            TaxAuthority(
                compile_profile(
                    TaxProfile(agent_id=ALICE, jurisdiction_ids=list(FILED_IN), tax_authority_agent_id=IRS),
                    {id_: load_jurisdiction(id_) for id_ in FILED_IN},
                    currency=USD,
                ),
                indexation=FixedNominalLaw(),
            )
        )
    scale = quantity_scale_for_asset(FUND)
    world.declare_pool(agent_id=ALICE, account_id=BROKERAGE, asset_id=AssetId(SYMBOL), quantity_scale=scale)
    world.hold_lot(
        lot_id=LotId("bnd-lot"),
        agent_id=ALICE,
        account_id=BROKERAGE,
        asset_id=AssetId(SYMBOL),
        purchase_month=-24,
        quantity_scale=scale,
        units=quantity_to_quanta(UNITS, scale=scale),
        basis=int(currency_amount_to_quanta(UNITS * PRICE, quantum=QUANTUM)),
    )
    if distributes:
        world.declare_distribution(
            agent_id=ALICE,
            holding_account_id=holding_account_id,
            asset_id=AssetId(SYMBOL),
            to_account_id=CHECKING,
            tax_character=tax_character,
        )
    if bill is not None:
        world.track(
            Biller(
                obligation_id="bill",
                obligation_type=ObligationType.CASH_SPEND,
                from_account=AccountRef(agent_id=ALICE, account_id=CHECKING),
                to_account=AccountRef(agent_id=IRS, account_id=CHECKING),
                amount_due=int(currency_amount_to_quanta(bill, quantum=QUANTUM)),
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=rate_to_ppb(1),
                schedule=Once(month=0),
            )
        )
    return world


def _run(world: World) -> Rollout:
    """Receive modeled payouts and explicitly pay observed claims; never trade or retry."""
    [result] = finish(ActionSession({0: world}, ALICE, capture="forensic"), each(ClaimPayer(ALICE).decide)).rollouts
    assert result.stop is None
    return result


def _cash_by_month(result: Rollout) -> dict[int, int]:
    [cash] = result.summary.cash
    assert (cash.account.agent_id, cash.account.account_id) == (ALICE, CHECKING)
    return {month: after - before for month, (before, after) in enumerate(pairwise(cash.values))}


def test_zero_payout_months_move_no_cash_between_actual_payments() -> None:
    payout = payout_levels(Decimal(0))
    payout[0, 6] = float(PER_UNIT)
    expected = dict.fromkeys(range(HORIZON), 0)
    expected[6] = MONTHLY_PAYOUT_QUANTA
    assert _cash_by_month(_run(compose(is_taxed=False, payout=payout))) == expected


def test_a_payout_below_one_quantum_per_unit_still_reaches_cash() -> None:
    """#5832. A per-unit figure is a RATE, and a rate has no reason to be a whole number of
    cents: 10,000 units at $0.0004 each is an ordinary $4.00 payment. Rounding the rate to
    the currency quantum before multiplying by the position sent it to zero per unit, and
    the engine then rejected the whole series as non-positive rather than paying $0.00 —
    which is the only reason anyone noticed.

    This is about the SIZE of the per-unit payout, not the length of the run: a fund at a
    low unit price reaches it at any horizon, so a thirteen-month case pins it.
    """

    result = _run(compose(is_taxed=False, payout=payout_levels(SUB_QUANTUM_PER_UNIT)))
    assert _cash_by_month(result) == dict.fromkeys(range(HORIZON), payout_quanta(SUB_QUANTUM_PER_UNIT))


def test_a_payout_of_a_fractional_quantum_per_unit_is_not_rounded_away() -> None:
    """The half of #5832 that never raised: quantizing the rate first loses up to half a
    quantum PER UNIT, scaled up by the whole position.

    10,000 units at $0.0123 is $123.00. Rounded to whole cents per unit first it is
    $100.00 — a fifth of the payout gone, silently, on a series that validates fine. The
    engine multiplies before it divides, so there is nothing to round until the amount is
    money.
    """

    result = _run(compose(is_taxed=False, payout=payout_levels(LOSSY_PER_UNIT)))
    assert _cash_by_month(result) == dict.fromkeys(range(HORIZON), payout_quanta(LOSSY_PER_UNIT))


def test_splitting_the_tax_character_does_not_change_what_reaches_cash() -> None:
    """The slices are a tax decomposition, not separate payouts. They share one
    destination account, so this also catches a scatter that overwrote instead of
    accumulating."""

    split = _run(compose(tax_character=AGGREGATE, is_taxed=False))
    whole = _run(compose(tax_character=TREASURY, is_taxed=False))

    assert _cash_by_month(split) == _cash_by_month(whole)


def test_a_holding_with_no_declared_distribution_pays_nothing() -> None:
    """Guards the cases above against passing for some unrelated reason: the same lot, the
    same sampled series, no payout declared, no cash."""

    assert set(_cash_by_month(_run(compose(is_taxed=False, distributes=False))).values()) == {0}


def test_a_treasury_funds_distribution_is_federally_taxed_and_california_exempt() -> None:
    tax = tax_by_jurisdiction(_run(compose(tax_character=TREASURY)))

    assert tax["federal_us"] > 0
    assert tax["california"] == 0


def test_an_in_state_muni_funds_distribution_is_exempt_everywhere() -> None:
    tax = tax_by_jurisdiction(_run(compose(tax_character=CALIFORNIA_MUNI)))

    assert tax["federal_us"] == 0
    assert tax["california"] == 0


def test_a_mixed_fund_is_exempt_only_on_its_treasury_slice() -> None:
    """The reason the tax character is a vector. California taxes the corporate 60% and
    not the Treasury 40%, so a mixed fund owes strictly between the all-Treasury and
    all-taxable cases — a number neither single tag can produce."""

    mixed = tax_by_jurisdiction(_run(compose(tax_character=AGGREGATE)))
    treasury = tax_by_jurisdiction(_run(compose(tax_character=TREASURY)))
    taxable = tax_by_jurisdiction(_run(compose(tax_character=TAXABLE)))

    assert treasury["california"] < mixed["california"] < taxable["california"]
    # Federal taxes both slices, so the split changes nothing there.
    assert mixed["federal_us"] == treasury["federal_us"] == taxable["federal_us"]


def test_the_payout_accrues_as_interest_per_character_and_not_as_one_lump() -> None:
    """The slices land in their own income rows rather than summing into one, which is
    what makes the per-jurisdiction exemption computable at all."""

    result = _run(compose(tax_character=AGGREGATE))
    assert result.trace is not None
    december = sorted(
        (
            row
            for book in result.trace.books
            if book.month == YEAR_END
            for row in book.income
            if row.agent_id == ALICE and row.income > 0
        ),
        key=lambda row: row.income_source,
    )
    paid = PAYOUTS_BY_YEAR_END * MONTHLY_PAYOUT_QUANTA

    assert [row.income_source for row in december] == ["interest:taxable", "interest:treasury"]
    assert [row.income for row in december] == [int(CORPORATE_SHARE * paid), int(TREASURY_SHARE * paid)]


def test_qualified_dividends_take_federal_preferential_rates_and_california_ordinary_rates() -> None:
    """Twelve $2,000 payouts close the year. Federal: 24,000 - 14,600 = 9,400 taxable, all in
    the 0% long-term band, where the same payout as corporate interest owes 940.00 at 10%.
    California: 24,000 - 5,363 = 18,637 at 1% to 10,412 and 2% above: 104.12 + 164.50."""

    assert tax_by_jurisdiction(_run(compose(tax_character=QUALIFIED_DIVIDENDS))) == {
        "federal_us": 0,
        "california": 26_862,
    }
    assert tax_by_jurisdiction(_run(compose(tax_character=TAXABLE)))["federal_us"] == 94_000


def test_qualified_dividends_are_their_own_row_in_the_holders_tax_records() -> None:
    session = ActionSession({0: compose(tax_character=QUALIFIED_DIVIDENDS)}, ALICE)
    try:
        batch = session.start()
    finally:
        session.close()
    assert not isinstance(batch, Finished)
    [decision] = batch
    assert decision.observation.tax_records is not None
    assert decision.observation.tax_records.income == (("ordinary", 0), ("qualified_dividend", MONTHLY_PAYOUT_QUANTA))


def test_current_payout_funds_an_explicit_same_month_claim() -> None:
    result = _run(compose(is_taxed=False, opening_cash=Decimal(0), bill=Decimal(2_000)))
    assert result.summary.cash[0].values[:2] == [0, 0]
    assert result.summary.cash[0].values[-1] == 2_400_000
    [payment] = result.summary.payments
    assert payment.month == 0
    assert payment.cause_id == "bill_m0"
    assert payment.receipt.amount_paid == 200_000
    assert isinstance(payment.receipt.outcome, Paid)
    assert result.trace is not None
    assert result.trace.distributions is not None
    [payout] = [row for row in result.trace.distributions if row.month == 0]
    assert (payout.asset_id, payout.amount, payout.income_source) == ("bnd", 200_000, "interest:treasury")


if __name__ == "__main__":
    pytest_bazel.main()
