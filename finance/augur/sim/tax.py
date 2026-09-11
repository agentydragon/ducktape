"""Year-to-date tax facts and exact assessment of already-resolved jurisdiction rules."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from finance.augur.sim.compiler.tax import PreparedTaxBracket, PreparedTaxRules
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.jurisdictions import JurisdictionLevel
from finance.augur.sim.money import MAX_COUNT, checked_count, checked_wide, mul_div, round_ratio
from finance.augur.sim.scenario import ORDINARY_INCOME, TransferIncomeCategory


@dataclass
class TaxFacts:
    taxable_ordinary_income: int = 0
    short_term_gain: int = 0
    long_term_gain: int = 0
    section_1250_recapture: int = 0
    capital_loss_carryforward: int = 0
    itemized_deduction: int = 0
    rental_interest_deduction: int = 0
    depreciation_deduction: int = 0
    mortgage_interest_deduction: int = 0
    property_tax_paid: int = 0
    salt_deduction: int = 0


@dataclass(frozen=True)
class NettedGains:
    short_term: int
    long_term: int
    ordinary_offset: int
    carryforward: int


@dataclass(frozen=True, kw_only=True)
class TaxAssessment:
    short_term_gain: int
    long_term_gain: int
    ordinary_loss_offset: int
    ordinary_taxable: int
    long_term_capital_gain_taxable: int
    ordinary_tax: int
    capital_gain_tax: int
    section_1250_tax: int
    total_tax: int
    capital_loss_carryforward: int


class IncomeLedger:
    """Income remains per source, including income a particular jurisdiction exempts."""

    def __init__(self, taxpayers: Iterable[str], sources: Sequence[TransferIncomeCategory]) -> None:
        self.by_source = {(agent, source): 0 for agent in taxpayers for source in sources}

    def accrue(self, agent_id: str, source: TransferIncomeCategory, amount: int) -> None:
        key = (agent_id, source)
        if key in self.by_source:
            self.by_source[key] = checked_count(self.by_source[key] + amount, "money addition")

    def deduct_from_ordinary(self, agent_id: str, amount: int) -> None:
        self.accrue(agent_id, ORDINARY_INCOME, checked_count(-amount, "money negation"))

    def ordinary(self, agent_id: str) -> int:
        return self.by_source.get((agent_id, ORDINARY_INCOME), 0)

    def reset(self, agent_id: str) -> None:
        for key in self.by_source:
            if key[0] == agent_id:
                self.by_source[key] = 0


def taxes_interest_from(rules: PreparedTaxRules, issuer_id: str | None, issuer_level: JurisdictionLevel | None) -> bool:
    if issuer_id is None or issuer_level is None:
        return True
    if issuer_id == rules.jurisdiction_id:
        return not rules.exempts_own_issue
    return issuer_level not in rules.exempt_interest_from_levels


def validate_brackets(brackets: Sequence[PreparedTaxBracket]) -> None:
    if not brackets:
        raise ValueError("tax brackets are empty")
    previous = -1
    saw_open = False
    for bracket in brackets:
        if not 0 <= bracket.rate_ppb <= MONEY_FACTOR_SCALE:
            raise ValueError("tax rate is outside [0, 1000000000]")
        if saw_open or (bracket.upper is not None and bracket.upper <= previous):
            raise ValueError("tax bracket upper edges are not strictly increasing")
        if bracket.upper is None:
            saw_open = True
        else:
            previous = checked_count(bracket.upper, "tax bracket upper edge")
    if not saw_open:
        raise ValueError("tax bracket upper edges are not strictly increasing")


def validate_rules(rules: PreparedTaxRules) -> None:
    if rules.standard_deduction < 0:
        raise ValueError("standard_deduction must be nonnegative")
    if rules.max_capital_loss_ordinary_offset < 0:
        raise ValueError("max_capital_loss_ordinary_offset must be nonnegative")
    if not 0 <= rules.section_1250_rate_ppb <= MONEY_FACTOR_SCALE:
        raise ValueError("tax rate is outside [0, 1000000000]")
    validate_brackets(rules.ordinary_brackets)
    if rules.long_term_capital_gain_brackets:
        validate_brackets(rules.long_term_capital_gain_brackets)


def apply_brackets(amount: int, brackets: Sequence[PreparedTaxBracket]) -> int:
    return apply_stacked_brackets(amount, 0, brackets)


def apply_stacked_brackets(amount: int, lower_stack: int, brackets: Sequence[PreparedTaxBracket]) -> int:
    validate_brackets(brackets)
    total = checked_count(lower_stack + amount, "money addition")
    previous = 0
    numerator = 0
    for bracket in brackets:
        upper = MAX_COUNT if bracket.upper is None else bracket.upper
        width = max(0, min(total, upper) - max(lower_stack, previous))
        numerator = checked_wide(numerator + width * bracket.rate_ppb, "tax bracket accumulation")
        previous = upper
    return checked_count(round_ratio(numerator, MONEY_FACTOR_SCALE), "tax bracket rounding")


def net_capital_gains(
    short_term: int, long_term: int, carryforward_in: int, maximum_ordinary_offset: int
) -> NettedGains:
    short_loss = min(max(0, min(MAX_COUNT, -short_term)), max(0, long_term))
    short = checked_count(short_term + short_loss, "short-term loss netting")
    long = checked_count(long_term - short_loss, "long-term gain netting")
    long_loss = min(max(0, min(MAX_COUNT, -long)), max(0, short))
    long = checked_count(long + long_loss, "long-term loss netting")
    short = checked_count(short - long_loss, "short-term gain netting")
    carry = max(0, carryforward_in)
    used = min(max(0, short), carry)
    short -= used
    carry -= used
    used = min(max(0, long), carry)
    long -= used
    carry -= used
    net_gain = checked_count(short + long, "capital-gain netting")
    loss = checked_count(max(0, checked_count(-net_gain, "capital-loss netting")) + carry, "capital-loss carryforward")
    offset = min(loss, max(0, maximum_ordinary_offset))
    return NettedGains(max(0, short), max(0, long), offset, loss - offset)


def _taxable(ordinary: int, short: int, long: int, offset: int, deduction: int) -> int:
    total = checked_count(ordinary + short, "money addition")
    total = checked_count(total + long, "money addition")
    total = checked_count(total - offset, "money subtraction")
    return max(0, checked_count(total - deduction, "money subtraction"))


def assess(facts: TaxFacts, rules: PreparedTaxRules) -> TaxAssessment:
    validate_rules(rules)
    gains = net_capital_gains(
        facts.short_term_gain,
        facts.long_term_gain,
        facts.capital_loss_carryforward,
        rules.max_capital_loss_ordinary_offset,
    )
    deduction = max(facts.itemized_deduction, rules.standard_deduction)
    ordinary = facts.taxable_ordinary_income
    if rules.section_1250_rate_ppb == 0:
        ordinary = checked_count(ordinary + facts.section_1250_recapture, "money addition")
    total_taxable = _taxable(ordinary, gains.short_term, gains.long_term, gains.ordinary_offset, deduction)
    if rules.long_term_capital_gain_brackets:
        ordinary_taxable = _taxable(ordinary, gains.short_term, 0, gains.ordinary_offset, deduction)
        capital_taxable = checked_count(total_taxable - ordinary_taxable, "money subtraction")
        capital_tax = apply_stacked_brackets(capital_taxable, ordinary_taxable, rules.long_term_capital_gain_brackets)
    else:
        ordinary_taxable, capital_taxable, capital_tax = total_taxable, 0, 0
    ordinary_tax = apply_brackets(ordinary_taxable, rules.ordinary_brackets)
    recapture_tax = 0
    if rules.section_1250_rate_ppb > 0:
        with_recapture = apply_brackets(
            checked_count(ordinary_taxable + facts.section_1250_recapture, "money addition"), rules.ordinary_brackets
        )
        implied_tax = max(0, checked_count(with_recapture - ordinary_tax, "money subtraction"))
        cap = mul_div(
            facts.section_1250_recapture, rules.section_1250_rate_ppb, MONEY_FACTOR_SCALE, "section 1250 tax cap"
        )
        recapture_tax = min(implied_tax, cap)
    capital_tax = checked_count(capital_tax + recapture_tax, "money addition")
    return TaxAssessment(
        short_term_gain=gains.short_term,
        long_term_gain=gains.long_term,
        ordinary_loss_offset=gains.ordinary_offset,
        ordinary_taxable=ordinary_taxable,
        long_term_capital_gain_taxable=capital_taxable,
        ordinary_tax=ordinary_tax,
        capital_gain_tax=capital_tax,
        section_1250_tax=recapture_tax,
        total_tax=checked_count(ordinary_tax + capital_tax, "money addition"),
        capital_loss_carryforward=gains.carryforward,
    )
