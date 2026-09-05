//! Obligation assembly and settlement. Obligations sharing one payer and source account
//! settle all-or-none, which is what makes an unpaid obligation mean the portfolio
//! genuinely could not fund it.

use super::*;

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
    Mortgage {
        mortgage_index: usize,
        interest: Money,
        principal: Money,
    },
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
}

/// What settling one configured obligation does, or `None` when it does not accrue at all
/// this month because the property it is tied to is no longer on the books.
///
/// A property-tied obligation reads its deductible share off that property's rented fraction
/// at accrual time, which is the same fraction settlement would see: property lifecycle
/// events and purchases both run earlier in the month than obligation assembly.
pub(super) fn configured_obligation_effect(
    properties: &[PropertyState],
    property_id: Option<&str>,
    deduction_category: Option<&str>,
) -> Option<ObligationEffect> {
    let deductible_fraction_ppb = match property_id {
        None => RATE_SCALE_PPB,
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

pub(super) fn property_obligations(
    fixture: &Fixture,
    properties: &[PropertyState],
    mortgages: &[MortgageState],
    month: u32,
) -> Result<Vec<ActiveObligation>, SimulationError> {
    let mut obligations = Vec::new();
    for (index, mortgage) in mortgages.iter().enumerate() {
        if !mortgage.active || mortgage.origination_month >= month || mortgage.principal.0 <= 0 {
            continue;
        }
        let interest = Money(mul_div_round_half_up(
            mortgage.principal.0,
            mortgage.annual_interest_rate_ppb,
            12 * RATE_SCALE_PPB,
            "mortgage monthly interest",
        )?);
        let due = Money(
            mortgage
                .monthly_payment
                .0
                .min(mortgage.principal.checked_add(interest)?.0),
        );
        let principal = Money((due.0 - interest.0).max(0).min(mortgage.principal.0));
        obligations.push(ActiveObligation {
            cause_id: format!("{}_payment_m{month}", mortgage.liability_id),
            obligation_type: "mortgage_payment".into(),
            from: AccountRef::new(&mortgage.agent_id, &mortgage.payment_account_id),
            to: AccountRef::new(
                &mortgage.counterparty_agent_id,
                &mortgage.counterparty_account_id,
            ),
            amount_due: due,
            effect: ObligationEffect::Mortgage {
                mortgage_index: index,
                interest,
                principal,
            },
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
                    .checked_mul(i128::from(RATE_SCALE_PPB))
                    .and_then(|special| value.checked_add(special))
            })
            .ok_or(ArithmeticError::Overflow {
                operation: "property tax",
            })?;
        let amount_due = Money(
            i64::try_from(mul_div_i128_round_half_up(
                annual_tax_numerator,
                1,
                12 * i128::from(RATE_SCALE_PPB),
                "property tax",
            )?)
            .map_err(|_| ArithmeticError::Overflow {
                operation: "property tax",
            })?,
        );
        obligations.push(ActiveObligation {
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

pub(super) fn tax_obligations(
    fixture: &Fixture,
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
            let amount_due = Money(mul_div_round_half_up(
                profile.prior_year_tax.0,
                1,
                4,
                "quarterly estimated tax",
            )?);
            if amount_due == Money(0) {
                continue;
            }
            obligations.push(ActiveObligation {
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
        let first_three_quarters = Money(mul_div_round_half_up(
            profile.prior_year_tax.0,
            3,
            4,
            "first three estimated-tax quarters",
        )?);
        let q4_due = Money((safe_harbor.0 - first_three_quarters.0).max(0));
        if q4_due != Money(0) {
            obligations.push(ActiveObligation {
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

pub(super) fn mortgage_monthly_payment(
    principal: Money,
    annual_rate_ppb: i64,
    term_months: u32,
) -> Result<Money, SimulationError> {
    if annual_rate_ppb == 0 {
        return Ok(Money(mul_div_round_half_up(
            principal.0,
            1,
            i64::from(term_months),
            "zero-rate mortgage payment",
        )?));
    }
    let monthly_rate = mul_div_i128_round_half_up(
        i128::from(annual_rate_ppb),
        CONTRACT_SCALE,
        12 * i128::from(RATE_SCALE_PPB),
        "mortgage monthly rate",
    )?;
    let factor = CONTRACT_SCALE
        .checked_add(monthly_rate)
        .ok_or(ArithmeticError::Overflow {
            operation: "mortgage rate factor",
        })?;
    let mut discount = CONTRACT_SCALE;
    for _ in 0..term_months {
        discount = mul_div_i128_round_half_up(
            discount,
            CONTRACT_SCALE,
            factor,
            "mortgage discount factor",
        )?;
    }
    let denominator = CONTRACT_SCALE
        .checked_sub(discount)
        .ok_or(ArithmeticError::Overflow {
            operation: "mortgage annuity denominator",
        })?;
    let payment = mul_div_i128_round_half_up(
        i128::from(principal.0),
        monthly_rate,
        denominator,
        "mortgage payment",
    )?;
    Ok(Money(i64::try_from(payment).map_err(|_| {
        ArithmeticError::Overflow {
            operation: "mortgage payment",
        }
    })?))
}

#[allow(clippy::too_many_arguments)]
pub(super) fn settle_obligations(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &[PropertyState],
    mortgages: &mut [MortgageState],
    tax_liabilities: &mut [TaxLiabilityState],
    month: u32,
    obligations: &[ActiveObligation],
    product_agent_id: Option<&str>,
) -> Result<(bool, Money), SimulationError> {
    let mut due_by_source = BTreeMap::<AccountRef, Money>::new();
    for obligation in obligations {
        let due = due_by_source
            .get(&obligation.from)
            .copied()
            .unwrap_or_default()
            .checked_add(obligation.amount_due)?;
        due_by_source.insert(obligation.from.clone(), due);
    }
    let funded_by_source: BTreeMap<_, _> = due_by_source
        .into_iter()
        .map(|(account, due)| {
            let funded = ledger
                .balance(&account)
                .map(|available| available.0 >= due.0)?;
            Ok((account, funded))
        })
        .collect::<Result<_, LedgerError>>()?;

    let mut any_failure = false;
    let mut product_shortfall = Money(0);
    for obligation in obligations {
        let funded = funded_by_source[&obligation.from];
        let firing_id = obligation.cause_id.clone();
        let attempted_funding_sources =
            target_allocation_attempted_sources(fixture, &obligation.from);
        let is_tax_payment = matches!(
            obligation.effect,
            ObligationEffect::TaxPayment { .. } | ObligationEffect::TaxTrueUp { .. }
        );
        let (amount_paid, shortfall) = if funded {
            match obligation.effect {
                ObligationEffect::None => transfer_money(
                    ledger,
                    recorder,
                    month,
                    &firing_id,
                    &obligation.from,
                    &obligation.to,
                    obligation.amount_due,
                )?,
                ObligationEffect::OrdinaryDeduction {
                    deductible_fraction_ppb,
                } => {
                    transfer_money(
                        ledger,
                        recorder,
                        month,
                        &firing_id,
                        &obligation.from,
                        &obligation.to,
                        obligation.amount_due,
                    )?;
                    record_ordinary_deduction(
                        tax_facts,
                        &obligation.from.agent_id,
                        obligation.amount_due,
                        deductible_fraction_ppb,
                    )?;
                }
                ObligationEffect::PropertyTax {
                    ref owner_agent_id,
                    rented_fraction_ppb,
                } => {
                    transfer_money(
                        ledger,
                        recorder,
                        month,
                        &firing_id,
                        &obligation.from,
                        &obligation.to,
                        obligation.amount_due,
                    )?;
                    record_property_tax_paid(
                        tax_facts,
                        owner_agent_id,
                        obligation.amount_due,
                        rented_fraction_ppb,
                    )?;
                }
                ObligationEffect::TaxPayment { profile_index } => {
                    book_tax_payment(
                        fixture,
                        ledger,
                        recorder,
                        month,
                        &firing_id,
                        profile_index,
                        obligation.amount_due,
                    )?;
                }
                ObligationEffect::TaxTrueUp {
                    profile_index,
                    tax_year_end_month,
                } => {
                    book_tax_payment(
                        fixture,
                        ledger,
                        recorder,
                        month,
                        &firing_id,
                        profile_index,
                        obligation.amount_due,
                    )?;
                    let profile = &fixture.scenario.tax_profiles[profile_index];
                    let settled = settle_tax_liabilities(
                        ledger,
                        recorder,
                        tax_liabilities,
                        month,
                        profile,
                        tax_year_end_month,
                    )?;
                    recorder.record_tax_settlement(TaxSettlementOutcome {
                        month,
                        cause_id: format!(
                            "{}_tax_settlement_y{}",
                            profile.agent_id,
                            (tax_year_end_month - 11) / 12
                        ),
                        agent_id: profile.agent_id.clone(),
                        tax_year_end_month,
                        amount: settled,
                    })?;
                }
                ObligationEffect::Mortgage {
                    mortgage_index,
                    interest,
                    principal,
                } => {
                    let mortgage = &mut mortgages[mortgage_index];
                    recorder.apply_entry(
                        ledger,
                        JournalEntry {
                            month,
                            cause_id: firing_id.clone(),
                            postings: vec![
                                Posting {
                                    account: obligation.from.clone(),
                                    amount: obligation.amount_due.checked_neg()?,
                                },
                                Posting {
                                    account: obligation.to.clone(),
                                    amount: obligation.amount_due,
                                },
                                Posting {
                                    account: mortgage_liability_account(
                                        &mortgage.agent_id,
                                        &mortgage.liability_id,
                                    ),
                                    amount: principal,
                                },
                                Posting {
                                    account: mortgage_interest_expense_account(
                                        &mortgage.agent_id,
                                        &mortgage.liability_id,
                                    ),
                                    amount: interest,
                                },
                                Posting {
                                    account: mortgage_receivable_account(
                                        &mortgage.counterparty_agent_id,
                                        &mortgage.liability_id,
                                    ),
                                    amount: principal.checked_neg()?,
                                },
                                Posting {
                                    account: mortgage_interest_income_account(
                                        &mortgage.counterparty_agent_id,
                                        &mortgage.liability_id,
                                    ),
                                    amount: interest.checked_neg()?,
                                },
                            ],
                        },
                    )?;
                    mortgage.principal = mortgage.principal.checked_sub(principal)?;
                    mortgage.interest_paid_ytd =
                        mortgage.interest_paid_ytd.checked_add(interest)?;
                    let rented_fraction_ppb = properties
                        .iter()
                        .find(|property| property.property_id == mortgage.property_id)
                        .map_or(0, |property| property.rented_fraction_ppb);
                    let rental_interest = Money(mul_div_round_half_up(
                        interest.0,
                        rented_fraction_ppb,
                        RATE_SCALE_PPB,
                        "rental mortgage interest",
                    )?);
                    mortgage.rental_interest_paid_ytd = mortgage
                        .rental_interest_paid_ytd
                        .checked_add(rental_interest)?;
                    record_rental_interest_deduction(
                        tax_facts,
                        &mortgage.agent_id,
                        rental_interest,
                    )?;
                    if mortgage.principal == Money(0) {
                        mortgage.active = false;
                    }
                    recorder.record_mortgage_payment(MortgagePaymentOutcome {
                        month,
                        cause_id: firing_id.clone(),
                        liability_id: mortgage.liability_id.clone(),
                        agent_id: mortgage.agent_id.clone(),
                        counterparty_agent_id: mortgage.counterparty_agent_id.clone(),
                        property_id: mortgage.property_id.clone(),
                        from_account_id: mortgage.payment_account_id.clone(),
                        to_account_id: mortgage.counterparty_account_id.clone(),
                        interest,
                        principal,
                        total_payment: obligation.amount_due,
                    })?;
                }
            }
            (obligation.amount_due, Money(0))
        } else {
            any_failure = true;
            (Money(0), obligation.amount_due)
        };
        if is_tax_payment {
            recorder.record_tax_payment(TaxPaymentOutcome {
                month,
                cause_id: firing_id.clone(),
                agent_id: obligation.from.agent_id.clone(),
                obligation_type: obligation.obligation_type.clone(),
                amount_due: obligation.amount_due,
                amount_paid,
                shortfall,
            })?;
        }
        if amount_paid.0 > 0 {
            recorder.record_transfer(TransferOutcome {
                month,
                cause_id: firing_id.clone(),
                from: obligation.from.clone(),
                to: obligation.to.clone(),
                amount: amount_paid,
                income_category: None,
            });
        }
        if !funded {
            recorder.record_rollout_failure(RolloutFailureOutcome {
                month,
                cause_id: format!("{firing_id}_failure"),
                agent_id: obligation.from.agent_id.clone(),
                deficit: shortfall,
                obligation_id: firing_id.clone(),
                obligation_type: obligation.obligation_type.clone(),
                amount_due: obligation.amount_due,
                amount_paid,
                shortfall,
                attempted_funding_sources: attempted_funding_sources.clone(),
            });
        }
        recorder.record_obligation(ObligationOutcome {
            month,
            cause_id: firing_id.clone(),
            obligation_id: firing_id,
            obligation_type: obligation.obligation_type.clone(),
            from: obligation.from.clone(),
            to: obligation.to.clone(),
            amount_due: obligation.amount_due,
            amount_paid,
            shortfall,
            attempted_funding_sources,
            failure_active: !funded,
        });
        if product_agent_id.is_some_and(|agent_id| agent_id == obligation.from.agent_id) {
            product_shortfall = product_shortfall.checked_add(shortfall)?;
        }
    }
    Ok((any_failure, product_shortfall))
}

fn target_allocation_attempted_sources(fixture: &Fixture, account: &AccountRef) -> String {
    fixture
        .scenario
        .target_allocation_policies
        .iter()
        .find(|policy| {
            policy.agent_id == account.agent_id && policy.account_id == account.account_id
        })
        .map(|policy| {
            policy
                .sleeves
                .iter()
                .map(|sleeve| format!("security:{}", sleeve.asset_id))
                .collect::<Vec<_>>()
                .join(",")
        })
        .unwrap_or_default()
}
