//! Assemble this month's configured demands from terms and current contract/tax state.
//! Assembly does not choose funding, move money, or settle a claim. Runtime effects stay
//! attached to each amount so payment need not reconstruct mortgage or tax calculations.

use super::*;

/// A particular occurrence in one month's claim set, not a user-supplied cause label.
/// Handles are scoped to the containing rollout; another month's handle is stale.
#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
pub struct ClaimId {
    pub(super) month: u32,
    pub(super) index: usize,
}

pub(super) struct Claims {
    pub month: u32,
    pub entries: Vec<ActiveObligation>,
}

/// Configured order is scheduled demands, recurring demands, property charges, then tax.
/// The caller supplies the current month; this is not an actor-facing future-bill preview.
pub(super) fn assemble(
    fixture: &ExecutionInput,
    rollout_id: u32,
    month: u32,
    properties: &[PropertyState],
    mortgages: &[mortgages::Installment],
    tax_liabilities: &[TaxLiabilityState],
) -> Result<Claims, SimulationError> {
    let mut active_obligations = Vec::new();
    for obligation in fixture
        .scenario
        .obligations
        .iter()
        .filter(|obligation| obligation.month == month)
    {
        let Some(effect) = configured_obligation_effect(
            properties,
            obligation.property_id.as_deref(),
            obligation.deduction_category.as_deref(),
            obligation.deductible_fraction_ppb,
        ) else {
            continue;
        };
        active_obligations.push(ActiveObligation {
            paid: false,
            cause_id: format!("{}_m{month}", obligation.obligation_id),
            obligation_type: obligation.obligation_type.clone(),
            from: obligation.from.clone(),
            to: obligation.to.clone(),
            amount_due: amount_value(fixture, rollout_id, month, &obligation.amount_due)?,
            effect,
        });
    }
    for obligation in fixture
        .scenario
        .recurring_obligations
        .iter()
        .filter(|obligation| {
            obligation.start_month <= month && obligation.end_month.is_none_or(|end| month <= end)
        })
    {
        let Some(effect) = configured_obligation_effect(
            properties,
            obligation.property_id.as_deref(),
            obligation.deduction_category.as_deref(),
            obligation.deductible_fraction_ppb,
        ) else {
            continue;
        };
        active_obligations.push(ActiveObligation {
            paid: false,
            cause_id: format!("{}_m{month}", obligation.obligation_id),
            obligation_type: obligation.obligation_type.clone(),
            from: obligation.from.clone(),
            to: obligation.to.clone(),
            amount_due: amount_value(fixture, rollout_id, month, &obligation.amount_due)?,
            effect,
        });
    }
    active_obligations.extend(property_obligations(fixture, properties, mortgages, month)?);
    active_obligations.extend(tax_obligations(fixture, tax_liabilities, month)?);
    Ok(Claims {
        month,
        entries: active_obligations,
    })
}

#[derive(Clone, Debug)]
pub(super) enum ObligationEffect {
    None,
    /// Paying it writes this share of the amount off the payer's ordinary income. A
    /// property-tied obligation carries the property's rented fraction, which is the
    /// Schedule E share a mid-horizon rental transition resizes.
    OrdinaryDeduction {
        deductible_fraction_ppb: i64,
    },
    TaxPayment {
        profile_index: usize,
    },
    TaxTrueUp {
        profile_index: usize,
        tax_year_end_month: u32,
    },
    Mortgage(mortgages::Installment),
    PropertyTax {
        owner_agent_id: String,
        rented_fraction_ppb: i64,
    },
}

#[derive(Clone, Debug)]
pub(super) struct ActiveObligation {
    pub(super) cause_id: String,
    pub(super) obligation_type: String,
    pub(super) from: AccountRef,
    pub(super) to: AccountRef,
    pub(super) amount_due: Money,
    pub(super) effect: ObligationEffect,
    pub(super) paid: bool,
}

/// What settling one configured obligation does, or `None` when it does not accrue at all
/// this month because the property it is tied to is no longer on the books.
///
/// A property-tied obligation reads its deductible share off that property's rented fraction
/// at accrual time, which is the same fraction settlement would see: property lifecycle
/// events and purchases both run earlier in the month than obligation assembly.
fn configured_obligation_effect(
    properties: &[PropertyState],
    property_id: Option<&str>,
    deduction_category: Option<&str>,
    configured_fraction_ppb: i64,
) -> Option<ObligationEffect> {
    let deductible_fraction_ppb = match property_id {
        None => configured_fraction_ppb,
        Some(property_id) => {
            properties
                .iter()
                .find(|property| property.property_id == property_id && property.active)?
                .rented_fraction_ppb
        }
    };
    Some(if deduction_category == Some("ordinary") {
        ObligationEffect::OrdinaryDeduction {
            deductible_fraction_ppb,
        }
    } else {
        ObligationEffect::None
    })
}

fn property_obligations(
    fixture: &ExecutionInput,
    properties: &[PropertyState],
    mortgages: &[mortgages::Installment],
    month: u32,
) -> Result<Vec<ActiveObligation>, SimulationError> {
    let mut obligations = Vec::new();
    for installment in mortgages {
        let (purchase, loan) = mortgages::terms(fixture, &installment.liability_id)?;
        obligations.push(ActiveObligation {
            paid: false,
            cause_id: format!("{}_payment_m{month}", loan.liability_id),
            obligation_type: "mortgage_payment".into(),
            from: AccountRef::new(&purchase.buyer_agent_id, &purchase.buyer_account_id),
            to: AccountRef::new(&loan.lender_agent_id, &loan.lender_account_id),
            amount_due: installment.interest.checked_add(installment.principal)?,
            effect: ObligationEffect::Mortgage(installment.clone()),
        });
    }
    for policy in &fixture.scenario.property_tax_policies {
        let Some(property) = properties
            .iter()
            .find(|property| property.property_id == policy.property_id && property.active)
        else {
            continue;
        };
        if property.purchase_month >= month
            || policy.start_month > month
            || policy.end_month.is_some_and(|end| month > end)
        {
            continue;
        }
        let purchase = fixture
            .scenario
            .scheduled_property_purchases
            .iter()
            .find(|purchase| purchase.property_id == policy.property_id)
            .expect("validated property has a purchase");
        let location = fixture
            .scenario
            .locations
            .iter()
            .find(|location| location.location_id == property.location_id)
            .expect("validated property has a location");
        let rate = policy
            .annual_tax_rate_ppb
            .unwrap_or(location.annual_property_tax_rate_ppb);
        let annual_tax_numerator = i128::from(purchase.purchase_price.0)
            .checked_mul(i128::from(rate))
            .and_then(|value| {
                i128::from(location.annual_special_assessment.0)
                    .checked_mul(i128::from(WIRE_RATE_SCALE))
                    .and_then(|special| value.checked_add(special))
            })
            .ok_or(ArithmeticError::Overflow {
                operation: "property tax",
            })?;
        let amount_due = Money(
            i64::try_from(mul_div_i128_round_half_up(
                annual_tax_numerator,
                1,
                12 * i128::from(WIRE_RATE_SCALE),
                "property tax",
            )?)
            .map_err(|_| ArithmeticError::Overflow {
                operation: "property tax",
            })?,
        );
        obligations.push(ActiveObligation {
            paid: false,
            cause_id: format!("{}_property_tax_m{month}", policy.property_id),
            obligation_type: "property_tax".into(),
            from: AccountRef::new(&policy.owner_agent_id, &policy.from_account_id),
            to: AccountRef::new(
                &policy.tax_authority_agent_id,
                &policy.tax_authority_account_id,
            ),
            amount_due,
            effect: ObligationEffect::PropertyTax {
                owner_agent_id: policy.owner_agent_id.clone(),
                rented_fraction_ppb: property.rented_fraction_ppb,
            },
        });
    }
    Ok(obligations)
}

fn tax_obligations(
    fixture: &ExecutionInput,
    tax_liabilities: &[TaxLiabilityState],
    month: u32,
) -> Result<Vec<ActiveObligation>, SimulationError> {
    let Some(quarter) = estimated_tax_quarter(month) else {
        return Ok(Vec::new());
    };
    let mut obligations = Vec::new();
    if quarter <= 3 {
        for (profile_index, profile) in fixture.scenario.tax_profiles.iter().enumerate() {
            if profile.prior_year_tax.0 <= 0 {
                continue;
            }
            let amount_due = profile
                .prior_year_tax
                .scaled_by(Factor::new(1, 4), "quarterly estimated tax")?;
            if amount_due == Money(0) {
                continue;
            }
            obligations.push(ActiveObligation {
                paid: false,
                cause_id: format!(
                    "{}_estimated_tax_q{quarter}_y{}",
                    profile.agent_id,
                    month / 12
                ),
                obligation_type: "estimated_tax".into(),
                from: AccountRef::new(&profile.agent_id, &profile.payment_account_id),
                to: AccountRef::new(
                    &profile.tax_authority_agent_id,
                    &profile.tax_authority_account_id,
                ),
                amount_due,
                effect: ObligationEffect::TaxPayment { profile_index },
            });
        }
        return Ok(obligations);
    }

    let tax_year = month / 12 - 1;
    let tax_year_end_month = tax_year * 12 + 11;
    for (profile_index, profile) in fixture.scenario.tax_profiles.iter().enumerate() {
        let actual = tax_liabilities
            .iter()
            .filter(|liability| {
                liability.active
                    && liability.agent_id == profile.agent_id
                    && liability.tax_year_end_month == tax_year_end_month
            })
            .try_fold(Money(0), |total, liability| {
                total.checked_add(liability.amount_owed)
            })?;
        let safe_harbor = Money(profile.prior_year_tax.0.min(actual.0));
        let first_three_quarters = profile
            .prior_year_tax
            .scaled_by(Factor::new(3, 4), "first three estimated-tax quarters")?;
        let q4_due = Money((safe_harbor.0 - first_three_quarters.0).max(0));
        if q4_due != Money(0) {
            obligations.push(ActiveObligation {
                paid: false,
                cause_id: format!("{}_estimated_tax_q4_y{tax_year}", profile.agent_id),
                obligation_type: "estimated_tax".into(),
                from: AccountRef::new(&profile.agent_id, &profile.payment_account_id),
                to: AccountRef::new(
                    &profile.tax_authority_agent_id,
                    &profile.tax_authority_account_id,
                ),
                amount_due: q4_due,
                effect: ObligationEffect::TaxPayment { profile_index },
            });
        }
        let true_up_due = Money((actual.0 - safe_harbor.0).max(0));
        if true_up_due != Money(0) {
            obligations.push(ActiveObligation {
                paid: false,
                cause_id: format!("{}_tax_true_up_y{tax_year}", profile.agent_id),
                obligation_type: "tax_true_up".into(),
                from: AccountRef::new(&profile.agent_id, &profile.payment_account_id),
                to: AccountRef::new(
                    &profile.tax_authority_agent_id,
                    &profile.tax_authority_account_id,
                ),
                amount_due: true_up_due,
                effect: ObligationEffect::TaxTrueUp {
                    profile_index,
                    tax_year_end_month,
                },
            });
        }
    }
    Ok(obligations)
}

fn estimated_tax_quarter(month: u32) -> Option<u32> {
    match month % 12 {
        3 => Some(1),
        5 => Some(2),
        8 => Some(3),
        0 if month > 0 => Some(4),
        _ => None,
    }
}
