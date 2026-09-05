use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::money::{ArithmeticError, Money};

pub const RATE_SCALE: i64 = 1_000_000_000;

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum JurisdictionLevel {
    Federal,
    State,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct TaxFacts {
    pub ordinary_income: Money,
    pub interest_income: Money,
    pub short_term_gain: Money,
    pub long_term_gain: Money,
    pub section_1250_recapture: Money,
    pub capital_loss_carryforward: Money,
    pub itemized_deduction: Money,
    pub rental_interest_deduction: Money,
    pub depreciation_deduction: Money,
    pub mortgage_interest_deduction: Money,
    pub property_tax_paid: Money,
    pub salt_deduction: Money,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TaxBracket {
    /// Inclusive upper edge. `None` is the open-ended top bracket.
    pub upper: Option<Money>,
    /// Marginal rate in parts per billion.
    pub rate_ppb: i64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TaxRules {
    pub jurisdiction_id: String,
    #[serde(default)]
    pub exempt_interest_from_levels: Vec<JurisdictionLevel>,
    #[serde(default)]
    pub exempts_own_issue: bool,
    pub ordinary_brackets: Vec<TaxBracket>,
    #[serde(default)]
    pub long_term_capital_gain_brackets: Vec<TaxBracket>,
    pub standard_deduction: Money,
    pub max_capital_loss_ordinary_offset: Money,
    /// Positive means federal-style capped §1250 tax; zero routes recapture
    /// through ordinary brackets.
    #[serde(default)]
    pub section_1250_rate_ppb: i64,
}

impl TaxRules {
    pub fn taxes_interest_from(
        &self,
        issuer_jurisdiction_id: Option<&str>,
        issuer_level: Option<JurisdictionLevel>,
    ) -> bool {
        let (Some(issuer_jurisdiction_id), Some(issuer_level)) =
            (issuer_jurisdiction_id, issuer_level)
        else {
            return true;
        };
        if issuer_jurisdiction_id == self.jurisdiction_id {
            return !self.exempts_own_issue;
        }
        !self.exempt_interest_from_levels.contains(&issuer_level)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct TaxAssessment {
    pub short_term_gain: Money,
    pub long_term_gain: Money,
    pub ordinary_loss_offset: Money,
    pub ordinary_taxable: Money,
    pub long_term_capital_gain_taxable: Money,
    pub ordinary_tax: Money,
    pub capital_gain_tax: Money,
    pub section_1250_tax: Money,
    pub total_tax: Money,
    pub capital_loss_carryforward: Money,
}

#[derive(Debug, Error, Eq, PartialEq)]
pub enum TaxError {
    #[error("tax brackets are empty")]
    EmptyBrackets,
    #[error("tax bracket upper edges are not strictly increasing")]
    InvalidBrackets,
    #[error("tax rate {rate_ppb} is outside [0, {scale}]")]
    InvalidRate { rate_ppb: i64, scale: i64 },
    #[error("{field} must be nonnegative, got {value}")]
    NegativeRuleValue { field: &'static str, value: i64 },
    #[error(transparent)]
    Arithmetic(#[from] ArithmeticError),
}

pub fn validate_rules(rules: &TaxRules) -> Result<(), TaxError> {
    if rules.standard_deduction.0 < 0 {
        return Err(TaxError::NegativeRuleValue {
            field: "standard_deduction",
            value: rules.standard_deduction.0,
        });
    }
    if rules.max_capital_loss_ordinary_offset.0 < 0 {
        return Err(TaxError::NegativeRuleValue {
            field: "max_capital_loss_ordinary_offset",
            value: rules.max_capital_loss_ordinary_offset.0,
        });
    }
    if !(0..=RATE_SCALE).contains(&rules.section_1250_rate_ppb) {
        return Err(TaxError::InvalidRate {
            rate_ppb: rules.section_1250_rate_ppb,
            scale: RATE_SCALE,
        });
    }
    validate_brackets(&rules.ordinary_brackets)?;
    if !rules.long_term_capital_gain_brackets.is_empty() {
        validate_brackets(&rules.long_term_capital_gain_brackets)?;
    }
    Ok(())
}

pub fn assess(facts: TaxFacts, rules: &TaxRules) -> Result<TaxAssessment, TaxError> {
    validate_rules(rules)?;
    let (short_term, long_term, ordinary_offset, carryforward) = net_capital_gains(
        facts.short_term_gain,
        facts.long_term_gain,
        facts.capital_loss_carryforward,
        rules.max_capital_loss_ordinary_offset,
    )?;
    let deduction = facts.itemized_deduction.max(rules.standard_deduction);
    let federal_style_recapture = rules.section_1250_rate_ppb > 0;
    let taxable_ordinary_income = facts.ordinary_income.checked_add(facts.interest_income)?;
    let ordinary_for_brackets = if federal_style_recapture {
        taxable_ordinary_income
    } else {
        taxable_ordinary_income.checked_add(facts.section_1250_recapture)?
    };
    if rules.long_term_capital_gain_brackets.is_empty() {
        let taxable = nonnegative(
            ordinary_for_brackets
                .checked_add(short_term)?
                .checked_add(long_term)?
                .checked_sub(ordinary_offset)?
                .checked_sub(deduction)?,
        );
        let ordinary_tax = apply_brackets(taxable, &rules.ordinary_brackets)?;
        let section_1250_tax = if federal_style_recapture {
            capped_section_1250_tax(taxable, ordinary_tax, facts.section_1250_recapture, rules)?
        } else {
            Money(0)
        };
        return Ok(TaxAssessment {
            short_term_gain: short_term,
            long_term_gain: long_term,
            ordinary_loss_offset: ordinary_offset,
            ordinary_taxable: taxable,
            long_term_capital_gain_taxable: Money(0),
            ordinary_tax,
            capital_gain_tax: section_1250_tax,
            section_1250_tax,
            total_tax: ordinary_tax.checked_add(section_1250_tax)?,
            capital_loss_carryforward: carryforward,
        });
    }

    // §63 nets the deduction against taxable income, which includes the gain, and §1(h)
    // rates what is left of it — the order the Qualified Dividends and Capital Gain Tax
    // Worksheet starts from, opening at Form 1040 line 15. A deduction larger than ordinary
    // income therefore shelters part of the gain rather than going to waste.
    let total_taxable = nonnegative(
        ordinary_for_brackets
            .checked_add(short_term)?
            .checked_add(long_term)?
            .checked_sub(ordinary_offset)?
            .checked_sub(deduction)?,
    );
    let ordinary_taxable = nonnegative(
        ordinary_for_brackets
            .checked_add(short_term)?
            .checked_sub(ordinary_offset)?
            .checked_sub(deduction)?,
    );
    let capital_taxable = total_taxable.checked_sub(ordinary_taxable)?;
    let ordinary_tax = apply_brackets(ordinary_taxable, &rules.ordinary_brackets)?;
    let long_term_capital_gain_tax = apply_stacked_brackets(
        capital_taxable,
        ordinary_taxable,
        &rules.long_term_capital_gain_brackets,
    )?;
    let section_1250_tax = if federal_style_recapture {
        capped_section_1250_tax(
            ordinary_taxable,
            ordinary_tax,
            facts.section_1250_recapture,
            rules,
        )?
    } else {
        Money(0)
    };
    let capital_gain_tax = long_term_capital_gain_tax.checked_add(section_1250_tax)?;
    Ok(TaxAssessment {
        short_term_gain: short_term,
        long_term_gain: long_term,
        ordinary_loss_offset: ordinary_offset,
        ordinary_taxable,
        long_term_capital_gain_taxable: capital_taxable,
        ordinary_tax,
        capital_gain_tax,
        section_1250_tax,
        total_tax: ordinary_tax.checked_add(capital_gain_tax)?,
        capital_loss_carryforward: carryforward,
    })
}

fn capped_section_1250_tax(
    ordinary_taxable: Money,
    ordinary_tax: Money,
    recapture: Money,
    rules: &TaxRules,
) -> Result<Money, TaxError> {
    let ordinary_tax_with_recapture = apply_brackets(
        ordinary_taxable.checked_add(recapture)?,
        &rules.ordinary_brackets,
    )?;
    let implied_tax = nonnegative(ordinary_tax_with_recapture.checked_sub(ordinary_tax)?);
    let capped_tax = Money(crate::money::mul_div_round_half_up(
        recapture.0,
        rules.section_1250_rate_ppb,
        RATE_SCALE,
        "section 1250 tax cap",
    )?);
    Ok(implied_tax.min(capped_tax))
}

pub fn apply_brackets(amount: Money, brackets: &[TaxBracket]) -> Result<Money, TaxError> {
    validate_brackets(brackets)?;
    let mut previous = 0_i64;
    let mut numerator = 0_i128;
    for bracket in brackets {
        let upper = bracket.upper.map_or(i64::MAX, |value| value.0);
        let slice = amount.0.min(upper).saturating_sub(previous).max(0);
        numerator = numerator
            .checked_add(i128::from(slice) * i128::from(bracket.rate_ppb))
            .ok_or(ArithmeticError::Overflow {
                operation: "tax bracket accumulation",
            })?;
        previous = upper;
    }
    Ok(Money(round_positive_numerator(
        numerator,
        RATE_SCALE,
        "tax bracket rounding",
    )?))
}

pub fn apply_stacked_brackets(
    amount: Money,
    lower_stack: Money,
    brackets: &[TaxBracket],
) -> Result<Money, TaxError> {
    validate_brackets(brackets)?;
    let total = lower_stack.checked_add(amount)?;
    let mut previous = 0_i64;
    let mut numerator = 0_i128;
    for bracket in brackets {
        let upper = bracket.upper.map_or(i64::MAX, |value| value.0);
        let slice_top = total.0.min(upper);
        let slice_bottom = lower_stack.0.max(previous);
        let slice = slice_top.saturating_sub(slice_bottom).max(0);
        numerator = numerator
            .checked_add(i128::from(slice) * i128::from(bracket.rate_ppb))
            .ok_or(ArithmeticError::Overflow {
                operation: "capital-gain bracket accumulation",
            })?;
        previous = upper;
    }
    Ok(Money(round_positive_numerator(
        numerator,
        RATE_SCALE,
        "capital-gain bracket rounding",
    )?))
}

pub fn net_capital_gains(
    short_term: Money,
    long_term: Money,
    carryforward_in: Money,
    maximum_ordinary_offset: Money,
) -> Result<(Money, Money, Money, Money), ArithmeticError> {
    let mut short = short_term.0;
    let mut long = long_term.0;
    let short_loss_against_long = short.saturating_neg().max(0).min(long.max(0));
    short = short
        .checked_add(short_loss_against_long)
        .ok_or(ArithmeticError::Overflow {
            operation: "short-term loss netting",
        })?;
    long = long
        .checked_sub(short_loss_against_long)
        .ok_or(ArithmeticError::Overflow {
            operation: "long-term gain netting",
        })?;
    let long_loss_against_short = long.saturating_neg().max(0).min(short.max(0));
    long = long
        .checked_add(long_loss_against_short)
        .ok_or(ArithmeticError::Overflow {
            operation: "long-term loss netting",
        })?;
    short = short
        .checked_sub(long_loss_against_short)
        .ok_or(ArithmeticError::Overflow {
            operation: "short-term gain netting",
        })?;

    let mut carry = carryforward_in.0.max(0);
    let used_short = short.max(0).min(carry);
    short -= used_short;
    carry -= used_short;
    let used_long = long.max(0).min(carry);
    long -= used_long;
    carry -= used_long;
    let net_gain = short.checked_add(long).ok_or(ArithmeticError::Overflow {
        operation: "capital-gain netting",
    })?;
    let residual_loss = net_gain
        .checked_neg()
        .ok_or(ArithmeticError::Overflow {
            operation: "capital-loss netting",
        })?
        .max(0)
        .checked_add(carry)
        .ok_or(ArithmeticError::Overflow {
            operation: "capital-loss carryforward",
        })?;
    let ordinary_offset = residual_loss.min(maximum_ordinary_offset.0.max(0));
    Ok((
        Money(short.max(0)),
        Money(long.max(0)),
        Money(ordinary_offset),
        Money(residual_loss - ordinary_offset),
    ))
}

fn validate_brackets(brackets: &[TaxBracket]) -> Result<(), TaxError> {
    if brackets.is_empty() {
        return Err(TaxError::EmptyBrackets);
    }
    let mut previous = -1_i64;
    let mut saw_open = false;
    for bracket in brackets {
        if !(0..=RATE_SCALE).contains(&bracket.rate_ppb) {
            return Err(TaxError::InvalidRate {
                rate_ppb: bracket.rate_ppb,
                scale: RATE_SCALE,
            });
        }
        match bracket.upper {
            Some(upper) if !saw_open && upper.0 > previous => previous = upper.0,
            None if !saw_open => saw_open = true,
            _ => return Err(TaxError::InvalidBrackets),
        }
    }
    if !saw_open {
        return Err(TaxError::InvalidBrackets);
    }
    Ok(())
}

fn round_positive_numerator(
    numerator: i128,
    denominator: i64,
    operation: &'static str,
) -> Result<i64, ArithmeticError> {
    debug_assert!(numerator >= 0);
    let denominator = i128::from(denominator);
    let rounded =
        numerator / denominator + i128::from(numerator % denominator >= (denominator + 1) / 2);
    i64::try_from(rounded).map_err(|_| ArithmeticError::Overflow { operation })
}

fn nonnegative(value: Money) -> Money {
    Money(value.0.max(0))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn federal() -> TaxRules {
        TaxRules {
            jurisdiction_id: "federal_us".into(),
            exempt_interest_from_levels: vec![JurisdictionLevel::State],
            exempts_own_issue: false,
            ordinary_brackets: vec![
                TaxBracket {
                    upper: Some(Money(1_160_000)),
                    rate_ppb: 100_000_000,
                },
                TaxBracket {
                    upper: Some(Money(4_715_000)),
                    rate_ppb: 120_000_000,
                },
                TaxBracket {
                    upper: None,
                    rate_ppb: 220_000_000,
                },
            ],
            long_term_capital_gain_brackets: vec![
                TaxBracket {
                    upper: Some(Money(4_702_500)),
                    rate_ppb: 0,
                },
                TaxBracket {
                    upper: None,
                    rate_ppb: 150_000_000,
                },
            ],
            standard_deduction: Money(1_460_000),
            max_capital_loss_ordinary_offset: Money(300_000),
            section_1250_rate_ppb: 250_000_000,
        }
    }

    #[test]
    fn bracket_tax_rounds_aggregate_once() {
        let tax = apply_brackets(Money(2_000_000), &federal().ordinary_brackets).unwrap();
        assert_eq!(tax, Money(216_800));
    }

    #[test]
    fn preferential_gain_stacks_above_ordinary_income() {
        let assessment = assess(
            TaxFacts {
                ordinary_income: Money(5_000_000),
                long_term_gain: Money(2_000_000),
                ..TaxFacts::default()
            },
            &federal(),
        )
        .unwrap();
        assert_eq!(assessment.ordinary_taxable, Money(3_540_000));
        assert_eq!(assessment.capital_gain_tax, Money(125_625));
    }

    #[test]
    fn section_1250_uses_incremental_brackets_below_the_rate_cap() {
        let assessment = assess(
            TaxFacts {
                section_1250_recapture: Money(1_454_545),
                ..TaxFacts::default()
            },
            &federal(),
        )
        .unwrap();
        assert_eq!(assessment.ordinary_taxable, Money(0));
        assert_eq!(assessment.section_1250_tax, Money(151_345));
        assert_eq!(assessment.capital_gain_tax, Money(151_345));
    }

    #[test]
    fn losses_cross_net_and_carry_forward() {
        let (short, long, offset, carry) =
            net_capital_gains(Money(-1_000_000), Money(200_000), Money(0), Money(300_000)).unwrap();
        assert_eq!(
            (short, long, offset, carry),
            (Money(0), Money(0), Money(300_000), Money(500_000))
        );

        let assessment = assess(
            TaxFacts {
                ordinary_income: Money(1_000_000),
                short_term_gain: Money(-1_000_000),
                long_term_gain: Money(200_000),
                ..TaxFacts::default()
            },
            &federal(),
        )
        .unwrap();
        assert_eq!(assessment.short_term_gain, Money(0));
        assert_eq!(assessment.long_term_gain, Money(0));
        assert_eq!(assessment.ordinary_loss_offset, Money(300_000));
        assert_eq!(assessment.capital_loss_carryforward, Money(500_000));
    }

    #[test]
    fn capital_gain_netting_reports_overflow() {
        assert_eq!(
            net_capital_gains(Money(i64::MAX), Money(i64::MAX), Money(0), Money(300_000),),
            Err(ArithmeticError::Overflow {
                operation: "capital-gain netting",
            })
        );
    }

    #[test]
    fn rejects_negative_rule_amounts() {
        let mut rules = federal();
        rules.standard_deduction = Money(-1);
        assert_eq!(
            validate_rules(&rules),
            Err(TaxError::NegativeRuleValue {
                field: "standard_deduction",
                value: -1,
            })
        );
    }
}
