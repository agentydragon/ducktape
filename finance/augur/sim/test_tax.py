"""Independent tax controls: aggregate rounding, stacking, netting and exemptions."""

from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.compiler.tax import PreparedTaxBracket, PreparedTaxRules, PreparedThresholdTax
from finance.augur.sim.ids import AgentId, JurisdictionId
from finance.augur.sim.jurisdictions import JurisdictionLevel
from finance.augur.sim.money import MAX_COUNT
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome
from finance.augur.sim.tax import (
    IncomeLedger,
    NettedGains,
    TaxFacts,
    apply_brackets,
    assess,
    is_investment_income,
    net_capital_gains,
    taxes_interest_from,
    validate_rules,
)

# 3.8% over a $200,000 MAGI threshold, in quanta of $0.01.
NIIT = PreparedThresholdTax(rate_ppb=38_000_000, threshold=20_000_000)


@pytest.fixture
def federal() -> PreparedTaxRules:
    return PreparedTaxRules(
        jurisdiction_id=JurisdictionId("test_federal"),
        exempt_interest_from_levels=(JurisdictionLevel.STATE,),
        exempts_own_issue=False,
        ordinary_brackets=(
            PreparedTaxBracket(1_160_000, 100_000_000),
            PreparedTaxBracket(4_715_000, 120_000_000),
            PreparedTaxBracket(None, 220_000_000),
        ),
        long_term_capital_gain_brackets=(PreparedTaxBracket(4_702_500, 0), PreparedTaxBracket(None, 150_000_000)),
        standard_deduction=1_460_000,
        max_capital_loss_ordinary_offset=300_000,
        section_1250_rate_ppb=250_000_000,
    )


def test_bracket_tax_rounds_aggregate_once(federal: PreparedTaxRules) -> None:
    assert apply_brackets(2_000_000, federal.ordinary_brackets) == 216_800
    # Two individually sub-half-quantum slices form one taxable quantum together.
    assert apply_brackets(2, (PreparedTaxBracket(1, 400_000_000), PreparedTaxBracket(None, 400_000_000))) == 1


def test_preferential_gain_stacks_above_ordinary_income(federal: PreparedTaxRules) -> None:
    assessment = assess(TaxFacts(taxable_ordinary_income=5_000_000, long_term_gain=2_000_000), federal)
    assert assessment.ordinary_taxable == 3_540_000
    assert assessment.capital_gain_tax == 125_625


def test_section_1250_uses_incremental_brackets_below_the_rate_cap(federal: PreparedTaxRules) -> None:
    assessment = assess(TaxFacts(section_1250_recapture=1_454_545), federal)
    assert assessment.ordinary_taxable == 0
    assert assessment.section_1250_tax == 151_345
    assert assessment.capital_gain_tax == 151_345


def test_losses_cross_net_and_carry_forward(federal: PreparedTaxRules) -> None:
    assert net_capital_gains(-1_000_000, 200_000, 0, 300_000) == NettedGains(0, 0, 300_000, 500_000)
    assessment = assess(
        TaxFacts(taxable_ordinary_income=1_000_000, short_term_gain=-1_000_000, long_term_gain=200_000), federal
    )
    assert assessment.short_term_gain == 0
    assert assessment.long_term_gain == 0
    assert assessment.ordinary_loss_offset == 300_000
    assert assessment.capital_loss_carryforward == 500_000


def test_capital_gain_netting_reports_overflow() -> None:
    with pytest.raises(OverflowError, match="capital-gain netting"):
        net_capital_gains(MAX_COUNT, MAX_COUNT, 0, 300_000)


def test_rejects_negative_rule_amounts(federal: PreparedTaxRules) -> None:
    with pytest.raises(ValueError, match="standard_deduction must be nonnegative"):
        validate_rules(replace(federal, standard_deduction=-1))


def test_unused_standard_deduction_shelters_preferential_gain(federal: PreparedTaxRules) -> None:
    assessment = assess(TaxFacts(taxable_ordinary_income=460_000, long_term_gain=6_000_000), federal)
    assert assessment.ordinary_taxable == 0
    assert assessment.long_term_capital_gain_taxable == 5_000_000
    assert assessment.capital_gain_tax == 44_625


def test_carryforward_offsets_short_before_long() -> None:
    assert net_capital_gains(100, 200, 150, 30) == NettedGains(0, 150, 0, 0)
    assert net_capital_gains(100, 200, 350, 30) == NettedGains(0, 0, 30, 20)


def test_income_retains_exempt_sources_and_resets_only_one_taxpayer(federal: PreparedTaxRules) -> None:
    state_coupon = InterestIncome(issuer_jurisdiction_id=JurisdictionId("test_state"))
    income = IncomeLedger([ORDINARY_INCOME, state_coupon])
    income.enroll(AgentId("test_household"))
    income.enroll(AgentId("test_other"))
    income.accrue(AgentId("test_household"), ORDINARY_INCOME, 100)
    income.accrue(AgentId("test_household"), state_coupon, 20)
    income.accrue(AgentId("test_other"), ORDINARY_INCOME, 500)
    income.deduct_from_ordinary(AgentId("test_household"), 40)
    assert income.ordinary(AgentId("test_household")) == 60
    assert income.by_source[AgentId("test_household"), state_coupon] == 20
    assert not taxes_interest_from(federal, JurisdictionId("test_state"), JurisdictionLevel.STATE)
    assert taxes_interest_from(federal, None, None)
    assert taxes_interest_from(federal, JurisdictionId("test_unknown"), None)
    income.reset(AgentId("test_household"))
    assert income.ordinary(AgentId("test_household")) == 0
    assert income.by_source[AgentId("test_household"), state_coupon] == 0
    assert income.ordinary(AgentId("test_other")) == 500


def test_untaxed_recipients_do_not_acquire_income_rows() -> None:
    income = IncomeLedger([ORDINARY_INCOME])
    income.enroll(AgentId("test_household"))
    income.accrue(AgentId("test_counterparty"), ORDINARY_INCOME, 10)
    assert income.by_source == {("test_household", ORDINARY_INCOME): 0}


@pytest.mark.parametrize(
    ("facts", "niit"),
    [
        # MAGI exactly $200,000 is not over the threshold.
        (TaxFacts(taxable_ordinary_income=20_000_000, investment_income=5_000_000), 0),
        # MAGI $220,000 straddles it: 3.8% of the $20,000 excess, smaller than $30,000 NII.
        (TaxFacts(taxable_ordinary_income=22_000_000, investment_income=3_000_000), 76_000),
        # MAGI $310,000: the $10,000 of NII is smaller than the $110,000 excess.
        (TaxFacts(taxable_ordinary_income=31_000_000, investment_income=1_000_000), 38_000),
        # A $50,000 long-term gain is NII and MAGI: 3.8% of min(50,000, 100,000).
        (TaxFacts(taxable_ordinary_income=25_000_000, long_term_gain=5_000_000), 190_000),
        # A $10,000 short-term loss enters NII only as its $3,000 allowed offset:
        # MAGI 270,000 - 3,000 = 267,000; NII 20,000 - 3,000 = 17,000; 3.8% × 17,000.
        (TaxFacts(taxable_ordinary_income=27_000_000, investment_income=2_000_000, short_term_gain=-1_000_000), 64_600),
        # A $4,000 short-term loss nets against a $10,000 long-term gain: NII 6,000.
        (TaxFacts(taxable_ordinary_income=25_000_000, short_term_gain=-400_000, long_term_gain=1_000_000), 22_800),
        # A $5,000 carryforward absorbs most of an $8,000 gain: NII 3,000.
        (
            TaxFacts(taxable_ordinary_income=25_000_000, long_term_gain=800_000, capital_loss_carryforward=500_000),
            11_400,
        ),
        # Unrecaptured depreciation is gain on a disposition: $30,000 is NII and MAGI.
        (TaxFacts(taxable_ordinary_income=19_000_000, section_1250_recapture=3_000_000), 76_000),
    ],
)
def test_niit_is_the_rate_on_the_lesser_of_nii_and_the_magi_excess(
    federal: PreparedTaxRules, facts: TaxFacts, niit: int
) -> None:
    assert assess(facts, replace(federal, net_investment_income_tax=NIIT)).net_investment_income_tax == niit
    assert assess(facts, federal).net_investment_income_tax == 0


def test_niit_is_a_separate_component_of_the_total(federal: PreparedTaxRules) -> None:
    """Taxable 220,000 - 14,600 = 205,400 at 10/12/22%: 1160 + 4266 + 34815 = 40241; plus $760 NIIT."""
    assessment = assess(
        TaxFacts(taxable_ordinary_income=22_000_000, investment_income=3_000_000),
        replace(federal, net_investment_income_tax=NIIT),
    )
    assert (assessment.ordinary_tax, assessment.capital_gain_tax) == (4_024_100, 0)
    assert assessment.total_tax == 4_100_100


def test_only_interest_is_investment_income() -> None:
    assert is_investment_income(InterestIncome(issuer_jurisdiction_id=JurisdictionId("test_state")))
    assert not is_investment_income(ORDINARY_INCOME)


@pytest.mark.parametrize(
    ("facts", "surtax", "total"),
    [
        # Taxable income 1,005,363 - 5,363 = $1,000,000, not above the threshold.
        (TaxFacts(taxable_ordinary_income=100_536_300), 0, 10_000_000),
        # $1,000,001: 1% of the $1 over it, on top of the 10% bracket tax.
        (TaxFacts(taxable_ordinary_income=100_536_400), 1, 10_000_011),
        # Gains are taxable income here: 505,363 + 600,000 - 5,363 = $1,100,000.
        (TaxFacts(taxable_ordinary_income=50_536_300, long_term_gain=60_000_000), 100_000, 11_100_000),
    ],
)
def test_state_surtax_on_taxable_income_above_a_million(facts: TaxFacts, surtax: int, total: int) -> None:
    rules = PreparedTaxRules(
        jurisdiction_id=JurisdictionId("test_state"),
        exempt_interest_from_levels=(JurisdictionLevel.FEDERAL,),
        exempts_own_issue=True,
        ordinary_brackets=(PreparedTaxBracket(None, 100_000_000),),
        long_term_capital_gain_brackets=(),
        standard_deduction=536_300,
        max_capital_loss_ordinary_offset=300_000,
        section_1250_rate_ppb=0,
        taxable_income_surtax=PreparedThresholdTax(rate_ppb=10_000_000, threshold=100_000_000),
    )
    assessment = assess(facts, rules)
    assert assessment.taxable_income_surtax == surtax
    assert assessment.total_tax == total


if __name__ == "__main__":
    pytest_bazel.main()
