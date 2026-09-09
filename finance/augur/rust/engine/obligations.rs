//! Execute payments and record their canonical financial consequences. The configured
//! control groups obligations by payer/source account and settles each group all-or-none.

use super::*;

pub(super) fn mortgage_monthly_payment(
    principal: Money,
    annual_rate_ppb: i64,
    term_months: u32,
) -> Result<Money, SimulationError> {
    if annual_rate_ppb == 0 {
        return Ok(principal.scaled_by(
            Factor::new(1, i64::from(term_months)),
            "zero-rate mortgage payment",
        )?);
    }
    let monthly_rate = mul_div_i128_round_half_up(
        i128::from(annual_rate_ppb),
        CONTRACT_SCALE,
        12 * i128::from(WIRE_RATE_SCALE),
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
    fixture: &ExecutionInput,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax: &mut TaxState,
    properties: &[PropertyState],
    mortgages: &mut [MortgageState],
    tax_liabilities: &mut [TaxLiabilityState],
    month: u32,
    obligations: &[ActiveObligation],
    product_agent_id: Option<&str>,
    spending_obligation_index: Option<usize>,
) -> Result<Settlement, SimulationError> {
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
    let mut spending_paid = None;
    for (index, obligation) in obligations.iter().enumerate() {
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
                        tax,
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
                        tax,
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
                    let rental_interest = interest.scaled_by(
                        Factor::parts_per_billion(rented_fraction_ppb),
                        "rental mortgage interest",
                    )?;
                    mortgage.rental_interest_paid_ytd = mortgage
                        .rental_interest_paid_ytd
                        .checked_add(rental_interest)?;
                    record_rental_interest_deduction(tax, &mortgage.agent_id, rental_interest)?;
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
        if spending_obligation_index == Some(index) {
            spending_paid = Some(amount_paid);
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
    Ok(Settlement {
        failed: any_failure,
        product_shortfall,
        spending_paid,
    })
}

/// Actual payment outcome from the canonical funding groups.
pub(super) struct Settlement {
    pub(super) failed: bool,
    pub(super) product_shortfall: Money,
    /// None when this month contained no policy consumption demand.
    pub(super) spending_paid: Option<Money>,
}

fn target_allocation_attempted_sources(fixture: &ExecutionInput, account: &AccountRef) -> String {
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
