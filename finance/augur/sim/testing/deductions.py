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

from __future__ import annotations

from decimal import Decimal

import polars as pl
import pytest

from finance.augur.sim.fixed_point import round_currency_amount
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    FederalSaltCapEntry,
    FederalSaltDeductionPolicy,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    PropertyTaxPolicy,
    RecurringTransfer,
    Scenario,
    ScheduledPropertyPurchase,
)
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking, taxed
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

LOCATION_ID = "san_francisco"
LOCATIONS = {
    LOCATION_ID: Location(
        location_id=LOCATION_ID,
        display_name="San Francisco, CA",
        jurisdiction_ids=["federal_us", "california"],
        annual_property_tax_rate=0.01180,
        annual_special_assessment=0,
    )
}

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


def itemizer(
    *,
    purchase_price: Decimal | int,
    down_payment: Decimal | int,
    annual_rate: float,
    term_months: int,
    annual_w2_income: Decimal | int = 200_000,
    horizon_months: int = 13,
    mortgage_interest_deduction_policies: list[MortgageInterestDeductionPolicy] | None = None,
    federal_salt_deduction_policies: list[FederalSaltDeductionPolicy] | None = None,
) -> Scenario:
    """A single filer on W-2 wages who buys a financed San Francisco home at month 0.

    Wages are level and the purchase is at month 0, so every year of the horizon has the same
    income shape and any change between years belongs to the deduction rules rather than to
    the scenario.
    """

    return scenario(
        checking(
            ("alice", Decimal(down_payment) + Decimal(50_000)),
            ("payroll", Decimal(0)),
            ("irs", Decimal(0)),
            ("seller", Decimal(0)),
            ("bank", Decimal(0)),
            ("sf_tax_collector", Decimal(0)),
        ),
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=horizon_months - 1,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount=round_currency_amount(Decimal(annual_w2_income) / Decimal(12), quantum=Decimal("0.01")),
                income_category=ORDINARY_INCOME,
            )
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=0,
                cause_id="alice_buys_sf_home",
                property_id="sf_home",
                location_id=LOCATION_ID,
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price=purchase_price,
                down_payment=down_payment,
                buyer_closing_cost=0,
                mortgage=MortgageFinancing(
                    liability_id=MORTGAGE_ID,
                    lender_agent_id="bank",
                    principal=Decimal(purchase_price) - Decimal(down_payment),
                    annual_interest_rate=annual_rate,
                    term_months=term_months,
                ),
            )
        ],
        property_tax_policies=[
            PropertyTaxPolicy(
                property_id="sf_home",
                owner_agent_id="alice",
                tax_authority_agent_id="sf_tax_collector",
                annual_tax_rate=0.012,
            )
        ],
        mortgage_interest_deduction_policies=mortgage_interest_deduction_policies or [],
        federal_salt_deduction_policies=federal_salt_deduction_policies or [],
        tax_profiles=[taxed("alice", "federal_us", "california")],
        horizon_months=horizon_months,
    )


def _run(backend: Backend, scenario_: Scenario) -> SimulationResult:
    return backend(Case(scenario=scenario_, rollout_count=1, locations=LOCATIONS))


def standard_home(
    *,
    annual_w2_income: Decimal | int = 200_000,
    horizon_months: int = 13,
    mortgage_interest_deduction_policies: list[MortgageInterestDeductionPolicy] | None = None,
    federal_salt_deduction_policies: list[FederalSaltDeductionPolicy] | None = None,
) -> Scenario:
    """The $900k home on a $720k mortgage: first-year interest clears both standard deductions
    and the principal sits under the federal cap, so nothing but the policy under test binds."""

    return itemizer(
        purchase_price=900_000,
        down_payment=180_000,
        annual_rate=0.07,
        term_months=360,
        annual_w2_income=annual_w2_income,
        horizon_months=horizon_months,
        mortgage_interest_deduction_policies=mortgage_interest_deduction_policies,
        federal_salt_deduction_policies=federal_salt_deduction_policies,
    )


def small_home(
    *, mortgage_interest_deduction_policies: list[MortgageInterestDeductionPolicy] | None = None
) -> Scenario:
    """An $80k mortgage at 5%: first-year interest lands well under the federal standard deduction."""

    return itemizer(
        purchase_price=200_000,
        down_payment=120_000,
        annual_rate=0.05,
        term_months=360,
        mortgage_interest_deduction_policies=mortgage_interest_deduction_policies,
    )


def deducts(liability_id: str, owner_agent_id: str = "alice", **parts: str) -> MortgageInterestDeductionPolicy:
    return MortgageInterestDeductionPolicy(liability_id=liability_id, owner_agent_id=owner_agent_id, **parts)


def breakdown(result: SimulationResult, *, jurisdiction_id: str, year_index: int = 0) -> dict:
    """The year-end return one jurisdiction assessed, in the year that ends at month `12y + 11`."""

    month = 12 * year_index + 11
    rows = result.events.tax_breakdowns.filter(
        (pl.col("jurisdiction_id") == jurisdiction_id) & (pl.col("month_index") == month)
    )
    return rows.row(0, named=True)


def usd(row: dict, field: str) -> float:
    return row[field] / 100


def interest_through(result: SimulationResult, *, liability_id: str, month: int) -> float:
    """Interest actually paid on a liability up to and including `month`."""

    rows = result.events.mortgage_payments.filter(
        (pl.col("liability_id") == liability_id) & (pl.col("month_index") <= month)
    )
    return rows.get_column("interest_quanta").sum() / 100


def property_tax_through(result: SimulationResult, *, month: int) -> float:
    rows = result.events.obligation_settlements.filter(
        (pl.col("obligation_type") == "property_tax") & (pl.col("month_index") <= month)
    )
    return rows.get_column("amount_paid_quanta").sum() / 100


class DeductionAcceptance:
    """One engine, against what the deduction rules let an itemizing filer subtract."""

    def test_acquisition_interest_above_the_standard_deduction_is_itemized(self, backend: Backend) -> None:
        """A $720k mortgage at 7% throws off ~$46k of first-year interest, well past both standards.

        Both returns take the whole of it — the principal is under the federal cap — so the tax
        saved is exactly the excess over each jurisdiction's standard deduction at that
        jurisdiction's marginal rate, which $200k of wages leaves unchanged either way.
        """

        baseline = _run(backend, standard_home())
        deducted = _run(backend, standard_home(mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)]))

        interest = interest_through(deducted, liability_id=MORTGAGE_ID, month=11)
        assert interest > FEDERAL_STANDARD

        federal_baseline = breakdown(baseline, jurisdiction_id="federal_us")
        federal = breakdown(deducted, jurisdiction_id="federal_us")
        california_baseline = breakdown(baseline, jurisdiction_id="california")
        california = breakdown(deducted, jurisdiction_id="california")

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

    def test_home_equity_interest_is_not_deductible(self, backend: Backend) -> None:
        """TCJA suspended the home-equity deduction, so the tax must match having no policy at all.

        The interest is real and paid either way; only its deductibility changes. Comparing
        against the no-policy baseline rather than against zero is what catches an engine that
        classifies the debt correctly and then deducts it anyway.
        """

        baseline = _run(backend, standard_home())
        home_equity = _run(
            backend,
            standard_home(mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID, debt_class="home_equity")]),
        )

        for jurisdiction in ("federal_us", "california"):
            booked = breakdown(home_equity, jurisdiction_id=jurisdiction)
            assert usd(booked, "mortgage_interest_deduction_quanta") == pytest.approx(0.0, abs=1e-9)
            assert usd(booked, "total_tax_quanta") == pytest.approx(
                usd(breakdown(baseline, jurisdiction_id=jurisdiction), "total_tax_quanta"), abs=0.02
            )

    def test_acquisition_and_home_equity_debt_are_classified_per_liability(self, backend: Backend) -> None:
        """Two liabilities, one of each class: only the acquisition interest reaches the deduction.

        The classification is a property of the liability rather than of the taxpayer, so a
        filer holding both must see them split. A second financed purchase stands in for the
        HELOC: the liability bookkeeping is identical and only `debt_class` differs.
        """

        heloc_principal = Decimal(60_000)
        base = itemizer(
            purchase_price=Decimal(900_000),
            down_payment=Decimal(180_000),
            annual_rate=0.07,
            term_months=360,
            mortgage_interest_deduction_policies=[
                deducts(MORTGAGE_ID, debt_class="acquisition"),
                deducts(HELOC_ID, debt_class="home_equity"),
            ],
        )
        result = _run(
            backend,
            base.model_copy(
                update={
                    "scheduled_property_purchases": [
                        *base.scheduled_property_purchases,
                        ScheduledPropertyPurchase(
                            month=0,
                            cause_id="alice_opens_heloc",
                            property_id="alice_heloc_collateral",
                            location_id=LOCATION_ID,
                            buyer_agent_id="alice",
                            buyer_account_id="checking",
                            seller_agent_id="seller",
                            purchase_price=heloc_principal,
                            down_payment=0,
                            buyer_closing_cost=0,
                            mortgage=MortgageFinancing(
                                liability_id=HELOC_ID,
                                lender_agent_id="bank",
                                principal=heloc_principal,
                                annual_interest_rate=0.08,
                                term_months=360,
                            ),
                        ),
                    ]
                }
            ),
        )

        acquisition = interest_through(result, liability_id=MORTGAGE_ID, month=11)
        heloc = interest_through(result, liability_id=HELOC_ID, month=11)
        assert heloc > 0.0  # the excluded interest was really paid

        federal = breakdown(result, jurisdiction_id="federal_us")
        assert usd(federal, "mortgage_interest_deduction_quanta") == pytest.approx(acquisition, rel=1e-5)
        assert usd(federal, "mortgage_interest_deduction_quanta") < acquisition + heloc

    def test_without_a_policy_no_interest_is_deducted(self, backend: Backend) -> None:
        """A mortgage alone does not itemize a return; the standard deduction stands."""

        result = _run(
            backend, itemizer(purchase_price=900_000, down_payment=180_000, annual_rate=0.07, term_months=360)
        )

        federal = breakdown(result, jurisdiction_id="federal_us")
        california = breakdown(result, jurisdiction_id="california")
        assert usd(federal, "mortgage_interest_deduction_quanta") == 0.0
        assert usd(federal, "itemized_deduction_quanta") == 0.0
        assert usd(federal, "standard_deduction_quanta") == pytest.approx(FEDERAL_STANDARD)
        assert usd(california, "mortgage_interest_deduction_quanta") == 0.0
        assert usd(california, "itemized_deduction_quanta") == 0.0
        assert usd(california, "standard_deduction_quanta") == pytest.approx(CALIFORNIA_STANDARD)

    def test_the_federal_principal_cap_prorates_interest_and_california_does_not(self, backend: Backend) -> None:
        """An $850k mortgage: the federal return deducts 750/850 of the interest, California all of it.

        The two jurisdictions differ only in this cap, so a single run shows both readings and
        California itemizing strictly more is the check that they were not resolved together.
        """

        result = _run(
            backend,
            itemizer(
                purchase_price=1_050_000,
                down_payment=200_000,
                annual_rate=0.07,
                term_months=360,
                mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)],
            ),
        )

        interest = interest_through(result, liability_id=MORTGAGE_ID, month=11)
        federal = breakdown(result, jurisdiction_id="federal_us")
        california = breakdown(result, jurisdiction_id="california")
        assert usd(federal, "mortgage_interest_deduction_quanta") == pytest.approx(
            interest * (FEDERAL_PRINCIPAL_CAP / 850_000.0), rel=1e-5
        )
        assert usd(california, "mortgage_interest_deduction_quanta") == pytest.approx(interest, rel=1e-5)
        assert usd(california, "itemized_deduction_quanta") > usd(federal, "itemized_deduction_quanta")

    def test_interest_below_the_standard_deduction_is_still_reported(self, backend: Backend) -> None:
        """The return books what was itemized even when the standard deduction wins.

        $80k at 5% is far under the federal standard, so the tax is the no-policy tax. The
        itemized figure is still reported, which is how a consumer can tell that the standard
        deduction won rather than that nothing was deductible.
        """

        baseline = _run(backend, small_home())
        with_policy = _run(backend, small_home(mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)]))

        interest = interest_through(with_policy, liability_id=MORTGAGE_ID, month=11)
        assert interest < FEDERAL_STANDARD

        federal = breakdown(with_policy, jurisdiction_id="federal_us")
        assert usd(federal, "mortgage_interest_deduction_quanta") == pytest.approx(interest, rel=1e-5)
        assert usd(federal, "itemized_deduction_quanta") == pytest.approx(interest, rel=1e-5)
        assert usd(federal, "standard_deduction_quanta") == pytest.approx(FEDERAL_STANDARD)
        assert usd(federal, "total_tax_quanta") == pytest.approx(
            usd(breakdown(baseline, jurisdiction_id="federal_us"), "total_tax_quanta"), abs=0.02
        )

    def test_each_year_deducts_only_its_own_interest(self, backend: Backend) -> None:
        """Year two takes year two's interest, not everything paid since origination.

        The deduction is fed by an interest-to-date accumulator, so a year end that does not
        reset it produces a second-year deduction of the cumulative sum — larger than the
        first year's on a loan whose interest is falling, which is the shape asserted last.
        """

        result = _run(
            backend,
            itemizer(
                purchase_price=600_000,
                down_payment=200_000,
                annual_rate=0.07,
                term_months=360,
                horizon_months=25,
                mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)],
            ),
        )

        federal_years = result.events.tax_breakdowns.filter(pl.col("jurisdiction_id") == "federal_us").sort(
            "month_index"
        )
        assert federal_years.height == 2  # year ends at month 11 and month 23

        # Origination is month 0 and the first amortizing payment lands at month 1, so year one
        # carries eleven payments and year two carries twelve.
        year_1_interest = interest_through(result, liability_id=MORTGAGE_ID, month=11)
        year_2_interest = interest_through(result, liability_id=MORTGAGE_ID, month=23) - year_1_interest

        rows = federal_years.rows(named=True)
        assert usd(rows[0], "mortgage_interest_deduction_quanta") == pytest.approx(year_1_interest, rel=1e-5)
        assert usd(rows[1], "mortgage_interest_deduction_quanta") == pytest.approx(year_2_interest, rel=1e-5)

    def test_state_and_property_tax_under_the_cap_deduct_in_full(self, backend: Backend) -> None:
        """SALT is the California tax plus the property tax, and federal-only.

        Both figures are read back from the run — the state tax off California's own return,
        the property tax off what was actually settled — so this states the relationship
        rather than a pre-computed total.
        """

        result = _run(
            backend,
            standard_home(
                mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)],
                federal_salt_deduction_policies=[FederalSaltDeductionPolicy(profile_id="alice")],
            ),
        )

        federal = breakdown(result, jurisdiction_id="federal_us")
        california = breakdown(result, jurisdiction_id="california")
        expected = property_tax_through(result, month=11) + usd(california, "total_tax_quanta")
        assert expected < OBBBA_CAP

        assert usd(federal, "salt_deduction_quanta") == pytest.approx(expected, rel=1e-5)
        assert usd(federal, "itemized_deduction_quanta") == pytest.approx(
            usd(federal, "mortgage_interest_deduction_quanta") + expected, rel=1e-5
        )
        assert usd(california, "salt_deduction_quanta") == 0.0

    def test_state_and_property_tax_over_the_cap_clip_to_it(self, backend: Backend) -> None:
        """A $1.5M home on $1M of wages puts SALT far past the cap; the deduction is the cap."""

        result = _run(
            backend,
            itemizer(
                purchase_price=1_500_000,
                down_payment=400_000,
                annual_rate=0.07,
                term_months=360,
                annual_w2_income=1_000_000,
                mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)],
                federal_salt_deduction_policies=[FederalSaltDeductionPolicy(profile_id="alice")],
            ),
        )

        federal = breakdown(result, jurisdiction_id="federal_us")
        california = breakdown(result, jurisdiction_id="california")
        uncapped = property_tax_through(result, month=11) + usd(california, "total_tax_quanta")
        assert uncapped > OBBBA_CAP

        assert usd(federal, "salt_deduction_quanta") == pytest.approx(OBBBA_CAP, rel=1e-5)
        assert usd(federal, "itemized_deduction_quanta") == pytest.approx(
            usd(federal, "mortgage_interest_deduction_quanta") + OBBBA_CAP, rel=1e-5
        )

    def test_without_a_policy_no_state_or_property_tax_is_deducted(self, backend: Backend) -> None:
        """Paying state and property tax does not by itself put SALT on the return."""

        result = _run(backend, standard_home(mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)]))

        federal = breakdown(result, jurisdiction_id="federal_us")
        assert usd(federal, "salt_deduction_quanta") == 0.0
        assert usd(federal, "itemized_deduction_quanta") == pytest.approx(
            usd(federal, "mortgage_interest_deduction_quanta"), rel=1e-5
        )

    def test_the_cap_steps_down_on_its_scheduled_year(self, backend: Backend) -> None:
        """The OBBBA cap gives way to the TCJA cap from year four.

        Income and property are level across the five-year horizon, so the two years differ in
        nothing but which cap applies and the drop can only come from the schedule.
        """

        result = _run(
            backend,
            standard_home(
                horizon_months=60,
                mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)],
                federal_salt_deduction_policies=[FederalSaltDeductionPolicy(profile_id="alice")],
            ),
        )

        first = breakdown(result, jurisdiction_id="federal_us", year_index=0)
        stepped = breakdown(result, jurisdiction_id="federal_us", year_index=TCJA_CAP_YEAR)
        assert usd(stepped, "salt_deduction_quanta") == pytest.approx(TCJA_CAP, rel=1e-5)
        assert usd(first, "salt_deduction_quanta") > usd(stepped, "salt_deduction_quanta")

    def test_an_empty_schedule_is_no_cap_at_all(self, backend: Backend) -> None:
        """A full TCJA sunset is expressible: no entries means nothing clips.

        Absence of a cap has to be distinguishable from a very large one, because a sensitivity
        run that assumes the sunset is asking exactly that question.
        """

        result = _run(
            backend,
            itemizer(
                purchase_price=1_500_000,
                down_payment=400_000,
                annual_rate=0.07,
                term_months=360,
                annual_w2_income=1_000_000,
                mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)],
                federal_salt_deduction_policies=[FederalSaltDeductionPolicy(profile_id="alice", cap_schedule=[])],
            ),
        )

        federal = breakdown(result, jurisdiction_id="federal_us")
        california = breakdown(result, jurisdiction_id="california")
        expected = property_tax_through(result, month=11) + usd(california, "total_tax_quanta")
        assert usd(federal, "salt_deduction_quanta") == pytest.approx(expected, rel=1e-5)
        assert usd(federal, "salt_deduction_quanta") > OBBBA_CAP

    def test_an_authored_schedule_overrides_the_default(self, backend: Backend) -> None:
        """The cap comes from the policy, not from a constant compiled into the engine."""

        result = _run(
            backend,
            itemizer(
                purchase_price=900_000,
                down_payment=180_000,
                annual_rate=0.07,
                term_months=360,
                mortgage_interest_deduction_policies=[deducts(MORTGAGE_ID)],
                federal_salt_deduction_policies=[
                    FederalSaltDeductionPolicy(
                        profile_id="alice", cap_schedule=[FederalSaltCapEntry(effective_year_index=0, cap=5000)]
                    )
                ],
            ),
        )

        assert usd(breakdown(result, jurisdiction_id="federal_us"), "salt_deduction_quanta") == pytest.approx(
            5_000.0, rel=1e-5
        )
