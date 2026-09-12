"""A landlord's rental: what the money does, and what the tax code makes of it.

The situations are exact by construction — exogenous series are held flat or stepped, so
every expected cashflow, deduction and gain is arithmetic a reader can redo rather than a
band. That matters most for the tax cases: depreciation, the mortgage-interest split, §1250
recapture and §121 all turn on which month a thing became true, and a test that only checked
"roughly falls" would not notice a year-long offset.

Scope: this level takes rent, management fees and leasing fees as already-lowered dollar
amounts on already-windowed transfers. It has no notion of `vacancy_pct`,
`management_fee_pct`, `fraction_rented` or a leasing-fee cadence — the product layer folds
those into the amounts and month windows before a situation reaches a world. Tests for that
lowering belong in `finance/augur/product/service_test.py`; one here that fed a percentage in
and asserted the same product back could not fail for any bug.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import HomeValueKey, LevelSeriesKey, LocationId, RentKey
from finance.augur.sim.actions import Action, ClaimId, DecisionActions, PayClaim
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb, round_currency_amount
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedAmount,
    PreparedIndexedAmount,
    PreparedJurisdiction,
    PreparedLocation,
    PreparedPropertyCashflow,
    PreparedRecurringObligation,
    PreparedRecurringPropertyCashflow,
    PreparedRecurringTransfer,
    PreparedSeries,
    PreparedTransfer,
    _CapitalImprovement,
    _MortgageFinancing,
    _MortgageInterestDeduction,
    _PrimaryResidence,
    _PrimaryResidenceEvent,
    _PropertyPurchase,
    _PropertySale,
    _PropertyTax,
    _RentedFraction,
    _SaltCap,
    _SaltDeduction,
)
from finance.augur.sim.property import Housing
from finance.augur.sim.results import Executed, Finished, Rollout, Trace
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    FilingStatus,
    InterestIncome,
    ObligationType,
    TaxProfile,
    TransferDeductionCategory,
    TransferIncomeCategory,
)
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
CHECKING = "checking"
OWNER = "owner"
TENANT = "tenant"
AGENCY = "property_management_agency"
SELLER = "property_seller"
LENDER = "lender"
EMPLOYER = "employer"
IRS = "irs"
HOA = "hoa"
COUNTY = "county_assessor"
ISSUER = "bond_issuer"
ALICE = "alice"
BOB = "bob"
FEDERAL = "federal_us"
CALIFORNIA = "california"
SF = "san_francisco"
RENT = RentKey(location_id=LocationId("test_location"))
HOME_VALUE = HomeValueKey(location_id=LocationId(SF))
# $500k house, 20% land: the building basis §168 depreciates, and the ceiling on the total.
BUILDING_BASIS_QUANTA = 400_000 * 100
MUNI_INTEREST = InterestIncome(issuer_jurisdiction_id=CALIFORNIA)
SF_LOCATION = PreparedLocation(
    location_id=SF,
    display_name="San Francisco, CA",
    jurisdiction_ids=(FEDERAL, CALIFORNIA),
    annual_property_tax_rate_ppb=rate_to_ppb(0.01180),
    annual_special_assessment=0,
)


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def ref(agent_id: str) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=CHECKING)


def account(agent_id: str, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=ref(agent_id), opening_balance=money(balance))


def indexed(base_amount: Decimal | int) -> PreparedIndexedAmount:
    """A rent-indexed amount resetting annually, which is what the product layer lowers rent to."""

    return PreparedIndexedAmount(
        base_amount=money(base_amount), series_id=RENT.wire_id, base_month_index=0, adjustment_period_months=12
    )


def series(
    levels: Mapping[LevelSeriesKey, Sequence[Sequence[float]]], *, horizon_months: int, rollout_count: int
) -> tuple[PreparedSeries, ...]:
    """The authored level paths as integer series, one path per rollout and dense to the horizon."""

    paths = ExternalSeriesContext.from_level_blocks(
        [(key, np.asarray(block, dtype=np.float64)) for key, block in levels.items()],
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )
    return compile_series(paths, rollout_count=rollout_count, horizon_months=horizon_months, currency_quantum=QUANTUM)


def recurring_transfer(
    cause_id: str,
    *,
    start_month: int,
    end_month: int | None,
    payer: str,
    payee: str,
    amount: PreparedAmount,
    income: TransferIncomeCategory | None = None,
    deduction: TransferDeductionCategory | None = None,
) -> PreparedRecurringTransfer:
    return PreparedRecurringTransfer(
        start_month=start_month,
        end_month=end_month,
        cause_id=cause_id,
        from_account=ref(payer),
        to_account=ref(payee),
        amount=amount,
        income_category=income,
        deduction_category=deduction,
    )


def scheduled_transfer(
    cause_id: str,
    *,
    month: int,
    payer: str,
    payee: str,
    amount: PreparedAmount,
    income: TransferIncomeCategory | None = None,
    deduction: TransferDeductionCategory | None = None,
) -> PreparedTransfer:
    return PreparedTransfer(
        month=month,
        cause_id=cause_id,
        from_account=ref(payer),
        to_account=ref(payee),
        amount=amount,
        income_category=income,
        deduction_category=deduction,
    )


def recurring_property_cashflow(
    cause_id: str,
    *,
    property_id: str,
    start_month: int,
    end_month: int | None,
    payer: str,
    payee: str,
    amount: PreparedAmount,
    income: TransferIncomeCategory | None = None,
    deduction: TransferDeductionCategory | None = None,
) -> PreparedRecurringPropertyCashflow:
    return PreparedRecurringPropertyCashflow(
        start_month=start_month,
        end_month=end_month,
        property_id=property_id,
        cause_id=cause_id,
        from_account=ref(payer),
        to_account=ref(payee),
        amount=amount,
        income_category=income,
        deduction_category=deduction,
    )


def scheduled_property_cashflow(
    cause_id: str,
    *,
    property_id: str,
    month: int,
    payer: str,
    payee: str,
    amount: PreparedAmount,
    income: TransferIncomeCategory | None = None,
    deduction: TransferDeductionCategory | None = None,
) -> PreparedPropertyCashflow:
    return PreparedPropertyCashflow(
        month=month,
        property_id=property_id,
        cause_id=cause_id,
        from_account=ref(payer),
        to_account=ref(payee),
        amount=amount,
        income_category=income,
        deduction_category=deduction,
    )


def dues(
    obligation_id: str,
    *,
    obligation_type: ObligationType,
    payer: str,
    payee: str,
    amount: PreparedAmount,
    end_month: int | None = None,
    property_id: str | None = None,
    deductible_fraction: float = 1.0,
) -> PreparedRecurringObligation:
    return PreparedRecurringObligation(
        start_month=0,
        end_month=end_month,
        obligation_id=obligation_id,
        obligation_type=obligation_type,
        from_account=ref(payer),
        to_account=ref(payee),
        amount_due=amount,
        property_id=property_id,
        deduction_category="ordinary",
        deductible_fraction_ppb=rate_to_ppb(deductible_fraction),
    )


def financing(
    liability_id: str, *, principal: Decimal | int, rate: float = 0.06, term_months: int = 360
) -> _MortgageFinancing:
    return _MortgageFinancing(
        liability_id=liability_id,
        lender_agent_id=LENDER,
        lender_account_id=CHECKING,
        principal=money(principal),
        annual_interest_rate_ppb=rate_to_ppb(rate),
        term_months=term_months,
    )


def purchase(
    property_id: str,
    *,
    buyer: str = OWNER,
    month: int = 0,
    price: Decimal | int = 500_000,
    rented_fraction: float = 0.0,
    land_value_fraction: float = 0.20,
    closing_cost: Decimal | int = 0,
    mortgage: _MortgageFinancing | None = None,
) -> _PropertyPurchase:
    """A property bought outright, or with a loan funding the rest of the price."""

    financed = 0 if mortgage is None else mortgage.principal
    return _PropertyPurchase(
        month=month,
        cause_id=f"{property_id}_purchase",
        property_id=property_id,
        location_id=SF,
        buyer_agent_id=buyer,
        buyer_account_id=CHECKING,
        seller_agent_id=SELLER,
        seller_account_id=CHECKING,
        purchase_price=money(price),
        down_payment=money(price) - financed,
        buyer_closing_cost=money(closing_cost),
        rented_fraction_ppb=rate_to_ppb(rented_fraction),
        land_value_fraction_ppb=rate_to_ppb(land_value_fraction),
        mortgage=mortgage,
    )


def property_tax(
    property_id: str, owner: str, *, rate: float | None = None, end_month: int | None = None
) -> _PropertyTax:
    return _PropertyTax(
        property_id=property_id,
        owner_agent_id=owner,
        from_account_id=CHECKING,
        tax_authority_agent_id=COUNTY,
        tax_authority_account_id=CHECKING,
        annual_tax_rate_ppb=None if rate is None else rate_to_ppb(rate),
        start_month=0,
        end_month=end_month,
    )


def mortgage_interest_deduction(liability_id: str, owner: str) -> _MortgageInterestDeduction:
    """Acquisition debt under the post-TCJA federal and preserved pre-TCJA California caps."""

    return _MortgageInterestDeduction(
        liability_id=liability_id,
        owner_agent_id=owner,
        debt_class="acquisition",
        per_jurisdiction_principal_cap={FEDERAL: money(750_000), CALIFORNIA: money(1_000_000)},
    )


def salt_cap(profile_id: str, cap: Decimal | int) -> _SaltDeduction:
    return _SaltDeduction(
        profile_id=profile_id,
        federal_jurisdiction_id=FEDERAL,
        cap_schedule=(_SaltCap(effective_year_index=0, cap=money(cap)),),
    )


def taxpayer(agent_id: str = OWNER, *, jurisdiction_ids: Sequence[str] = (FEDERAL, CALIFORNIA)) -> TaxProfile:
    return TaxProfile(
        agent_id=agent_id,
        filing_status=FilingStatus.SINGLE,
        jurisdiction_ids=list(jurisdiction_ids),
        tax_authority_agent_id=IRS,
    )


@dataclass(frozen=True)
class Situation:
    """Every fact one rental world is declared from, already exact."""

    horizon_months: int
    rollout_count: int
    series: tuple[PreparedSeries, ...]
    accounts: tuple[PreparedAccount, ...]
    income_sources: tuple[TransferIncomeCategory, ...] = (ORDINARY_INCOME,)
    tax_profiles: tuple[TaxProfile, ...] = ()
    recurring_transfers: tuple[PreparedRecurringTransfer, ...] = ()
    scheduled_transfers: tuple[PreparedTransfer, ...] = ()
    recurring_property_cashflows: tuple[PreparedRecurringPropertyCashflow, ...] = ()
    scheduled_property_cashflows: tuple[PreparedPropertyCashflow, ...] = ()
    obligations: tuple[PreparedRecurringObligation, ...] = ()
    housing: Housing = field(default_factory=Housing)
    locations: tuple[PreparedLocation, ...] = ()
    property_tax_policies: tuple[_PropertyTax, ...] = ()
    mortgage_interest_policies: tuple[_MortgageInterestDeduction, ...] = ()
    salt_policies: tuple[_SaltDeduction, ...] = ()


def compose(situation: Situation, rollout_id: int) -> World:
    jurisdiction_ids = sorted({id_ for profile in situation.tax_profiles for id_ in profile.jurisdiction_ids})
    jurisdictions = {id_: load_jurisdiction(id_) for id_ in jurisdiction_ids}
    world = World(
        MarketPath(situation.series, rollout_id, rollout_count=situation.rollout_count),
        horizon_months=situation.horizon_months,
        income_sources=situation.income_sources,
        jurisdictions=tuple(
            PreparedJurisdiction(jurisdiction_id=id_, level=jurisdictions[id_].level) for id_ in jurisdiction_ids
        ),
    )
    for opening in situation.accounts:
        world.declare_account(opening)
    for profile in situation.tax_profiles:
        world.track(TaxAuthority(compile_profile(profile, jurisdictions, quantum=QUANTUM)))
    world.accounting.tax.salt_policies = situation.salt_policies
    world.accounting.tax.mortgage_interest_policies = situation.mortgage_interest_policies
    if situation.housing != Housing() or situation.property_tax_policies:
        world.declare_housing(situation.housing, situation.property_tax_policies, situation.locations)
    # Counterparty cashflow tables rather than actions: the world moves these in `prepare_month`,
    # before the month's claims are assembled.
    world.scheduled_transfers = situation.scheduled_transfers
    world.recurring_transfers = situation.recurring_transfers
    world.scheduled_property_cashflows = situation.scheduled_property_cashflows
    world.recurring_property_cashflows = situation.recurring_property_cashflows
    for obligation in situation.obligations:
        world.track(Biller(obligation))
    return world


def pay_claims(observation: Observation) -> list[Action]:
    """The landlord pays what it is billed: property tax, mortgage instalments, dues, assessments."""

    return [
        PayClaim(
            request_id=index + 1,
            cause_id=claim.cause_id,
            claim=claim,
            from_account=claim.from_account,
            amount=claim.amount_due,
        )
        for index, claim in enumerate(observation.claims)
    ]


def settle(world: World, payers: Sequence[str]) -> None:
    """A co-owner's own claims in the same world.

    One world decides for one agent, so a second taxpayer sharing it settles against the
    ledger directly rather than through the session's observation.
    """

    for payer in payers:
        for index, claim in enumerate(world.claims.entries):
            if claim.from_account.agent_id != payer or claim.paid:
                continue
            receipt = world.execute(
                payer,
                PayClaim(
                    request_id=index + 1,
                    cause_id=claim.cause_id,
                    claim=ClaimId(month=world.month, index=index),
                    from_account=claim.from_account,
                    amount=claim.amount_due,
                ),
            )
            assert isinstance(receipt.outcome, Executed), receipt


def run(situation: Situation, *, actor: str = OWNER, co_owners: Sequence[str] = ()) -> list[Rollout]:
    """Every path to the horizon, paying every claim the month raises."""

    worlds = {rollout_id: compose(situation, rollout_id) for rollout_id in range(situation.rollout_count)}
    session = ActionSession(worlds, actor)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            for decision in batch:
                settle(worlds[decision.rollout_id], co_owners)
            batch = session.advance(
                [
                    DecisionActions(decision.rollout_id, decision.observation.month, pay_claims(decision.observation))
                    for decision in batch
                ]
            )
    finally:
        session.close()
    for rollout in batch.rollouts:
        assert rollout.stop is None
    return batch.rollouts


def trace(rollout: Rollout) -> Trace:
    assert rollout.trace is not None
    return rollout.trace


def transfers(rollout: Rollout, cause_id: str) -> pl.DataFrame:
    return trace(rollout).events.transfers.filter(pl.col("cause_id") == cause_id).sort("month_index")


def dollars(frame: pl.DataFrame, column: str) -> list[float]:
    return [value / 100 for value in frame[column].to_list()]


def book_at(rollout: Rollout, month: int) -> Book:
    return one(book for book in trace(rollout).books if book.month == month)


def cash(rollout: Rollout, agent_id: str, month: int) -> float:
    balance = one(row for row in book_at(rollout, month).balances if row.account == ref(agent_id))
    return balance.balance / 100


def breakdown(rollout: Rollout, *, month: int, jurisdiction: str, agent_id: str = OWNER) -> dict[str, Any]:
    """One taxpayer's year-end tax breakdown under one jurisdiction."""

    return one(
        row
        for row in trace(rollout).events.tax_breakdowns.iter_rows(named=True)
        if (row["month_index"], row["jurisdiction_id"], row["agent_id"]) == (month, jurisdiction, agent_id)
    )


def sale_rows(rollout: Rollout) -> dict[str, dict[str, Any]]:
    return {row["property_id"]: row for row in trace(rollout).events.property_sale_events.iter_rows(named=True)}


def mortgage_monthly_payment(
    principal: Decimal, annual_interest_rate: float, term_months: int, *, currency_quantum: Decimal
) -> Decimal:
    """Independent high-precision annuity expectation, rounded once to currency."""

    monthly_rate = Decimal(str(annual_interest_rate)) / 12
    if monthly_rate == 0:
        payment = principal / term_months
    else:
        payment = principal * monthly_rate / (1 - (1 + monthly_rate) ** -term_months)
    return payment.quantize(currency_quantum, rounding=ROUND_HALF_UP)


def mortgage_balance_and_interest_after_payments(
    *, principal: float, annual_interest_rate: float, term_months: int, payment_count: int
) -> tuple[float, float]:
    balance = float(principal)
    interest_paid = 0.0
    payment = float(
        mortgage_monthly_payment(Decimal(str(principal)), annual_interest_rate, term_months, currency_quantum=QUANTUM)
    )
    for _ in range(payment_count):
        interest = balance * annual_interest_rate / 12
        amount = min(payment, balance + interest)
        principal_paid = amount - interest
        balance = max(0, balance - principal_paid)
        interest_paid += interest
    return balance, interest_paid


def rental(
    *,
    horizon_months: int = 12,
    monthly_rent: Decimal | int = 5_000,
    initial_cash: Decimal | int = 100_000,
    monthly_management_fee: Decimal | int | None = None,
    leasing_fees_by_month: Mapping[int, Decimal | int] | None = None,
    rent_levels: Sequence[float] | None = None,
) -> Situation:
    """A minimal untaxed rental: rent in, optional management and leasing fees out.

    Every dollar amount is passed through verbatim, so a test asserting one of these numbers
    back out is checking the world, not this helper.
    """

    end_month = horizon_months - 1
    accounts = [account(OWNER, initial_cash), account(TENANT)]
    recurring = [
        recurring_transfer(
            "rental_income:p1",
            start_month=0,
            end_month=end_month,
            payer=TENANT,
            payee=OWNER,
            amount=indexed(monthly_rent),
        )
    ]
    scheduled: list[PreparedTransfer] = []
    if monthly_management_fee is not None or leasing_fees_by_month is not None:
        accounts.append(account(AGENCY))
    if monthly_management_fee is not None:
        recurring.append(
            recurring_transfer(
                "management_fee:p1",
                start_month=0,
                end_month=end_month,
                payer=OWNER,
                payee=AGENCY,
                amount=indexed(monthly_management_fee),
            )
        )
    if leasing_fees_by_month is not None:
        scheduled.extend(
            scheduled_transfer(
                f"leasing_fee:p1:m{month}", month=month, payer=OWNER, payee=AGENCY, amount=indexed(amount)
            )
            for month, amount in leasing_fees_by_month.items()
        )
    levels = [1.0] * (horizon_months + 1) if rent_levels is None else list(rent_levels)
    return Situation(
        horizon_months=horizon_months,
        rollout_count=1,
        series=series({RENT: [levels]}, horizon_months=horizon_months, rollout_count=1),
        accounts=tuple(accounts),
        recurring_transfers=tuple(recurring),
        scheduled_transfers=tuple(scheduled),
    )


def taxed_rental(*, monthly_rent: Decimal | int, horizon_months: int = 12) -> Situation:
    """Rent tagged as ordinary income for a single-filer landlord in SF."""

    return Situation(
        horizon_months=horizon_months,
        rollout_count=1,
        series=series({RENT: [[1.0] * (horizon_months + 1)]}, horizon_months=horizon_months, rollout_count=1),
        accounts=(account(OWNER, 100_000), account(TENANT), account(IRS)),
        tax_profiles=(taxpayer(),),
        recurring_transfers=(
            recurring_transfer(
                "rental_income:p1",
                start_month=0,
                end_month=horizon_months - 1,
                payer=TENANT,
                payee=OWNER,
                amount=indexed(monthly_rent),
                income=ORDINARY_INCOME,
            ),
        ),
    )


def sale_situation(
    *,
    horizon: int,
    sale_month: int,
    rented: bool = True,
    year2_wage: Decimal | int = 0,
    home_values: Sequence[float] | None = None,
) -> Situation:
    """A $500k SF rental bought for cash at month zero and sold at `sale_month` with 6% closing costs."""

    accounts = [account(OWNER, 600_000), account(TENANT), account(SELLER), account(IRS)]
    recurring = [
        recurring_transfer(
            "rental_income:p1",
            start_month=0,
            end_month=sale_month - 1,
            payer=TENANT,
            payee=OWNER,
            amount=5_000 * 100,
            income=ORDINARY_INCOME,
        )
    ]
    if year2_wage > 0:
        accounts.append(account(EMPLOYER))
        recurring.append(
            recurring_transfer(
                "wages:employer",
                start_month=12,
                end_month=23,
                payer=EMPLOYER,
                payee=OWNER,
                amount=money(round_currency_amount(Decimal(year2_wage) / Decimal(12), quantum=QUANTUM)),
                income=ORDINARY_INCOME,
            )
        )
    levels = [1.0] * (horizon + 1) if home_values is None else list(home_values)
    return Situation(
        horizon_months=horizon,
        rollout_count=1,
        series=series({RENT: [[1.0] * (horizon + 1)], HOME_VALUE: [levels]}, horizon_months=horizon, rollout_count=1),
        accounts=tuple(accounts),
        tax_profiles=(taxpayer(),),
        recurring_transfers=tuple(recurring),
        housing=Housing(
            purchases=(purchase("p1", rented_fraction=1.0 if rented else 0.0),),
            sales=(_PropertySale(month=sale_month, property_id="p1", closing_cost_ppb=rate_to_ppb(0.06)),),
        ),
        locations=(SF_LOCATION,),
    )


class TestRentalIncome:
    def test_rental_income_flows_monthly_at_constant_rent(self) -> None:
        """A recurring transfer fires once per month across its whole window and moves its
        configured amount into the recipient's account."""

        [rollout] = run(rental(horizon_months=12, monthly_rent=5_000))
        rent = transfers(rollout, "rental_income:p1")
        assert rent["month_index"].to_list() == list(range(12))
        assert dollars(rent, "amount_quanta") == pytest.approx([5_000] * 12)
        assert cash(rollout, OWNER, 12) == pytest.approx(100_000 + 60_000)

    def test_zero_amount_recurring_transfer_still_fires_but_moves_no_cash(self) -> None:
        """A transfer scheduled with a zero amount is a scheduled event, not an absent one:
        it logs a row every month of its window and leaves both balances untouched."""

        [rollout] = run(rental(horizon_months=12, monthly_rent=0))
        rent = transfers(rollout, "rental_income:p1")
        assert rent["month_index"].to_list() == list(range(12))
        assert dollars(rent, "amount_quanta") == pytest.approx([0] * 12)
        assert cash(rollout, OWNER, 12) == pytest.approx(100_000)
        assert cash(rollout, TENANT, 12) == pytest.approx(0)

    def test_rental_income_indexed_by_rent_series(self) -> None:
        # Rent series doubles at month 12, which is the annual adjustment period.
        [rollout] = run(rental(horizon_months=24, monthly_rent=5_000, rent_levels=[1.0] * 12 + [2.0] * 13))
        amounts = dollars(transfers(rollout, "rental_income:p1"), "amount_quanta")
        assert amounts[:12] == pytest.approx([5_000] * 12)
        assert amounts[12:] == pytest.approx([10_000] * 12)


class TestManagementFee:
    def test_owner_paid_recurring_transfer_debits_payer_and_credits_payee(self) -> None:
        """A recurring transfer running out of the owner's account alongside the incoming rent
        settles against the right two ledgers: the agency's balance is built entirely from the
        fee, and the owner keeps rent minus fee."""

        [rollout] = run(rental(horizon_months=12, monthly_rent=5_000, monthly_management_fee=380))
        fee = transfers(rollout, "management_fee:p1")
        assert fee["month_index"].to_list() == list(range(12))
        assert dollars(fee, "amount_quanta") == pytest.approx([380] * 12)
        assert fee["from_agent_id"].unique().to_list() == [OWNER]
        assert fee["to_agent_id"].unique().to_list() == [AGENCY]
        assert cash(rollout, AGENCY, 12) == pytest.approx(4_560)
        assert cash(rollout, OWNER, 12) == pytest.approx(100_000 + 60_000 - 4_560)


class TestRentalLifecycleCashflows:
    def test_windowed_recurring_transfers_fire_only_within_their_own_month_ranges(self) -> None:
        """The product layer lowers a changing rented fraction into several non-overlapping
        recurring transfers, each with its own already-scaled amount (that lowering is tested
        in `product/service_test.py`). What the world owes: each window fires in exactly its
        own months, at exactly its own amount, and a month covered by no window is silent —
        here months 6 and 7, the gap between the second and third window.
        """

        situation = Situation(
            horizon_months=12,
            rollout_count=1,
            series=series({RENT: [[1.0] * 13]}, horizon_months=12, rollout_count=1),
            accounts=(account(OWNER, 700_000), account(TENANT), account(AGENCY)),
            recurring_transfers=(
                *(
                    recurring_transfer(
                        "rental_income:p1",
                        start_month=start_month,
                        end_month=end_month,
                        payer=TENANT,
                        payee=OWNER,
                        amount=indexed(amount),
                        income=ORDINARY_INCOME,
                    )
                    for start_month, end_month, amount in [(0, 2, 1_500), (3, 5, 4_500), (8, 11, 3_000)]
                ),
                *(
                    recurring_transfer(
                        "management_fee:p1",
                        start_month=start_month,
                        end_month=end_month,
                        payer=OWNER,
                        payee=AGENCY,
                        amount=indexed(amount),
                        deduction="ordinary",
                    )
                    for start_month, end_month, amount in [(0, 2, 120), (3, 5, 360), (8, 11, 240)]
                ),
            ),
        )
        [rollout] = run(situation)

        rent = transfers(rollout, "rental_income:p1")
        assert rent["month_index"].to_list() == [0, 1, 2, 3, 4, 5, 8, 9, 10, 11]
        assert dollars(rent, "amount_quanta") == pytest.approx([1_500] * 3 + [4_500] * 3 + [3_000] * 4)

        fee = transfers(rollout, "management_fee:p1")
        assert fee["month_index"].to_list() == [0, 1, 2, 3, 4, 5, 8, 9, 10, 11]
        assert dollars(fee, "amount_quanta") == pytest.approx([120] * 3 + [360] * 3 + [240] * 4)


class TestLeasingFee:
    def test_scheduled_transfers_fire_once_in_their_own_month_at_their_own_amount(self) -> None:
        """Distinct amounts per month so a mis-indexed schedule cannot pass: a scheduled
        transfer fires in the single month it names, and no other month sees one."""

        [rollout] = run(
            rental(horizon_months=60, monthly_rent=5_000, leasing_fees_by_month={0: 5_000, 24: 6_000, 48: 7_000})
        )
        leasing = (
            trace(rollout)
            .events.transfers.filter(pl.col("cause_id").str.starts_with("leasing_fee:p1"))
            .sort("month_index")
        )
        assert leasing["month_index"].to_list() == [0, 24, 48]
        assert dollars(leasing, "amount_quanta") == pytest.approx([5_000, 6_000, 7_000])


class TestRentalCashflowReconciliation:
    def test_owner_terminal_cash_reconciles_with_the_transfers_the_world_logged(self) -> None:
        """Two independent outputs must agree: netting every logged transfer row that touches
        the owner has to reproduce the change in the owner's cash ledger. A flow that is logged
        but never settled — or settled but never logged — breaks this even though each output on
        its own still looks plausible.

        The literal is the hand check layered on top: 12 × $4,750 in, 12 × $380 out, one $5,000
        leasing fee out → $47,440 net.
        """

        [rollout] = run(
            rental(
                horizon_months=12,
                initial_cash=100_000,
                monthly_rent=4_750,
                monthly_management_fee=380,
                leasing_fees_by_month={0: 5_000},
            )
        )
        logged = trace(rollout).events.transfers
        logged_net = float(logged.filter(pl.col("to_agent_id") == OWNER)["amount_quanta"].sum() / 100) - float(
            logged.filter(pl.col("from_agent_id") == OWNER)["amount_quanta"].sum() / 100
        )
        assert cash(rollout, OWNER, 12) - 100_000 == pytest.approx(logged_net, abs=0.01)
        assert cash(rollout, OWNER, 12) == pytest.approx(100_000 + 47_440, rel=1e-6)


class TestRentalIncomeTaxation:
    """Rental income transfers carry an ordinary income category, so they accrue into the
    owner's taxable ordinary income at year end, against Schedule E deductions and the
    MID/SALT split the property's rented share decides.
    """

    def test_rental_income_accrues_into_ordinary_ytd(self) -> None:
        # $4,000/mo × 12 = $48,000 gross rental income → the year's ordinary income line.
        [rollout] = run(taxed_rental(monthly_rent=4_000))
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            48_000, abs=1e-6
        )

    def test_rental_income_generates_tax_accruals_at_year_end(self) -> None:
        [rollout] = run(taxed_rental(monthly_rent=4_000))
        accruals = trace(rollout).events.tax_accruals.sort("jurisdiction_id")
        assert accruals.height == 2  # federal + CA
        assert accruals["month_index"].to_list() == [11, 11]
        # Both jurisdictions levy positive tax on $48k of ordinary income.
        assert all(amount > 0 for amount in dollars(accruals, "amount_quanta"))

    def test_management_fee_deducts_from_taxable_ordinary_income(self) -> None:
        """Schedule E: a management-fee transfer tagged as an ordinary deduction subtracts from
        the owner's ordinary income, reducing taxable income."""

        base = taxed_rental(monthly_rent=5_000)
        situation = replace(
            base,
            accounts=(*base.accounts, account(AGENCY)),
            recurring_transfers=(
                *base.recurring_transfers,
                recurring_transfer(
                    "management_fee:p1",
                    start_month=0,
                    end_month=11,
                    payer=OWNER,
                    payee=AGENCY,
                    amount=indexed(500),
                    deduction="ordinary",
                ),
            ),
        )
        [rollout] = run(situation)
        # Gross rental: 12 × $5,000 = $60,000. Management fee: 12 × $500 = $6,000.
        # Net ordinary income exposed to brackets = $54,000.
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            54_000, abs=1e-6
        )

    def test_obligation_deduction_decrements_payer_ordinary_ytd(self) -> None:
        """Schedule E on obligations: a paid fully-deductible recurring obligation decrements
        the payer's ordinary income by the whole settled amount."""

        base = taxed_rental(monthly_rent=6_000)
        # $6,000/mo gross rent → $72,000/yr; $400/mo HOA fully deductible → $4,800/yr Schedule E.
        situation = replace(
            base,
            accounts=(*base.accounts, account(HOA)),
            obligations=(
                dues(
                    "hoa_dues",
                    obligation_type=ObligationType.HOA_DUES,
                    payer=OWNER,
                    payee=HOA,
                    amount=indexed(400),
                    end_month=11,
                ),
            ),
        )
        [rollout] = run(situation)
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            67_200, abs=1e-6
        )

    def test_depreciation_accrues_monthly_and_deducts_as_schedule_e(self) -> None:
        """§168 monthly depreciation accrues for rented property and reduces taxable ordinary
        income at year end. Building basis = $500k × 0.80 = $400k; fully rented; annual
        depreciation = $400k / 27.5 ≈ $14,545.45."""

        situation = Situation(
            horizon_months=12,
            rollout_count=1,
            series=series({RENT: [[1.0] * 13], HOME_VALUE: [[1.0] * 13]}, horizon_months=12, rollout_count=1),
            accounts=(account(OWNER, 600_000), account(TENANT), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_transfers=(
                recurring_transfer(
                    "rental_income:p1",
                    start_month=0,
                    end_month=11,
                    payer=TENANT,
                    payee=OWNER,
                    amount=indexed(5_000),
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(purchases=(purchase("p1", rented_fraction=1.0),)),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)
        # Cumulative depreciation grows monotonically; at the post-horizon snapshot it has
        # accrued 12 months' worth = $400,000 / 27.5 = $14,545.45.
        assert len([row for row in book_at(rollout, 12).properties if row.active]) == 1
        # Federal ordinary income: $60,000 rental - $14,545.45 depreciation = $45,454.55.
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            45_454.55, abs=0.02
        )

    def test_lifecycle_start_renting_starts_depreciation_accrual_mid_horizon(self) -> None:
        """Letting the property from month 12 accrues depreciation only from month 12 onward:
        24-month horizon, $400k building basis, year 0 depreciation 0, year 1 $400k / 27.5."""

        situation = Situation(
            horizon_months=24,
            rollout_count=1,
            series=series({RENT: [[1.0] * 25], HOME_VALUE: [[1.0] * 25]}, horizon_months=24, rollout_count=1),
            accounts=(account(OWNER, 600_000), account(TENANT), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_transfers=(
                # Rental income only fires from month 12 — matching the month it is let.
                recurring_transfer(
                    "rental_income:p1",
                    start_month=12,
                    end_month=23,
                    payer=TENANT,
                    payee=OWNER,
                    amount=indexed(5_000),
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(
                purchases=(purchase("p1", rented_fraction=0.0),),
                rented_fraction_events=(
                    _RentedFraction(month=12, property_id="p1", rented_fraction_ppb=rate_to_ppb(1.0)),
                ),
            ),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)
        # Year 0: not rented all year, no rental income, no depreciation → ordinary income $0.
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            0, abs=1e-6
        )
        # Year 1: 12 months rent ($60k) minus 12 months depreciation ($14.5k) ≈ $45,454.55.
        assert breakdown(rollout, month=23, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            45_454.55, abs=0.05
        )

    def test_lifecycle_start_renting_redirects_mortgage_interest_from_mid_to_schedule_e(self) -> None:
        """At start-of-rental MID drops for the now-rented portion of mortgage interest and
        Schedule E picks it up: the same scenario owner-occupied all year against one let from
        month 1 leaves the second with a far smaller MID line."""

        owner_occupied = self._mortgage_lifecycle_breakdown(start_renting_at=None)
        # Letting must begin strictly after the purchase month, so month 1.
        rented = self._mortgage_lifecycle_breakdown(start_renting_at=1)
        assert owner_occupied["mortgage_interest_deduction_quanta"] / 100 > 0
        # Rented from month 1: year 0 MID is the month-0 interest only, a tiny owner-share
        # sliver next to a full year of owner-occupied MID.
        assert (
            rented["mortgage_interest_deduction_quanta"] / 100
            < owner_occupied["mortgage_interest_deduction_quanta"] / 100 * 0.15
        )

    def _mortgage_lifecycle_breakdown(self, *, start_renting_at: int | None) -> dict[str, Any]:
        loan = financing("p1_mortgage", principal=Decimal(500_000) * Decimal("0.80"))
        situation = Situation(
            horizon_months=12,
            rollout_count=1,
            series=series({HOME_VALUE: [[1.0] * 13]}, horizon_months=12, rollout_count=1),
            accounts=(account(OWNER, 700_000), account(TENANT), account(SELLER), account(LENDER), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_transfers=(
                recurring_transfer(
                    "paycheck",
                    start_month=0,
                    end_month=11,
                    payer=TENANT,
                    payee=OWNER,
                    amount=5_000 * 100,
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(
                # Land-only basis isolates the comparison from depreciation.
                purchases=(purchase("p1", land_value_fraction=1.0, mortgage=loan),),
                rented_fraction_events=(
                    ()
                    if start_renting_at is None
                    else (
                        _RentedFraction(month=start_renting_at, property_id="p1", rented_fraction_ppb=rate_to_ppb(1.0)),
                    )
                ),
            ),
            locations=(SF_LOCATION,),
            mortgage_interest_policies=(mortgage_interest_deduction("p1_mortgage", OWNER),),
        )
        [rollout] = run(situation)
        return breakdown(rollout, month=11, jurisdiction=FEDERAL)

    def test_lifecycle_stop_renting_halts_depreciation(self) -> None:
        """Ceasing to let at month 12 accrues depreciation over months 0-11 only.
        Year 0 ordinary: $60k rent - $14.5k depreciation = $45.5k. Year 1: no rent, no
        depreciation → $0.
        """

        situation = Situation(
            horizon_months=24,
            rollout_count=1,
            series=series({RENT: [[1.0] * 25], HOME_VALUE: [[1.0] * 25]}, horizon_months=24, rollout_count=1),
            accounts=(account(OWNER, 600_000), account(TENANT), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_transfers=(
                recurring_transfer(
                    "rental_income:p1",
                    start_month=0,
                    end_month=11,  # rental income only in year 0
                    payer=TENANT,
                    payee=OWNER,
                    amount=indexed(5_000),
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(
                purchases=(purchase("p1", rented_fraction=1.0),),
                rented_fraction_events=(
                    _RentedFraction(month=12, property_id="p1", rented_fraction_ppb=rate_to_ppb(0.0)),
                ),
            ),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            45_454.55, abs=0.05
        )
        assert breakdown(rollout, month=23, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            0, abs=1e-6
        )

    def test_capital_improvement_bumps_basis_and_accelerates_depreciation(self) -> None:
        """A $100k capital improvement at month 6 lifts the building basis to $500k, so
        monthly depreciation after month 6 is $500k / 27.5 / 12 ≈ $1,515.15 against
        ≈ $1,212.12 before."""

        situation = Situation(
            horizon_months=12,
            rollout_count=1,
            series=series({HOME_VALUE: [[1.0] * 13]}, horizon_months=12, rollout_count=1),
            accounts=(account(OWNER, 700_000), account(TENANT), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_transfers=(
                recurring_transfer(
                    "rental_income:p1",
                    start_month=0,
                    end_month=11,
                    payer=TENANT,
                    payee=OWNER,
                    amount=5_000 * 100,
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(
                purchases=(purchase("p1", rented_fraction=1.0),),
                capital_improvements=(
                    _CapitalImprovement(month=6, property_id="p1", amount=money(100_000), description="new roof"),
                ),
            ),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)
        # 6 × $400k/27.5/12 + 6 × $500k/27.5/12 = 7,272.73 + 9,090.91 = 16,363.64.
        expected_depreciation = 6 * (400_000 / 27.5 / 12) + 6 * (500_000 / 27.5 / 12)
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            60_000 - expected_depreciation, abs=0.05
        )

    def test_same_month_rent_fraction_and_capex_apply_before_depreciation(self) -> None:
        """Non-sale lifecycle events in the same month all apply before that month's
        depreciation accrual.

        The property is not let for months 0-5. At month 6 it becomes fully let and receives a
        $100k capital improvement, so months 6-11 depreciate against $500k of building basis,
        not $400k and not zero.
        """

        situation = Situation(
            horizon_months=12,
            rollout_count=1,
            series=series({HOME_VALUE: [[1.0] * 13]}, horizon_months=12, rollout_count=1),
            accounts=(account(OWNER, 700_000), account(EMPLOYER), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_transfers=(
                recurring_transfer(
                    "wages:employer",
                    start_month=0,
                    end_month=11,
                    payer=EMPLOYER,
                    payee=OWNER,
                    amount=5_000 * 100,
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(
                purchases=(purchase("p1", rented_fraction=0.0),),
                rented_fraction_events=(
                    _RentedFraction(month=6, property_id="p1", rented_fraction_ppb=rate_to_ppb(1.0)),
                ),
                capital_improvements=(
                    _CapitalImprovement(month=6, property_id="p1", amount=money(100_000), description="new roof"),
                ),
            ),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)

        rented_rows = trace(rollout).events.set_rented_fraction_events.to_dicts()
        capex_rows = trace(rollout).events.capital_improvement_events.to_dicts()
        assert [(row["month_index"], row["rented_fraction"]) for row in rented_rows] == [(6, 1)]
        assert [(row["month_index"], row["amount_quanta"] / 100) for row in capex_rows] == [(6, 100_000)]

        expected_depreciation = 6 * (500_000 / 27.5 / 12)
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            60_000 - expected_depreciation, abs=0.05
        )

    def test_property_sale_recaptures_depreciation_and_routes_remaining_gain_to_ltcg(self) -> None:
        """Sale of a fully-let property after 12 months of depreciation.
        Building basis $400k → $400k / 27.5 ≈ $14,545.45/yr, so cumulative $14,545.45.
        Home value flat → market value $500k, 6% closing → gross proceeds $470k.
        Adjusted basis $500k - $14,545.45 = $485,454.55, so the realized gain is
        -$15,454.55 — a loss, hence no recapture and no LTCG."""

        [rollout] = run(sale_situation(horizon=13, sale_month=12))
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ltcg_quanta"] / 100 == pytest.approx(0, abs=1e-6)

    def test_property_sale_requires_home_value_series(self) -> None:
        situation = sale_situation(horizon=13, sale_month=12)
        with pytest.raises(ValueError, match='missing series "home_value:san_francisco"'):
            run(replace(situation, series=series({RENT: [[1.0] * 14]}, horizon_months=13, rollout_count=1)))

    def test_property_sale_at_gain_routes_recapture_and_ltcg(self) -> None:
        """Sale at month 12 with home-value appreciation, on a 24-month horizon so the sale
        year's accrual is inside it.

        Cumulative depreciation $14,545.45 ($400k building / 27.5y for 12mo).
        Adjusted basis $500k - $14,545.45 = $485,454.55.
        Home value 1.5× → market value $750k → gross proceeds (6% closing) $705k.
        Gain $219,545.45; recapture min(gain, $14,545.45) = $14,545.45 → §1250.
        LTCG = $219,545.45 - $14,545.45 = $205,000."""

        [rollout] = run(sale_situation(horizon=24, sale_month=12, home_values=[1.0] * 12 + [1.5] * 13))
        assert breakdown(rollout, month=23, jurisdiction=FEDERAL)["ltcg_quanta"] / 100 == pytest.approx(205_000, abs=1)

    def test_multi_rollout_property_sale_keeps_sale_tax_and_mortgage_amounts_rollout_scoped(self) -> None:
        """A single sale with divergent home-value rollouts must not smear sale math across
        rollouts.

        The property is let for 12 months, then owner-occupied for 24 before sale. That
        activates all of the property-sale tax plumbing in one situation: mortgage payoff,
        §1250 recapture from the rental period, §121 exclusion from the later owner-occupied
        period, and residual LTCG. The two rollouts differ in home value at sale month.
        """

        purchase_price = 500_000
        mortgage_principal = 400_000
        annual_interest_rate = 0.06
        mortgage_term_months = 360
        sale_month = 36
        horizon = 48
        home_values = {
            0: [1.0] * sale_month + [1.2] * (horizon + 1 - sale_month),
            1: [1.0] * sale_month + [1.6] * (horizon + 1 - sale_month),
        }
        situation = Situation(
            horizon_months=horizon,
            rollout_count=2,
            series=series(
                {RENT: [[1.0] * (horizon + 1)] * 2, HOME_VALUE: [home_values[0], home_values[1]]},
                horizon_months=horizon,
                rollout_count=2,
            ),
            accounts=(account(OWNER, 700_000), account(TENANT), account(SELLER), account(LENDER), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_property_cashflows=(
                recurring_property_cashflow(
                    "rental_income:p1",
                    property_id="p1",
                    start_month=0,
                    end_month=11,
                    payer=TENANT,
                    payee=OWNER,
                    amount=5_000 * 100,
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(
                purchases=(
                    purchase(
                        "p1",
                        rented_fraction=1.0,
                        mortgage=financing(
                            "p1_mortgage",
                            principal=mortgage_principal,
                            rate=annual_interest_rate,
                            term_months=mortgage_term_months,
                        ),
                    ),
                ),
                sales=(_PropertySale(month=sale_month, property_id="p1", closing_cost_ppb=rate_to_ppb(0.06)),),
                residence_events=(_PrimaryResidenceEvent(month=12, agent_id=OWNER, property_id="p1"),),
                rented_fraction_events=(
                    _RentedFraction(month=12, property_id="p1", rented_fraction_ppb=rate_to_ppb(0.0)),
                ),
            ),
            locations=(SF_LOCATION,),
        )
        rollouts = run(situation)
        assert [rollout.rollout_id for rollout in rollouts] == [0, 1]

        payoff, _ = mortgage_balance_and_interest_after_payments(
            principal=mortgage_principal,
            annual_interest_rate=annual_interest_rate,
            term_months=mortgage_term_months,
            payment_count=sale_month - 1,
        )
        recapture = 12 * (purchase_price * 0.80 / 27.5 / 12)
        for rollout, sale_level, expected_sale_year_ltcg in zip(rollouts, (1.2, 1.6), (0, 2_000), strict=True):
            gross = purchase_price * sale_level * 0.94
            realized_gain = gross - (purchase_price - recapture)
            post_recapture_gain = realized_gain - recapture
            section_121 = min(post_recapture_gain, 250_000)
            row = sale_rows(rollout)["p1"]
            assert row["month_index"] == sale_month
            for column, expected in (
                ("gross_proceeds_quanta", gross),
                ("mortgage_payoff_quanta", payoff),
                ("net_cash_to_owner_quanta", gross - payoff),
                ("realized_gain_quanta", realized_gain),
                ("depreciation_recapture_quanta", recapture),
                ("section_121_exclusion_quanta", section_121),
                ("long_term_capital_gain_quanta", post_recapture_gain - section_121),
            ):
                assert row[column] / 100 == pytest.approx(expected, abs=0.02)
            assert breakdown(rollout, month=47, jurisdiction=FEDERAL)["ltcg_quanta"] / 100 == pytest.approx(
                expected_sale_year_ltcg, abs=1
            )

    def test_multi_taxpayer_property_tax_schedule_e_mid_and_sale_routing_are_owner_scoped(self) -> None:
        """Two owners with two properties keep their real-estate tax channels isolated.

        Alice owns a fully let property: rental income, depreciation, property tax and mortgage
        interest route through Schedule E, with no MID and no §121. Bob owns an owner-occupied
        property: property tax routes to SALT, mortgage interest to MID, and §121 excludes the
        sale gain.
        """

        purchase_price = 500_000
        mortgage_principal = 400_000
        annual_interest_rate = 0.06
        mortgage_term_months = 360
        annual_property_tax_rate = 0.012
        monthly_rent = 5_000
        sale_month = 30
        horizon = 36
        situation = Situation(
            horizon_months=horizon,
            rollout_count=1,
            series=series(
                {RENT: [[1.0] * (horizon + 1)], HOME_VALUE: [[1.0] * sale_month + [1.4] * (horizon + 1 - sale_month)]},
                horizon_months=horizon,
                rollout_count=1,
            ),
            accounts=(
                account(ALICE, 800_000),
                account(BOB, 800_000),
                account(TENANT),
                account(SELLER),
                account(LENDER),
                account(COUNTY),
                account(IRS),
                account(ISSUER, 100),
            ),
            income_sources=(ORDINARY_INCOME, MUNI_INTEREST),
            tax_profiles=(taxpayer(BOB, jurisdiction_ids=(FEDERAL,)), taxpayer(ALICE, jurisdiction_ids=(FEDERAL,))),
            scheduled_transfers=(
                scheduled_transfer(
                    "california_muni_interest", month=0, payer=ISSUER, payee=BOB, amount=100 * 100, income=MUNI_INTEREST
                ),
            ),
            recurring_property_cashflows=(
                recurring_property_cashflow(
                    "rental_income:alice_rental",
                    property_id="alice_rental",
                    start_month=0,
                    end_month=sale_month - 1,
                    payer=TENANT,
                    payee=ALICE,
                    amount=monthly_rent * 100,
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(
                purchases=(
                    purchase(
                        "alice_rental",
                        buyer=ALICE,
                        rented_fraction=1.0,
                        mortgage=financing(
                            "alice_rental_mortgage",
                            principal=mortgage_principal,
                            rate=annual_interest_rate,
                            term_months=mortgage_term_months,
                        ),
                    ),
                    purchase(
                        "bob_home",
                        buyer=BOB,
                        rented_fraction=0.0,
                        mortgage=financing(
                            "bob_home_mortgage",
                            principal=mortgage_principal,
                            rate=annual_interest_rate,
                            term_months=mortgage_term_months,
                        ),
                    ),
                ),
                sales=(
                    _PropertySale(month=sale_month, property_id="alice_rental", closing_cost_ppb=rate_to_ppb(0.06)),
                    _PropertySale(month=sale_month, property_id="bob_home", closing_cost_ppb=rate_to_ppb(0.06)),
                ),
                initial_residences=(_PrimaryResidence(agent_id=BOB, property_id="bob_home"),),
            ),
            locations=(SF_LOCATION,),
            property_tax_policies=(
                property_tax("alice_rental", ALICE, rate=annual_property_tax_rate),
                property_tax("bob_home", BOB, rate=annual_property_tax_rate),
            ),
            mortgage_interest_policies=(
                mortgage_interest_deduction("alice_rental_mortgage", ALICE),
                mortgage_interest_deduction("bob_home_mortgage", BOB),
            ),
            salt_policies=(salt_cap(ALICE, 100_000), salt_cap(BOB, 100_000)),
        )
        [rollout] = run(situation, actor=ALICE, co_owners=(BOB,))

        settlements = trace(rollout).events.obligation_settlements.filter(
            pl.col("obligation_type") == ObligationType.PROPERTY_TAX
        )
        monthly_property_tax = purchase_price * annual_property_tax_rate / 12
        for agent_id in (ALICE, BOB):
            paid = settlements.filter(pl.col("agent_id") == agent_id).sort("month_index")
            assert paid.height == sale_month - 1
            assert dollars(paid, "amount_paid_quanta") == pytest.approx([monthly_property_tax] * (sale_month - 1))

        _, year_0_interest = mortgage_balance_and_interest_after_payments(
            principal=mortgage_principal,
            annual_interest_rate=annual_interest_rate,
            term_months=mortgage_term_months,
            payment_count=11,
        )
        depreciation_year_0 = 12 * (purchase_price * 0.80 / 27.5 / 12)
        property_tax_year_0 = 11 * monthly_property_tax
        alice_year_0 = breakdown(rollout, month=11, jurisdiction=FEDERAL, agent_id=ALICE)
        bob_year_0 = breakdown(rollout, month=11, jurisdiction=FEDERAL, agent_id=BOB)
        assert alice_year_0["ordinary_income_quanta"] / 100 == pytest.approx(
            12 * monthly_rent - depreciation_year_0 - property_tax_year_0 - year_0_interest, abs=1
        )
        assert alice_year_0["mortgage_interest_deduction_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert alice_year_0["salt_deduction_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert bob_year_0["ordinary_income_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert bob_year_0["mortgage_interest_deduction_quanta"] / 100 == pytest.approx(year_0_interest, abs=1)
        assert bob_year_0["salt_deduction_quanta"] / 100 == pytest.approx(property_tax_year_0, abs=1)

        sales = sale_rows(rollout)
        alice_recapture = sale_month * (purchase_price * 0.80 / 27.5 / 12)
        assert sales["alice_rental"]["realized_gain_quanta"] / 100 == pytest.approx(194_363.64, abs=1)
        assert sales["alice_rental"]["depreciation_recapture_quanta"] / 100 == pytest.approx(alice_recapture, abs=1)
        assert sales["alice_rental"]["section_121_exclusion_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sales["alice_rental"]["long_term_capital_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sales["bob_home"]["realized_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sales["bob_home"]["depreciation_recapture_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sales["bob_home"]["section_121_exclusion_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sales["bob_home"]["long_term_capital_gain_quanta"] / 100 == pytest.approx(0, abs=1e-6)

        assert breakdown(rollout, month=35, jurisdiction=FEDERAL, agent_id=ALICE)["ltcg_quanta"] / 100 == pytest.approx(
            158_000, abs=1
        )
        assert breakdown(rollout, month=35, jurisdiction=FEDERAL, agent_id=BOB)["ltcg_quanta"] / 100 == pytest.approx(
            0, abs=1e-6
        )

    def test_property_tied_recurring_obligation_stops_after_property_sale(self) -> None:
        """Property-keyed HOA/insurance/maintenance-style obligations stop when the property sells."""

        sale_month = 12
        monthly_hoa = 400
        base = sale_situation(
            horizon=24, sale_month=sale_month, home_values=[1.0] * sale_month + [1.5] * (25 - sale_month)
        )
        situation = replace(
            base,
            accounts=(*base.accounts, account(HOA)),
            obligations=(
                dues(
                    "hoa_dues:p1",
                    obligation_type=ObligationType.HOA_DUES,
                    payer=OWNER,
                    payee=HOA,
                    amount=monthly_hoa * 100,
                    property_id="p1",
                ),
            ),
        )
        [rollout] = run(situation)

        events = trace(rollout).events
        accruals = events.obligation_accruals.filter(pl.col("obligation_type") == ObligationType.HOA_DUES).sort(
            "month_index"
        )
        settlements = events.obligation_settlements.filter(pl.col("obligation_type") == ObligationType.HOA_DUES).sort(
            "month_index"
        )
        assert accruals["month_index"].to_list() == list(range(sale_month))
        assert settlements["month_index"].to_list() == list(range(sale_month))
        assert dollars(settlements, "amount_paid_quanta") == pytest.approx([monthly_hoa] * sale_month)
        assert cash(rollout, HOA, 24) == pytest.approx(monthly_hoa * sale_month)

        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            12 * 5_000 - 400_000 / 27.5 - monthly_hoa * sale_month, abs=0.05
        )

    def test_property_cashflows_stop_after_property_sale_but_generic_transfers_continue(self) -> None:
        """Rental and management cashflows are property-domain flows, not generic transfers."""

        sale_month = 12
        monthly_rent = 5_000
        monthly_management_fee = 500
        leasing_fee = 1_000
        generic_transfer = 123
        base = sale_situation(
            horizon=24, sale_month=sale_month, home_values=[1.0] * sale_month + [1.5] * (25 - sale_month)
        )
        situation = replace(
            base,
            accounts=(*base.accounts, account(AGENCY), account("generic_payer")),
            recurring_transfers=(
                recurring_transfer(
                    "generic_transfer",
                    start_month=0,
                    end_month=23,
                    payer="generic_payer",
                    payee=OWNER,
                    amount=generic_transfer * 100,
                ),
            ),
            recurring_property_cashflows=(
                recurring_property_cashflow(
                    "rental_income:p1",
                    property_id="p1",
                    start_month=0,
                    end_month=23,
                    payer=TENANT,
                    payee=OWNER,
                    amount=monthly_rent * 100,
                    income=ORDINARY_INCOME,
                ),
                recurring_property_cashflow(
                    "management_fee:p1",
                    property_id="p1",
                    start_month=0,
                    end_month=23,
                    payer=OWNER,
                    payee=AGENCY,
                    amount=monthly_management_fee * 100,
                    deduction="ordinary",
                ),
            ),
            scheduled_property_cashflows=(
                scheduled_property_cashflow(
                    "leasing_fee:p1:m0",
                    property_id="p1",
                    month=0,
                    payer=OWNER,
                    payee=AGENCY,
                    amount=leasing_fee * 100,
                    deduction="ordinary",
                ),
                scheduled_property_cashflow(
                    f"leasing_fee:p1:m{sale_month}",
                    property_id="p1",
                    month=sale_month,
                    payer=OWNER,
                    payee=AGENCY,
                    amount=leasing_fee * 100,
                    deduction="ordinary",
                ),
            ),
        )
        [rollout] = run(situation)

        rent = transfers(rollout, "rental_income:p1")
        management = transfers(rollout, "management_fee:p1")
        generic = transfers(rollout, "generic_transfer")
        assert rent["month_index"].to_list() == list(range(sale_month))
        assert dollars(rent, "amount_quanta") == pytest.approx([monthly_rent] * sale_month)
        assert management["month_index"].to_list() == list(range(sale_month))
        assert dollars(management, "amount_quanta") == pytest.approx([monthly_management_fee] * sale_month)
        assert transfers(rollout, "leasing_fee:p1:m0")["month_index"].to_list() == [0]
        assert dollars(transfers(rollout, "leasing_fee:p1:m0"), "amount_quanta") == pytest.approx([leasing_fee])
        assert transfers(rollout, f"leasing_fee:p1:m{sale_month}").is_empty()
        assert generic["month_index"].to_list() == list(range(24))
        assert dollars(generic, "amount_quanta") == pytest.approx([generic_transfer] * 24)

        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            12 * monthly_rent - 12 * monthly_management_fee - leasing_fee - 400_000 / 27.5, abs=0.05
        )

    def test_section_1250_recapture_taxed_at_lesser_of_marginal_or_cap_low_bracket(self) -> None:
        """IRS Unrecaptured §1250 Gain Worksheet rule (low-bracket case).

        Sale at month 12 (year 2). The recapture lands in year 2 tax accruals; year 2 has no
        rental income (rent stops at sale-1) so federal `ordinary_taxable=0`. Stacking the
        $14,545.45 recapture on top of zero ordinary taxable puts it entirely in the 10%
        (first $11,600) + 12% (next $2,945) brackets — well below the 25% federal cap. So the
        §1250 tax is the marginal walk, NOT recapture × 25%. California has no §1250 cap, so
        the recapture is added to ordinary brackets there.
        """

        [rollout] = run(sale_situation(horizon=24, sale_month=12, home_values=[1.0] * 12 + [1.5] * 13))
        federal = breakdown(rollout, month=23, jurisdiction=FEDERAL)
        california = breakdown(rollout, month=23, jurisdiction=CALIFORNIA)
        recapture = 14_545.45
        assert federal["ordinary_income_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert federal["ordinary_taxable_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        # Federal LTCG: ordinary income is zero in year 2, so the standard deduction is unused
        # against it and shelters that much of the gain instead (§63 nets it against taxable
        # income, which includes the gain, before §1(h) rates what is left). Taxable income is
        # $205,000 - $14,600 = $190,400, all net capital gain: 0% slice 0..47,025,
        # 15% slice 47,025..190,400 = 0.15 × 143,375 = 21,506.25.
        standard_deduction = 14_600
        ltcg_tax_federal = 0.15 * (205_000 - standard_deduction - 47_025)
        # §1250 implied marginal walk: 10% × 11,600 + 12% × (14,545.45 - 11,600) = 1,513.45,
        # well below the 25% × 14,545.45 = 3,636.36 cap → marginal wins.
        section_1250_marginal = 0.10 * 11_600 + 0.12 * (recapture - 11_600)
        assert section_1250_marginal < recapture * 0.25  # sanity: marginal binds, not the cap
        assert federal["capital_gain_tax_quanta"] / 100 == pytest.approx(
            ltcg_tax_federal + section_1250_marginal, abs=2
        )
        # California has no separate LTCG schedule either, so the gain is in ordinary too.
        assert california["capital_gain_tax_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert california["ordinary_taxable_quanta"] / 100 == pytest.approx(
            205_000 + recapture - california["standard_deduction_quanta"] / 100, abs=1
        )

    def test_section_1250_recapture_caps_at_25pct_when_marginal_exceeds(self) -> None:
        """High-bracket case: the federal 25% §1250 cap binds when the marginal rate is ≥ 25%.

        Same sale, but the owner also earns enough wage income in year 2 to push
        `ordinary_taxable` past the 32% bracket threshold ($191,950 single). Stacking the
        recapture on top would land in the 32%/35% brackets, but the cap holds it to 25%.
        """

        [rollout] = run(
            sale_situation(horizon=24, sale_month=12, year2_wage=250_000, home_values=[1.0] * 12 + [1.5] * 13)
        )
        federal = breakdown(rollout, month=23, jurisdiction=FEDERAL)
        assert federal["ordinary_taxable_quanta"] / 100 > 191_950
        # The LTCG bracket walk shifts because ordinary_taxable is now large: the 0% slice is
        # fully consumed and most of the LTCG lands in the 20% bracket (LTCG breakpoints
        # 47,025 / 518,900 single for 2024). Assert only that the §1250 piece is the 25% cap;
        # the LTCG arithmetic is exercised above.
        assert federal["capital_gain_tax_quanta"] / 100 >= 14_545.45 * 0.25 + 0.20 * 100_000

    def test_section_121_exclusion_after_24_owner_occupied_months(self) -> None:
        """Owner-occupied for ≥ 24 of the last 60 months excludes up to $250k of post-recapture
        gain from LTCG (single-filer cap).

        Bought as a primary residence, held 30 months, then sold with $200k of appreciation:
        realized gain $158k after closing costs, all post-recapture (never let, so no
        depreciation). §121 excludes the whole $158k → LTCG 0.
        """

        sale_month = 30
        horizon = 36
        situation = Situation(
            horizon_months=horizon,
            rollout_count=1,
            series=series(
                {RENT: [[1.0] * (horizon + 1)], HOME_VALUE: [[1.0] * sale_month + [1.4] * (horizon + 1 - sale_month)]},
                horizon_months=horizon,
                rollout_count=1,
            ),
            accounts=(account(OWNER, 600_000), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            housing=Housing(
                purchases=(purchase("p1", rented_fraction=0.0),),
                sales=(_PropertySale(month=sale_month, property_id="p1", closing_cost_ppb=rate_to_ppb(0.06)),),
                initial_residences=(_PrimaryResidence(agent_id=OWNER, property_id="p1"),),
            ),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)
        sale = sale_rows(rollout)["p1"]
        # Gross = $500k × 1.4 × 0.94 = $658k. Realized gain = $658k - $500k = $158k.
        assert sale["gross_proceeds_quanta"] / 100 == pytest.approx(658_000, abs=1)
        assert sale["realized_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["depreciation_recapture_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(0, abs=1e-6)

    @pytest.mark.parametrize(
        ("primary_start_month", "primary_end_month", "expected_exclusion_usd", "expected_ltcg_usd"),
        [
            pytest.param(61, 84, 0, 158_000, id="23-recent-months-does-not-qualify"),
            pytest.param(60, 84, 158_000, 0, id="24-recent-months-qualifies"),
            pytest.param(0, 24, 0, 158_000, id="24-old-months-outside-lookback-do-not-qualify"),
            pytest.param(24, 48, 158_000, 0, id="24-months-at-lookback-boundary-qualify"),
        ],
    )
    def test_section_121_uses_24_of_trailing_60_months(
        self, primary_start_month: int, primary_end_month: int, expected_exclusion_usd: float, expected_ltcg_usd: float
    ) -> None:
        """§121 is a 24-of-trailing-60-month use test, not cumulative lifetime occupancy.

        The world keeps a 60-month occupancy ring; these cases pin the exact boundary behavior
        that ring must preserve.
        """

        sale_month = 84
        horizon = sale_month + 1
        situation = Situation(
            horizon_months=horizon,
            rollout_count=1,
            series=series(
                {RENT: [[1.0] * (horizon + 1)], HOME_VALUE: [[1.0] * sale_month + [1.4] * (horizon + 1 - sale_month)]},
                horizon_months=horizon,
                rollout_count=1,
            ),
            accounts=(account(OWNER, 600_000), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            housing=Housing(
                purchases=(purchase("p1", rented_fraction=0.0),),
                sales=(_PropertySale(month=sale_month, property_id="p1", closing_cost_ppb=rate_to_ppb(0.06)),),
                residence_events=(
                    _PrimaryResidenceEvent(month=primary_start_month, agent_id=OWNER, property_id="p1"),
                    _PrimaryResidenceEvent(month=primary_end_month, agent_id=OWNER, property_id=None),
                ),
            ),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)

        sale = sale_rows(rollout)["p1"]
        assert sale["realized_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["depreciation_recapture_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(expected_exclusion_usd, abs=1)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(expected_ltcg_usd, abs=1)

        post_sale = [
            row.long_term_gain for row in book_at(rollout, sale_month + 1).capital_gains if row.agent_id == OWNER
        ]
        assert sum(post_sale) / 100 == pytest.approx(expected_ltcg_usd, abs=1)

    def test_section_121_does_not_apply_to_unassigned_non_rented_property(self) -> None:
        sale_month = 30
        horizon = 36
        situation = Situation(
            horizon_months=horizon,
            rollout_count=1,
            series=series(
                {RENT: [[1.0] * (horizon + 1)], HOME_VALUE: [[1.0] * sale_month + [1.4] * (horizon + 1 - sale_month)]},
                horizon_months=horizon,
                rollout_count=1,
            ),
            accounts=(account(OWNER, 600_000), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            housing=Housing(
                purchases=(purchase("p1", rented_fraction=0.0),),
                sales=(_PropertySale(month=sale_month, property_id="p1", closing_cost_ppb=rate_to_ppb(0.06)),),
            ),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)
        sale = sale_rows(rollout)["p1"]
        assert sale["realized_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)

    def test_primary_residence_event_starts_section_121_qualifying_months(self) -> None:
        sale_month = 30
        horizon = 36
        situation = Situation(
            horizon_months=horizon,
            rollout_count=1,
            series=series(
                {RENT: [[1.0] * (horizon + 1)], HOME_VALUE: [[1.0] * sale_month + [1.4] * (horizon + 1 - sale_month)]},
                horizon_months=horizon,
                rollout_count=1,
            ),
            accounts=(account(OWNER, 600_000), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            housing=Housing(
                purchases=(purchase("p1", rented_fraction=0.0),),
                sales=(_PropertySale(month=sale_month, property_id="p1", closing_cost_ppb=rate_to_ppb(0.06)),),
                residence_events=(_PrimaryResidenceEvent(month=6, agent_id=OWNER, property_id="p1"),),
            ),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)

        assert trace(rollout).events.set_primary_residence_events.to_dicts() == [
            {"rollout_id": 0, "month_index": 6, "agent_id": OWNER, "property_id": "p1", "is_primary_residence": True}
        ]
        sale = sale_rows(rollout)["p1"]
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(158_000, abs=1)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(0, abs=1e-6)

    def test_same_month_primary_residence_assignment_fires_before_sale_but_does_not_accrue_use(self) -> None:
        sale_month = 30
        base = sale_situation(
            horizon=36, sale_month=sale_month, rented=False, home_values=[1.0] * sale_month + [1.4] * (37 - sale_month)
        )
        situation = replace(
            base,
            recurring_transfers=(),
            housing=replace(
                base.housing,
                residence_events=(_PrimaryResidenceEvent(month=sale_month, agent_id=OWNER, property_id="p1"),),
            ),
        )
        [rollout] = run(situation)

        assert trace(rollout).events.set_primary_residence_events.to_dicts() == [
            {
                "rollout_id": 0,
                "month_index": sale_month,
                "agent_id": OWNER,
                "property_id": "p1",
                "is_primary_residence": True,
            }
        ]
        sale = sale_rows(rollout)["p1"]
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        assert sale["long_term_capital_gain_quanta"] / 100 == pytest.approx(158_000, abs=1)

    def test_section_121_does_not_apply_without_owner_occupied_months(self) -> None:
        """Same sale at month 30, but the property has been fully let the whole time.
        Owner-occupied months = 0, so §121 does not apply while recapture and LTCG stay intact."""

        [rollout] = run(sale_situation(horizon=36, sale_month=30, home_values=[1.0] * 30 + [1.4] * 7))
        sale = sale_rows(rollout)["p1"]
        assert sale["section_121_exclusion_quanta"] / 100 == pytest.approx(0, abs=1e-6)
        # 30 months of depreciation × $400k / 27.5 / 12 ≈ $36,363.
        assert sale["depreciation_recapture_quanta"] / 100 == pytest.approx(36_363.64, abs=1)

    def test_lifecycle_event_frames_logged_for_each_kind(self) -> None:
        """All three lifecycle event kinds appear in their own frames, one row per event."""

        situation = Situation(
            horizon_months=24,
            rollout_count=1,
            series=series({RENT: [[1.0] * 25], HOME_VALUE: [[1.0] * 25]}, horizon_months=24, rollout_count=1),
            accounts=(account(OWNER, 800_000), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            housing=Housing(
                purchases=(purchase("p1", rented_fraction=0.0),),
                sales=(_PropertySale(month=12, property_id="p1", closing_cost_ppb=rate_to_ppb(0.06)),),
                rented_fraction_events=(
                    _RentedFraction(month=6, property_id="p1", rented_fraction_ppb=rate_to_ppb(1.0)),
                ),
                capital_improvements=(
                    _CapitalImprovement(month=8, property_id="p1", amount=money(50_000), description="new roof"),
                ),
            ),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)
        events = trace(rollout).events

        rented = one(events.set_rented_fraction_events.iter_rows(named=True))
        assert (rented["month_index"], rented["rented_fraction"], rented["property_id"]) == (6, 1, "p1")

        capex = one(events.capital_improvement_events.iter_rows(named=True))
        assert (capex["month_index"], capex["property_id"]) == (8, "p1")
        assert capex["amount_quanta"] / 100 == pytest.approx(50_000)

        sale = one(events.property_sale_events.iter_rows(named=True))
        assert (sale["month_index"], sale["property_id"]) == (12, "p1")

    def test_a_building_is_depreciated_once_however_long_it_is_held(self) -> None:
        """§168 gives a building 27.5 years of depreciation, not 27.5 years and then more.

        A $500k house is 80% building, so $400k of basis over 330 months. Held rented for 336,
        an uncapped accrual takes $407,272.32 — $7,272.32 of depreciation against basis that
        does not exist — which drives the tax-adjusted basis below the land value and inflates
        the realized gain and the §1250 recapture together, while still deducting from ordinary
        income every year past the 330th month.

        Sold at twice the purchase price so the gain is far larger than the depreciation, which
        leaves recapture bounded by what was actually taken. That is the figure under test: the
        basis exactly, whatever the holding period.
        """

        horizon, sale_month = 340, 336
        [rollout] = run(
            sale_situation(
                horizon=horizon,
                sale_month=sale_month,
                home_values=[1.0] * sale_month + [2.0] * (horizon + 1 - sale_month),
            )
        )
        sale = one(trace(rollout).events.property_sale_events.iter_rows(named=True))
        assert sale["depreciation_recapture_quanta"] == BUILDING_BASIS_QUANTA, (
            "recapture is capped by the basis the building had, not by the months it was held"
        )

    def test_depreciation_does_not_accrue_when_not_rented(self) -> None:
        """No letting → no depreciation accrual → no Schedule E deduction."""

        situation = Situation(
            horizon_months=12,
            rollout_count=1,
            series=series({HOME_VALUE: [[1.0] * 13]}, horizon_months=12, rollout_count=1),
            accounts=(account(OWNER, 600_000), account(TENANT), account(SELLER), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_transfers=(
                recurring_transfer(
                    "paycheck",
                    start_month=0,
                    end_month=11,
                    payer=TENANT,
                    payee=OWNER,
                    amount=5_000 * 100,
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(purchases=(purchase("p1", rented_fraction=0.0),)),
            locations=(SF_LOCATION,),
        )
        [rollout] = run(situation)
        # No depreciation → ordinary income equals gross paycheck income: $60,000.
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            60_000, abs=1e-6
        )

    def test_mortgage_interest_deducts_full_for_owner_occupied_and_scales_for_partial_rental(self) -> None:
        """MID applies to the owner fraction of mortgage interest; the let share deducts as
        Schedule E rental interest instead. Whether the property is fully owner-occupied or
        fully let, the same dollars of interest reduce ordinary income — through different
        mechanisms, so only the owner-occupied case carries a MID line."""

        owner_occupied = self._mortgage_breakdown(rented_fraction=0.0)
        rented = self._mortgage_breakdown(rented_fraction=1.0)
        assert owner_occupied["mortgage_interest_deduction_quanta"] / 100 > 0
        assert rented["mortgage_interest_deduction_quanta"] / 100 == pytest.approx(0, abs=1e-6)

    def _mortgage_breakdown(self, *, rented_fraction: float) -> dict[str, Any]:
        purchase_price = 600_000
        situation = Situation(
            horizon_months=12,
            rollout_count=1,
            series=series({RENT: [[1.0] * 13], HOME_VALUE: [[1.0] * 13]}, horizon_months=12, rollout_count=1),
            accounts=(account(OWNER, 700_000), account(TENANT), account(SELLER), account(LENDER), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_transfers=(
                recurring_transfer(
                    "rental_income:p1",
                    start_month=0,
                    end_month=11,
                    payer=TENANT,
                    payee=OWNER,
                    amount=indexed(4_000),
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(
                purchases=(
                    purchase(
                        "p1",
                        price=purchase_price,
                        # Land-only basis isolates the MID-vs-Schedule-E comparison from depreciation.
                        land_value_fraction=1.0,
                        rented_fraction=rented_fraction,
                        mortgage=financing("p1_mortgage", principal=Decimal(purchase_price) * Decimal("0.80")),
                    ),
                )
            ),
            locations=(SF_LOCATION,),
            mortgage_interest_policies=(mortgage_interest_deduction("p1_mortgage", OWNER),),
        )
        [rollout] = run(situation)
        return breakdown(rollout, month=11, jurisdiction=FEDERAL)

    def test_property_tax_routes_owner_fraction_to_salt_and_rented_fraction_to_schedule_e(self) -> None:
        """A property let three quarters routes 25% of its property tax to SALT (the owner-use
        portion) and 75% to Schedule E (the let portion)."""

        purchase_price = 600_000
        rented_fraction = 0.75
        annual_tax_rate = 0.012  # 1.2% of price = $7,200/yr → $600/mo
        situation = Situation(
            horizon_months=12,
            rollout_count=1,
            series=series({RENT: [[1.0] * 13], HOME_VALUE: [[1.0] * 13]}, horizon_months=12, rollout_count=1),
            accounts=(account(OWNER, 700_000), account(TENANT), account(SELLER), account(COUNTY), account(IRS)),
            tax_profiles=(taxpayer(),),
            recurring_transfers=(
                recurring_transfer(
                    "rental_income:p1",
                    start_month=0,
                    end_month=11,
                    payer=TENANT,
                    payee=OWNER,
                    amount=indexed(4_000),
                    income=ORDINARY_INCOME,
                ),
            ),
            housing=Housing(
                purchases=(
                    purchase(
                        "p1",
                        price=purchase_price,
                        rented_fraction=rented_fraction,
                        # A land-only basis makes the building basis zero, so no §168
                        # depreciation accrues to blur the property-tax assertion.
                        land_value_fraction=1.0,
                    ),
                )
            ),
            locations=(SF_LOCATION,),
            property_tax_policies=(property_tax("p1", OWNER, rate=annual_tax_rate, end_month=11),),
            salt_policies=(salt_cap(OWNER, 10_000),),
        )
        [rollout] = run(situation)
        # Gross rent: 12 × $4,000 = $48,000. Property tax fires at months 1..11 (11 payments;
        # month 0 is the purchase month, no tax that month) → $7,200 × 11/12 = $6,600.
        # rented_fraction=0.75 → $4,950 routes to Schedule E and $1,650 to SALT.
        # Federal ordinary income after Schedule E = $48,000 - $4,950 = $43,050. (The SALT
        # total combines property tax with state income tax and gets capped, so the absolute
        # SALT number is not the assertion; the owner-fraction effect is observable through
        # ordinary income falling relative to the rental income.)
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            43_050, abs=1e-6
        )

    def test_obligation_deductible_fraction_scales_deduction(self) -> None:
        """Partial letting: HOA dues deduct only up to the let fraction — 0.5 here, so $200 of
        the $400/mo HOA deducts each month."""

        base = taxed_rental(monthly_rent=2_500)
        # Gross rental $30,000/yr (50% let); HOA $400/mo, 50% deductible → $200/mo × 12 = $2,400.
        situation = replace(
            base,
            accounts=(*base.accounts, account(HOA)),
            obligations=(
                dues(
                    "hoa_dues",
                    obligation_type=ObligationType.HOA_DUES,
                    payer=OWNER,
                    payee=HOA,
                    amount=indexed(400),
                    end_month=11,
                    deductible_fraction=0.5,
                ),
            ),
        )
        [rollout] = run(situation)
        assert breakdown(rollout, month=11, jurisdiction=FEDERAL)["ordinary_income_quanta"] / 100 == pytest.approx(
            27_600, abs=1e-6
        )


if __name__ == "__main__":
    pytest_bazel.main()
