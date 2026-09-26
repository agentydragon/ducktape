"""§163(h)(3) mortgage interest and the §164(b)(6) SALT cap, as an itemizing filer meets them.

Both deductions are conditional in ways a bracket walk cannot show. Mortgage interest is
deductible only on acquisition debt and only up to a principal cap that differs between the
federal and California returns; state and local taxes are deductible only federally and only
up to a cap that steps down by year. Every case here therefore reads the deduction the engine
booked rather than the tax it produced, because a deduction that lost to the standard
deduction and one that was never computed give the same tax and are not the same answer.

Interest expectations come from the `mortgage_payments` the run itself recorded, not from an
amortization formula restated here: what is under test is which interest reaches the return,
not what the schedule is.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb, round_currency_amount
from finance.augur.sim.ids import AgentId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedJurisdiction,
    PreparedLocation,
    PreparedRecurringTransfer,
    _MortgageFinancing,
    _MortgageInterestDeduction,
    _PropertyPurchase,
    _PropertyTax,
    _SaltCap,
    _SaltDeduction,
)
from finance.augur.sim.property import Housing
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME, FilingStatus, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE, PAYROLL, IRS, SELLER, BANK, COLLECTOR = "alice", "payroll", "irs", "seller", "bank", "sf_tax_collector"
CHECKING = "checking"
FEDERAL, CALIFORNIA = "federal_us", "california"

LOCATION_ID = "san_francisco"
MORTGAGE_ID = "sf_home_mortgage"
HELOC_ID = "alice_heloc"

# The two standard deductions a single filer is measured against, in the deployment's own
# jurisdiction records. Stated here because several cases turn on which one won.
FEDERAL_STANDARD = 14_600.0
CALIFORNIA_STANDARD = 5_363.0

# §164(b)(6) as the deployment encodes it: the OBBBA cap, stepping down to the TCJA cap.
OBBBA_CAP = 40_000.0
TCJA_CAP = 10_000.0
TCJA_CAP_YEAR = 4

# §163(h)(3)(B)(ii), federal only: California conforms to the pre-TCJA principal cap.
FEDERAL_PRINCIPAL_CAP = 750_000.0


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def usd(row: dict[str, Any], field: str) -> float:
    return int(row[field]) / 100


SAN_FRANCISCO = PreparedLocation(
    location_id=LOCATION_ID,
    display_name="San Francisco, CA",
    jurisdiction_ids=(FEDERAL, CALIFORNIA),
    annual_property_tax_rate_ppb=rate_to_ppb(0.01180),
    annual_special_assessment=0,
)

DEFAULT_SALT_SCHEDULE = (
    _SaltCap(effective_year_index=0, cap=money(int(OBBBA_CAP))),
    _SaltCap(effective_year_index=TCJA_CAP_YEAR, cap=money(int(TCJA_CAP))),
)


def account(agent_id: str, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=money(balance))


def deducts(
    liability_id: str, *, debt_class: Literal["acquisition", "home_equity"] = "acquisition"
) -> _MortgageInterestDeduction:
    return _MortgageInterestDeduction(
        liability_id=liability_id,
        owner_agent_id=ALICE,
        debt_class=debt_class,
        per_jurisdiction_principal_cap={FEDERAL: money(750_000), CALIFORNIA: money(1_000_000)},
    )


def salt(*, cap_schedule: tuple[_SaltCap, ...] = DEFAULT_SALT_SCHEDULE) -> _SaltDeduction:
    return _SaltDeduction(profile_id=ALICE, federal_jurisdiction_id=FEDERAL, cap_schedule=cap_schedule)


def financed_purchase(
    cause_id: str, property_id: str, *, price: int, down: int, liability_id: str, annual_rate: float, term_months: int
) -> _PropertyPurchase:
    return _PropertyPurchase(
        month=0,
        cause_id=cause_id,
        property_id=property_id,
        location_id=LOCATION_ID,
        buyer_agent_id=ALICE,
        buyer_account_id=CHECKING,
        seller_agent_id=SELLER,
        seller_account_id=CHECKING,
        purchase_price=money(price),
        down_payment=money(down),
        buyer_closing_cost=0,
        rented_fraction_ppb=0,
        land_value_fraction_ppb=rate_to_ppb(0.20),
        mortgage=_MortgageFinancing(
            liability_id=liability_id,
            lender_agent_id=BANK,
            lender_account_id=CHECKING,
            principal=money(price - down),
            annual_interest_rate_ppb=rate_to_ppb(annual_rate),
            term_months=term_months,
        ),
    )


@dataclass(frozen=True)
class Situation:
    """A single filer on W-2 wages who buys a financed San Francisco home at month 0.

    Wages are level and the purchase is at month 0, so every year of the horizon has the same
    income shape and any change between years belongs to the deduction rules rather than to
    the situation.
    """

    purchase_price: int
    down_payment: int
    annual_rate: float
    term_months: int = 360
    annual_w2_income: int = 200_000
    horizon_months: int = 13
    mortgage_interest_policies: tuple[_MortgageInterestDeduction, ...] = ()
    salt_policies: tuple[_SaltDeduction, ...] = ()
    extra_purchases: tuple[_PropertyPurchase, ...] = ()


def standard_home(
    *,
    annual_w2_income: int = 200_000,
    horizon_months: int = 13,
    mortgage_interest_policies: tuple[_MortgageInterestDeduction, ...] = (),
    salt_policies: tuple[_SaltDeduction, ...] = (),
) -> Situation:
    """The $900k home on a $720k mortgage: first-year interest clears both standard deductions
    and the principal sits under the federal cap, so nothing but the policy under test binds."""
    return Situation(
        purchase_price=900_000,
        down_payment=180_000,
        annual_rate=0.07,
        annual_w2_income=annual_w2_income,
        horizon_months=horizon_months,
        mortgage_interest_policies=mortgage_interest_policies,
        salt_policies=salt_policies,
    )


def small_home(*, mortgage_interest_policies: tuple[_MortgageInterestDeduction, ...] = ()) -> Situation:
    """An $80k mortgage at 5%: first-year interest lands well under the federal standard deduction."""
    return Situation(
        purchase_price=200_000,
        down_payment=120_000,
        annual_rate=0.05,
        mortgage_interest_policies=mortgage_interest_policies,
    )


def compose(case: Situation) -> World:
    jurisdictions = {id_: load_jurisdiction(id_) for id_ in (FEDERAL, CALIFORNIA)}
    world = World(
        MarketPath((), 0, rollout_count=1),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=tuple(
            PreparedJurisdiction(jurisdiction_id=id_, level=rules.level) for id_, rules in jurisdictions.items()
        ),
    )
    for opening in (
        account(ALICE, case.down_payment + 50_000),
        account(PAYROLL),
        account(IRS),
        account(SELLER),
        account(BANK),
        account(COLLECTOR),
    ):
        world.declare_account(opening)
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(
                    agent_id=ALICE,
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=[FEDERAL, CALIFORNIA],
                    tax_authority_agent_id=IRS,
                ),
                jurisdictions,
                quantum=QUANTUM,
            )
        )
    )
    world.accounting.tax.salt_policies = case.salt_policies
    world.accounting.tax.mortgage_interest_policies = case.mortgage_interest_policies
    world.declare_housing(
        Housing(
            purchases=(
                financed_purchase(
                    "alice_buys_sf_home",
                    "sf_home",
                    price=case.purchase_price,
                    down=case.down_payment,
                    liability_id=MORTGAGE_ID,
                    annual_rate=case.annual_rate,
                    term_months=case.term_months,
                ),
                *case.extra_purchases,
            )
        ),
        (
            _PropertyTax(
                property_id="sf_home",
                owner_agent_id=ALICE,
                from_account_id=CHECKING,
                tax_authority_agent_id=COLLECTOR,
                tax_authority_account_id=CHECKING,
                annual_tax_rate_ppb=rate_to_ppb(0.012),
                start_month=0,
                end_month=None,
            ),
        ),
        (SAN_FRANCISCO,),
    )
    world.recurring_transfers = (
        PreparedRecurringTransfer(
            start_month=0,
            end_month=case.horizon_months - 1,
            cause_id="alice_paycheck",
            from_account=AccountRef(agent_id=PAYROLL, account_id=CHECKING),
            to_account=AccountRef(agent_id=ALICE, account_id=CHECKING),
            amount=money(round_currency_amount(Decimal(case.annual_w2_income) / 12, quantum=QUANTUM)),
            income_category=ORDINARY_INCOME,
            deduction_category=None,
        ),
    )
    return world


def run(case: Situation) -> Rollout:
    """Alice pays each account's claims all or none: her installments, her property tax, her assessments."""
    household = ConfiguredHousehold(AgentId(ALICE), ())
    session = ActionSession({0: compose(case)}, ALICE)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id, decision.observation.month, household.decide(decision.observation)
                    )
                    for decision in batch
                ]
            )
    finally:
        session.close()
    return one(batch.rollouts)


def breakdown(rollout: Rollout, *, jurisdiction_id: str, year_index: int = 0) -> dict[str, Any]:
    """The year-end return one jurisdiction assessed, in the year that ends at month `12y + 11`."""
    assert rollout.trace is not None
    month = 12 * year_index + 11
    return rollout.trace.events.tax_breakdowns.filter(
        (pl.col("jurisdiction_id") == jurisdiction_id) & (pl.col("month_index") == month)
    ).row(0, named=True)


def interest_through(rollout: Rollout, *, liability_id: str, month: int) -> float:
    """Interest actually paid on a liability up to and including `month`."""
    assert rollout.trace is not None
    rows = rollout.trace.events.mortgage_payments.filter(
        (pl.col("liability_id") == liability_id) & (pl.col("month_index") <= month)
    )
    return int(rows.get_column("interest_quanta").sum()) / 100


def property_tax_through(rollout: Rollout, *, month: int) -> float:
    assert rollout.trace is not None
    rows = rollout.trace.events.obligation_settlements.filter(
        (pl.col("obligation_type") == "property_tax") & (pl.col("month_index") <= month)
    )
    return int(rows.get_column("amount_paid_quanta").sum()) / 100


def test_acquisition_interest_above_the_standard_deduction_is_itemized() -> None:
    """A $720k mortgage at 7% throws off ~$46k of first-year interest, well past both standards.

    Both returns take the whole of it — the principal is under the federal cap — so the tax
    saved is exactly the excess over each jurisdiction's standard deduction at that
    jurisdiction's marginal rate, which $200k of wages leaves unchanged either way.
    """
    baseline = run(standard_home())
    deducted = run(standard_home(mortgage_interest_policies=(deducts(MORTGAGE_ID),)))

    interest = interest_through(deducted, liability_id=MORTGAGE_ID, month=11)
    assert interest > FEDERAL_STANDARD

    federal_baseline = breakdown(baseline, jurisdiction_id=FEDERAL)
    federal = breakdown(deducted, jurisdiction_id=FEDERAL)
    california_baseline = breakdown(baseline, jurisdiction_id=CALIFORNIA)
    california = breakdown(deducted, jurisdiction_id=CALIFORNIA)

    assert usd(federal_baseline, "mortgage_interest_deduction_quanta") == 0.0
    assert usd(federal_baseline, "itemized_deduction_quanta") == 0.0
    assert usd(federal_baseline, "standard_deduction_quanta") == pytest.approx(FEDERAL_STANDARD)

    assert usd(federal, "mortgage_interest_deduction_quanta") == pytest.approx(interest, rel=1e-5)
    assert usd(federal, "itemized_deduction_quanta") == pytest.approx(interest, rel=1e-5)
    assert usd(california, "mortgage_interest_deduction_quanta") == pytest.approx(interest, rel=1e-5)
    assert usd(california, "itemized_deduction_quanta") == pytest.approx(interest, rel=1e-5)

    federal_saved = usd(federal_baseline, "total_tax_quanta") - usd(federal, "total_tax_quanta")
    assert federal_saved == pytest.approx((interest - FEDERAL_STANDARD) * 0.24, abs=0.5)
    california_saved = usd(california_baseline, "total_tax_quanta") - usd(california, "total_tax_quanta")
    assert california_saved == pytest.approx((interest - CALIFORNIA_STANDARD) * 0.093, abs=0.5)


def test_home_equity_interest_is_not_deductible() -> None:
    """TCJA suspended the home-equity deduction, so the tax must match having no policy at all.

    The interest is real and paid either way; only its deductibility changes. Comparing
    against the no-policy baseline rather than against zero is what catches an engine that
    classifies the debt correctly and then deducts it anyway.
    """
    baseline = run(standard_home())
    home_equity = run(standard_home(mortgage_interest_policies=(deducts(MORTGAGE_ID, debt_class="home_equity"),)))

    for jurisdiction in (FEDERAL, CALIFORNIA):
        booked = breakdown(home_equity, jurisdiction_id=jurisdiction)
        assert usd(booked, "mortgage_interest_deduction_quanta") == pytest.approx(0.0, abs=1e-9)
        assert usd(booked, "total_tax_quanta") == pytest.approx(
            usd(breakdown(baseline, jurisdiction_id=jurisdiction), "total_tax_quanta"), abs=0.02
        )


def test_acquisition_and_home_equity_debt_are_classified_per_liability() -> None:
    """Two liabilities, one of each class: only the acquisition interest reaches the deduction.

    The classification is a property of the liability rather than of the taxpayer, so a
    filer holding both must see them split. A second financed purchase stands in for the
    HELOC: the liability bookkeeping is identical and only `debt_class` differs.
    """
    rollout = run(
        Situation(
            purchase_price=900_000,
            down_payment=180_000,
            annual_rate=0.07,
            mortgage_interest_policies=(
                deducts(MORTGAGE_ID, debt_class="acquisition"),
                deducts(HELOC_ID, debt_class="home_equity"),
            ),
            extra_purchases=(
                financed_purchase(
                    "alice_opens_heloc",
                    "alice_heloc_collateral",
                    price=60_000,
                    down=0,
                    liability_id=HELOC_ID,
                    annual_rate=0.08,
                    term_months=360,
                ),
            ),
        )
    )

    acquisition = interest_through(rollout, liability_id=MORTGAGE_ID, month=11)
    heloc = interest_through(rollout, liability_id=HELOC_ID, month=11)
    assert heloc > 0.0  # the excluded interest was really paid

    federal = breakdown(rollout, jurisdiction_id=FEDERAL)
    assert usd(federal, "mortgage_interest_deduction_quanta") == pytest.approx(acquisition, rel=1e-5)
    assert usd(federal, "mortgage_interest_deduction_quanta") < acquisition + heloc


def test_without_a_policy_no_interest_is_deducted() -> None:
    """A mortgage alone does not itemize a return; the standard deduction stands."""
    rollout = run(Situation(purchase_price=900_000, down_payment=180_000, annual_rate=0.07))

    federal = breakdown(rollout, jurisdiction_id=FEDERAL)
    california = breakdown(rollout, jurisdiction_id=CALIFORNIA)
    assert usd(federal, "mortgage_interest_deduction_quanta") == 0.0
    assert usd(federal, "itemized_deduction_quanta") == 0.0
    assert usd(federal, "standard_deduction_quanta") == pytest.approx(FEDERAL_STANDARD)
    assert usd(california, "mortgage_interest_deduction_quanta") == 0.0
    assert usd(california, "itemized_deduction_quanta") == 0.0
    assert usd(california, "standard_deduction_quanta") == pytest.approx(CALIFORNIA_STANDARD)


def test_the_federal_principal_cap_prorates_interest_and_california_does_not() -> None:
    """An $850k mortgage: the federal return deducts 750/850 of the interest, California all of it.

    The two jurisdictions differ only in this cap, so a single run shows both readings and
    California itemizing strictly more is the check that they were not resolved together.
    """
    rollout = run(
        Situation(
            purchase_price=1_050_000,
            down_payment=200_000,
            annual_rate=0.07,
            mortgage_interest_policies=(deducts(MORTGAGE_ID),),
        )
    )

    interest = interest_through(rollout, liability_id=MORTGAGE_ID, month=11)
    federal = breakdown(rollout, jurisdiction_id=FEDERAL)
    california = breakdown(rollout, jurisdiction_id=CALIFORNIA)
    assert usd(federal, "mortgage_interest_deduction_quanta") == pytest.approx(
        interest * (FEDERAL_PRINCIPAL_CAP / 850_000.0), rel=1e-5
    )
    assert usd(california, "mortgage_interest_deduction_quanta") == pytest.approx(interest, rel=1e-5)
    assert usd(california, "itemized_deduction_quanta") > usd(federal, "itemized_deduction_quanta")


def test_interest_below_the_standard_deduction_is_still_reported() -> None:
    """The return books what was itemized even when the standard deduction wins.

    $80k at 5% is far under the federal standard, so the tax is the no-policy tax. The
    itemized figure is still reported, which is how a consumer can tell that the standard
    deduction won rather than that nothing was deductible.
    """
    baseline = run(small_home())
    with_policy = run(small_home(mortgage_interest_policies=(deducts(MORTGAGE_ID),)))

    interest = interest_through(with_policy, liability_id=MORTGAGE_ID, month=11)
    assert interest < FEDERAL_STANDARD

    federal = breakdown(with_policy, jurisdiction_id=FEDERAL)
    assert usd(federal, "mortgage_interest_deduction_quanta") == pytest.approx(interest, rel=1e-5)
    assert usd(federal, "itemized_deduction_quanta") == pytest.approx(interest, rel=1e-5)
    assert usd(federal, "standard_deduction_quanta") == pytest.approx(FEDERAL_STANDARD)
    assert usd(federal, "total_tax_quanta") == pytest.approx(
        usd(breakdown(baseline, jurisdiction_id=FEDERAL), "total_tax_quanta"), abs=0.02
    )


def test_each_year_deducts_only_its_own_interest() -> None:
    """Year two takes year two's interest, not everything paid since origination.

    The deduction is fed by an interest-to-date accumulator, so a year end that does not
    reset it produces a second-year deduction of the cumulative sum — larger than the
    first year's on a loan whose interest is falling, which is the shape asserted last.
    """
    rollout = run(
        Situation(
            purchase_price=600_000,
            down_payment=200_000,
            annual_rate=0.07,
            horizon_months=25,
            mortgage_interest_policies=(deducts(MORTGAGE_ID),),
        )
    )
    assert rollout.trace is not None

    federal_years = rollout.trace.events.tax_breakdowns.filter(pl.col("jurisdiction_id") == FEDERAL).sort("month_index")
    assert federal_years.height == 2  # year ends at month 11 and month 23

    # Origination is month 0 and the first amortizing payment lands at month 1, so year one
    # carries eleven payments and year two carries twelve.
    year_1_interest = interest_through(rollout, liability_id=MORTGAGE_ID, month=11)
    year_2_interest = interest_through(rollout, liability_id=MORTGAGE_ID, month=23) - year_1_interest

    rows = federal_years.rows(named=True)
    assert usd(rows[0], "mortgage_interest_deduction_quanta") == pytest.approx(year_1_interest, rel=1e-5)
    assert usd(rows[1], "mortgage_interest_deduction_quanta") == pytest.approx(year_2_interest, rel=1e-5)


def test_state_and_property_tax_under_the_cap_deduct_in_full() -> None:
    """SALT is the California tax plus the property tax, and federal-only.

    Both figures are read back from the run — the state tax off California's own return,
    the property tax off what was actually settled — so this states the relationship
    rather than a pre-computed total.
    """
    rollout = run(standard_home(mortgage_interest_policies=(deducts(MORTGAGE_ID),), salt_policies=(salt(),)))

    federal = breakdown(rollout, jurisdiction_id=FEDERAL)
    california = breakdown(rollout, jurisdiction_id=CALIFORNIA)
    expected = property_tax_through(rollout, month=11) + usd(california, "total_tax_quanta")
    assert expected < OBBBA_CAP

    assert usd(federal, "salt_deduction_quanta") == pytest.approx(expected, rel=1e-5)
    assert usd(federal, "itemized_deduction_quanta") == pytest.approx(
        usd(federal, "mortgage_interest_deduction_quanta") + expected, rel=1e-5
    )
    assert usd(california, "salt_deduction_quanta") == 0.0


def test_state_and_property_tax_over_the_cap_clip_to_it() -> None:
    """A $1.5M home on $1M of wages puts SALT far past the cap; the deduction is the cap."""
    rollout = run(
        Situation(
            purchase_price=1_500_000,
            down_payment=400_000,
            annual_rate=0.07,
            annual_w2_income=1_000_000,
            mortgage_interest_policies=(deducts(MORTGAGE_ID),),
            salt_policies=(salt(),),
        )
    )

    federal = breakdown(rollout, jurisdiction_id=FEDERAL)
    california = breakdown(rollout, jurisdiction_id=CALIFORNIA)
    uncapped = property_tax_through(rollout, month=11) + usd(california, "total_tax_quanta")
    assert uncapped > OBBBA_CAP

    assert usd(federal, "salt_deduction_quanta") == pytest.approx(OBBBA_CAP, rel=1e-5)
    assert usd(federal, "itemized_deduction_quanta") == pytest.approx(
        usd(federal, "mortgage_interest_deduction_quanta") + OBBBA_CAP, rel=1e-5
    )


def test_without_a_policy_no_state_or_property_tax_is_deducted() -> None:
    """Paying state and property tax does not by itself put SALT on the return."""
    rollout = run(standard_home(mortgage_interest_policies=(deducts(MORTGAGE_ID),)))

    federal = breakdown(rollout, jurisdiction_id=FEDERAL)
    assert usd(federal, "salt_deduction_quanta") == 0.0
    assert usd(federal, "itemized_deduction_quanta") == pytest.approx(
        usd(federal, "mortgage_interest_deduction_quanta"), rel=1e-5
    )


def test_the_cap_steps_down_on_its_scheduled_year() -> None:
    """The OBBBA cap gives way to the TCJA cap from year four.

    Income and property are level across the five-year horizon, so the two years differ in
    nothing but which cap applies and the drop can only come from the schedule.
    """
    rollout = run(
        standard_home(horizon_months=60, mortgage_interest_policies=(deducts(MORTGAGE_ID),), salt_policies=(salt(),))
    )

    first = breakdown(rollout, jurisdiction_id=FEDERAL, year_index=0)
    stepped = breakdown(rollout, jurisdiction_id=FEDERAL, year_index=TCJA_CAP_YEAR)
    assert usd(stepped, "salt_deduction_quanta") == pytest.approx(TCJA_CAP, rel=1e-5)
    assert usd(first, "salt_deduction_quanta") > usd(stepped, "salt_deduction_quanta")


def test_an_empty_schedule_is_no_cap_at_all() -> None:
    """A full TCJA sunset is expressible: no entries means nothing clips.

    Absence of a cap has to be distinguishable from a very large one, because a sensitivity
    run that assumes the sunset is asking exactly that question.
    """
    rollout = run(
        Situation(
            purchase_price=1_500_000,
            down_payment=400_000,
            annual_rate=0.07,
            annual_w2_income=1_000_000,
            mortgage_interest_policies=(deducts(MORTGAGE_ID),),
            salt_policies=(salt(cap_schedule=()),),
        )
    )

    federal = breakdown(rollout, jurisdiction_id=FEDERAL)
    california = breakdown(rollout, jurisdiction_id=CALIFORNIA)
    expected = property_tax_through(rollout, month=11) + usd(california, "total_tax_quanta")
    assert usd(federal, "salt_deduction_quanta") == pytest.approx(expected, rel=1e-5)
    assert usd(federal, "salt_deduction_quanta") > OBBBA_CAP


def test_an_authored_schedule_overrides_the_default() -> None:
    """The cap comes from the policy, not from a constant compiled into the engine."""
    rollout = run(
        standard_home(
            mortgage_interest_policies=(deducts(MORTGAGE_ID),),
            salt_policies=(salt(cap_schedule=(_SaltCap(effective_year_index=0, cap=money(5000)),)),),
        )
    )

    assert usd(breakdown(rollout, jurisdiction_id=FEDERAL), "salt_deduction_quanta") == pytest.approx(5_000.0, rel=1e-5)


if __name__ == "__main__":
    pytest_bazel.main()
