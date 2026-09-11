"""Independent tax controls: aggregate rounding, stacking, netting and exemptions."""

from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.compiler.tax import PreparedTaxBracket, PreparedTaxRules
from finance.augur.sim.jurisdictions import JurisdictionLevel
from finance.augur.sim.money import MAX_COUNT
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome
from finance.augur.sim.tax import (
    IncomeLedger,
    NettedGains,
    TaxFacts,
    apply_brackets,
    assess,
    net_capital_gains,
    taxes_interest_from,
    validate_rules,
)


@pytest.fixture
def federal() -> PreparedTaxRules:
    return PreparedTaxRules(
        jurisdiction_id="test_federal",
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
    state_coupon = InterestIncome(issuer_jurisdiction_id="test_state")
    income = IncomeLedger(["test_household", "test_other"], [ORDINARY_INCOME, state_coupon])
    income.accrue("test_household", ORDINARY_INCOME, 100)
    income.accrue("test_household", state_coupon, 20)
    income.accrue("test_other", ORDINARY_INCOME, 500)
    income.deduct_from_ordinary("test_household", 40)
    assert income.ordinary("test_household") == 60
    assert income.by_source["test_household", state_coupon] == 20
    assert not taxes_interest_from(federal, "test_state", JurisdictionLevel.STATE)
    assert taxes_interest_from(federal, None, None)
    assert taxes_interest_from(federal, "test_unknown", None)
    income.reset("test_household")
    assert income.ordinary("test_household") == 0
    assert income.by_source["test_household", state_coupon] == 0
    assert income.ordinary("test_other") == 500


def test_untaxed_recipients_do_not_acquire_income_rows() -> None:
    income = IncomeLedger(["test_household"], [ORDINARY_INCOME])
    income.accrue("test_counterparty", ORDINARY_INCOME, 10)
    assert income.by_source == {("test_household", ORDINARY_INCOME): 0}


if __name__ == "__main__":
    pytest_bazel.main()
