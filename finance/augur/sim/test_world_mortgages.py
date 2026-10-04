"""Mortgage settlement binds servicing facts to the ledger and selected payer cash."""

from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.model.series import LocationId
from finance.augur.sim.actions import ClaimId, PayClaim
from finance.augur.sim.agent import assemble
from finance.augur.sim.bills import Biller
from finance.augur.sim.capture import FinancialCapture
from finance.augur.sim.ids import AccountId, LiabilityId, PropertyId
from finance.augur.sim.market_path import Series
from finance.augur.sim.mortgage import Mortgage, MortgagePayment, MortgageTerms
from finance.augur.sim.property import Housing, MortgageFinancing, ScheduledPurchase, ScheduledSale
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.results import Executed
from finance.augur.sim.schedule import Once
from finance.augur.sim.testing.accounting import CASH, EXOGENOUS, HOUSEHOLD, RESERVE, WORLD, opening, world_on
from finance.augur.sim.testing.situs import START_YEAR, UNTAXED, flat_parcel
from finance.augur.sim.world import World


@dataclass(frozen=True)
class Situation:
    """A financed month-2 purchase sold in month 5, an ordinary bill and a property tax, on the market's paths."""

    purchase: ScheduledPurchase
    sale: ScheduledSale
    home_values: Series


@pytest.fixture
def case() -> Situation:
    return Situation(
        purchase=ScheduledPurchase(
            month=2,
            cause_id="test-purchase",
            property_id=PropertyId("test-home"),
            parcel=flat_parcel(Decimal("0.012")),
            market=LocationId("test-market"),
            buyer_agent_id=HOUSEHOLD,
            buyer_account_id=AccountId("checking"),
            seller_agent_id=WORLD,
            seller_account_id=AccountId("cash"),
            purchase_price=100_000,
            down_payment=40_000,
            buyer_closing_cost=0,
            rented_fraction_ppb=0,
            land_value_fraction_ppb=200_000_000,
            mortgage=MortgageFinancing(
                liability_id=LiabilityId("test-mortgage"),
                lender_agent_id=WORLD,
                lender_account_id=AccountId("cash"),
                principal=60_000,
                annual_interest_rate_ppb=0,
                term_months=60,
            ),
        ),
        sale=ScheduledSale(month=5, property_id=PropertyId("test-home"), commission_ppb=0, escrow_title_ppb=0),
        home_values=Series(series_id="home_value:test-market", snapshots=7, values=(50, 100, 200, 240, 300, 360, 800)),
    )


@pytest.fixture
def mortgage() -> Mortgage:
    return Mortgage(
        MortgageTerms(
            liability_id=LiabilityId("test-mortgage"),
            property_id=PropertyId("test-home"),
            borrower=CASH,
            lender=EXOGENOUS,
            origination_month=2,
            origination_principal=60_000,
            annual_interest_rate_ppb=0,
            term_months=60,
        )
    )


def composed(case: Situation) -> World:
    """The situation's world declared piece by piece, as an experiment would write it."""
    world = world_on(
        (case.home_values,), horizon_months=6, accounts=opening({CASH: 200_000, RESERVE: 3000}), taxpayers=()
    )
    world.track(
        Biller(
            schedule=Once(month=3),
            obligation_id="ordinary",
            obligation_type="rent",
            from_account=CASH,
            to_account=EXOGENOUS,
            amount_due=2,
            property_id=None,
            deduction_category=None,
            deductible_fraction_ppb=0,
        )
    )
    world.declare_housing(
        Housing(purchases=(case.purchase,), sales=(case.sale,)),
        (
            PropertyTaxPolicy(
                property_id=PropertyId("test-home"),
                owner_agent_id=HOUSEHOLD,
                from_account_id=AccountId("checking"),
                tax_authority_agent_id=WORLD,
                tax_authority_account_id=AccountId("cash"),
                start_year=START_YEAR,
                start_month=3,
                end_month=None,
            ),
        ),
    )
    return world


def installment(mortgage: Mortgage, month: int, principal_before: int, principal_paid: int = 1000) -> MortgagePayment:
    # Explicit settlement facts, not an amortization oracle.
    return MortgagePayment(
        terms=mortgage.terms,
        month=month,
        principal_before=principal_before,
        interest=0,
        principal=principal_paid,
        total=principal_paid,
        rental_interest=0,
    )


def fingerprint(world: World) -> tuple[object, ...]:
    properties = world.properties
    assert properties is not None
    return deepcopy(
        (
            world.book(),
            world.accounting.tax.years,
            world.accounting.tax.income.by_source,
            world.accounting.journal,
            world.accounting.transfers,
            world.accounting.mortgage_payments,
            properties.purchases,
            properties.sales,
            properties.originations,
        )
    )


def test_mortgage_postings_use_selected_cash_and_ledger_principal_through_payoff(
    case: Situation, mortgage: Mortgage
) -> None:
    world = composed(case)
    assert world.mortgage_principal(LiabilityId("test-mortgage")) == 0
    for month, ending_principal in enumerate((0, 0, 60_000, 59_000, 58_000, 0)):
        active = {LiabilityId("test-mortgage"): mortgage} if month >= 2 else {}
        originated, paid_off = world.prepare_month(month, active if month == 2 else {}, active)
        assert originated == (["test-mortgage"] if month == 2 else [])
        assert paid_off == (["test-mortgage"] if month == 5 else [])
        quote = installment(mortgage, month, 60_000 if month == 3 else 59_000) if month in (3, 4) else None
        world.assemble_claims([] if quote is None else [quote])
        if month in (3, 4):
            observation = assemble(HOUSEHOLD, month, world.open_mail(HOUSEHOLD))
            assert [claim.obligation_type for claim in observation.claims] == (
                ["rent", "mortgage_payment", "property_tax"] if month == 3 else ["mortgage_payment", "property_tax"]
            )
            claim = next(claim for claim in observation.claims if claim.obligation_type == "mortgage_payment")
            checking = world.account_balance(HOUSEHOLD, AccountId("checking"))
            assert not [row for row in world.accounting.mortgage_payments if row.month == month]
            action = PayClaim(
                request_id=1,
                cause_id="reserve-payment",
                claim=ClaimId(month=claim.month, index=claim.index),
                from_account=RESERVE,
                amount=1000,
            )
            assert isinstance(world.apply(HOUSEHOLD, action, 0), Executed)
            assert [row.liability_id for row in world.accounting.mortgage_payments if row.month == month] == [
                "test-mortgage"
            ]
            assert world.account_balance(HOUSEHOLD, AccountId("checking")) == checking
            others = [other for other in observation.claims if other is not claim]
            for index, other in enumerate(others, start=1):
                action = PayClaim(
                    request_id=index + 1,
                    cause_id=other.cause_id,
                    claim=ClaimId(month=other.month, index=other.index),
                    from_account=other.from_account,
                    amount=other.amount_due,
                )
                assert isinstance(world.apply(HOUSEHOLD, action, index), Executed)
            assert quote is not None
            mortgage.record_payment(quote, world.mortgage_principal(LiabilityId("test-mortgage")))
        if paid_off:
            mortgage.payoff()
        assert world.mortgage_principal(LiabilityId("test-mortgage")) == ending_principal
        world.close_books(failed=False, mortgages=list(active.values()))
    # No month was opened through `open_month`, so every month's outcomes are still in the buffers.
    capture = FinancialCapture(world, capture="forensic")
    capture.record()
    financial = capture.financial()
    assert len(financial.mortgage_payments) == 2
    assert all(row.from_account_id == "savings" for row in financial.mortgage_payments)
    assert financial.properties is not None
    sale = financial.properties.sales[0]
    assert (sale.mortgage_payoff, sale.net_cash_to_owner) == (58_000, 122_000)
    assert world.account_balance(HOUSEHOLD, AccountId("savings")) == 1000
    assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in financial.journal)


@pytest.mark.parametrize("bad_payoff", ["missing", "inactive", "wrong_contract"])
def test_invalid_mortgage_effects_do_not_change_cash_or_principal(
    case: Situation, mortgage: Mortgage, bad_payoff: str
) -> None:
    case = replace(case, purchase=replace(case.purchase, month=0), sale=replace(case.sale, month=1))
    mortgage = Mortgage(replace(mortgage.terms, origination_month=0))
    world = composed(case)
    before = fingerprint(world)
    with pytest.raises(ValueError, match="mortgage origination"):
        world.prepare_month(0, {}, {})
    assert fingerprint(world) == before
    assert world.mortgage_principal(LiabilityId("test-mortgage")) == 0
    world.prepare_month(0, {LiabilityId("test-mortgage"): mortgage}, {})
    world.assemble_claims([])
    world.close_books(failed=False, mortgages=[mortgage])
    invalid = deepcopy(mortgage)
    if bad_payoff == "inactive":
        invalid.payoff()
    elif bad_payoff == "wrong_contract":
        invalid = Mortgage(replace(mortgage.terms, origination_principal=60_001))
    before = fingerprint(world)
    with pytest.raises(ValueError, match="mortgage payoff"):
        world.prepare_month(1, {}, {} if bad_payoff == "missing" else {LiabilityId("test-mortgage"): invalid})
    assert fingerprint(world) == before
    assert world.mortgage_principal(LiabilityId("test-mortgage")) == 60_000
    with pytest.raises(ValueError, match="invalid mortgage installment"):
        world.assemble_claims([installment(mortgage, 1, 60_000, 60_001)])
    assert fingerprint(world) == before
    assert not world.accounting.mortgage_payments


def test_a_building_basis_rounds_in_the_engine_not_in_the_authoring() -> None:
    """Authored money is exact; the land share multiplies it here, rounding to the quantum once."""
    world = world_on(
        (Series(series_id="home_value:test-market", snapshots=2, values=(10_001, 10_001)),),
        horizon_months=1,
        accounts=opening({CASH: 200_00}),
        taxpayers=(),
    )
    world.declare_housing(
        Housing(
            purchases=(
                ScheduledPurchase(
                    month=0,
                    cause_id="test-purchase",
                    property_id=PropertyId("test-home"),
                    parcel=UNTAXED,
                    market=LocationId("test-market"),
                    buyer_agent_id=HOUSEHOLD,
                    buyer_account_id=AccountId("checking"),
                    seller_agent_id=WORLD,
                    seller_account_id=AccountId("cash"),
                    purchase_price=10_001,
                    down_payment=10_001,
                    buyer_closing_cost=0,
                    rented_fraction_ppb=0,
                    land_value_fraction_ppb=200_000_000,
                    mortgage=None,
                ),
            )
        )
    )
    world.prepare_month(0, {}, {})
    properties = world.properties
    assert properties is not None
    assert properties.properties[PropertyId("test-home")].state.building_basis == 8001


if __name__ == "__main__":
    pytest_bazel.main()
