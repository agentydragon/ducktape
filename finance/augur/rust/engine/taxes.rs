//! Tax accrual and settlement: what each transaction contributes to a taxpayer's yearly
//! facts, the year-end assessment, and the payment/true-up path.

use super::*;

pub(super) fn record_transfer_income(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    recipient_agent_id: &str,
    income_category: Option<&str>,
    amount: Money,
) -> Result<(), SimulationError> {
    if income_category != Some("ordinary") {
        return Ok(());
    }
    for ((agent_id, _), facts) in tax_facts {
        if agent_id == recipient_agent_id {
            facts.ordinary_income = facts.ordinary_income.checked_add(amount)?;
        }
    }
    Ok(())
}

pub(super) fn record_transfer_deduction(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    payer_agent_id: &str,
    deduction_category: Option<&str>,
    amount: Money,
) -> Result<(), SimulationError> {
    if deduction_category != Some("ordinary") {
        return Ok(());
    }
    record_ordinary_deduction(tax_facts, payer_agent_id, amount, RATE_SCALE_PPB)
}

pub(super) fn record_ordinary_deduction(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    payer_agent_id: &str,
    amount: Money,
    deductible_fraction_ppb: i64,
) -> Result<(), SimulationError> {
    let deduction = Money(mul_div_round_half_up(
        amount.0,
        deductible_fraction_ppb,
        RATE_SCALE_PPB,
        "ordinary deduction",
    )?);
    for ((agent_id, _), facts) in tax_facts {
        if agent_id == payer_agent_id {
            facts.ordinary_income = facts.ordinary_income.checked_sub(deduction)?;
        }
    }
    Ok(())
}

pub(super) fn record_capital_gain(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    agent_id: &str,
    gain: Money,
    long_term: bool,
) -> Result<(), SimulationError> {
    for ((taxpayer, _), facts) in tax_facts {
        if taxpayer != agent_id {
            continue;
        }
        if long_term {
            facts.long_term_gain = facts.long_term_gain.checked_add(gain)?;
        } else {
            facts.short_term_gain = facts.short_term_gain.checked_add(gain)?;
        }
    }
    Ok(())
}

pub(super) fn record_section_1250_recapture(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    agent_id: &str,
    amount: Money,
) -> Result<(), SimulationError> {
    for ((taxpayer, _), facts) in tax_facts {
        if taxpayer == agent_id {
            facts.section_1250_recapture = facts.section_1250_recapture.checked_add(amount)?;
        }
    }
    Ok(())
}

pub(super) fn record_rental_interest_deduction(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    agent_id: &str,
    amount: Money,
) -> Result<(), SimulationError> {
    for ((taxpayer, _), facts) in tax_facts {
        if taxpayer == agent_id {
            facts.ordinary_income = facts.ordinary_income.checked_sub(amount)?;
            facts.rental_interest_deduction =
                facts.rental_interest_deduction.checked_add(amount)?;
        }
    }
    Ok(())
}

pub(super) fn record_property_tax_paid(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    agent_id: &str,
    amount: Money,
    rented_fraction_ppb: i64,
) -> Result<(), SimulationError> {
    let rental_deduction = Money(mul_div_round_half_up(
        amount.0,
        rented_fraction_ppb,
        RATE_SCALE_PPB,
        "rental property tax deduction",
    )?);
    let owner_property_tax = Money(mul_div_round_half_up(
        amount.0,
        RATE_SCALE_PPB - rented_fraction_ppb,
        RATE_SCALE_PPB,
        "owner property tax",
    )?);
    for ((taxpayer, _), facts) in tax_facts {
        if taxpayer == agent_id {
            facts.ordinary_income = facts.ordinary_income.checked_sub(rental_deduction)?;
            facts.property_tax_paid = facts.property_tax_paid.checked_add(owner_property_tax)?;
        }
    }
    Ok(())
}

fn mortgage_interest_deduction_for(
    fixture: &Fixture,
    mortgages: &[MortgageState],
    agent_id: &str,
    jurisdiction_id: &str,
) -> Result<Money, SimulationError> {
    let mut scaled_total = 0_i128;
    for policy in fixture
        .scenario
        .mortgage_interest_deduction_policies
        .iter()
        .filter(|policy| policy.owner_agent_id == agent_id)
    {
        let Some(mortgage) = mortgages
            .iter()
            .find(|mortgage| mortgage.liability_id == policy.liability_id)
        else {
            continue;
        };
        let owner_interest = mortgage
            .interest_paid_ytd
            .checked_sub(mortgage.rental_interest_paid_ytd)?;
        let origination_principal = fixture
            .scenario
            .scheduled_property_purchases
            .iter()
            .filter_map(|purchase| purchase.mortgage.as_ref())
            .find(|spec| spec.liability_id == policy.liability_id)
            .expect("validated MID policy has a mortgage")
            .principal;
        let factor_ppb = if policy.debt_class == "home_equity" {
            0
        } else {
            let cap = if policy.per_jurisdiction_principal_cap.is_empty() {
                origination_principal
            } else {
                policy
                    .per_jurisdiction_principal_cap
                    .get(jurisdiction_id)
                    .copied()
                    .unwrap_or(Money(0))
            };
            mul_div_round_half_up(
                cap.0.min(origination_principal.0),
                RATE_SCALE_PPB,
                origination_principal.0,
                "mortgage-interest principal factor",
            )?
        };
        let scaled = i128::from(owner_interest.0)
            .checked_mul(i128::from(factor_ppb))
            .ok_or(ArithmeticError::Overflow {
                operation: "mortgage-interest scaled deduction",
            })?;
        scaled_total = scaled_total
            .checked_add(scaled)
            .ok_or(ArithmeticError::Overflow {
                operation: "mortgage-interest aggregate deduction",
            })?;
    }
    let denominator = i128::from(RATE_SCALE_PPB);
    let rounded = scaled_total / denominator
        + i128::from(scaled_total % denominator >= (denominator + 1) / 2);
    Ok(Money(i64::try_from(rounded).map_err(|_| {
        ArithmeticError::Overflow {
            operation: "mortgage-interest aggregate deduction",
        }
    })?))
}

pub(super) fn accrue_year_end_taxes(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    tax_liabilities: &mut Vec<TaxLiabilityState>,
    mortgages: &[MortgageState],
    month: u32,
) -> Result<(), SimulationError> {
    for profile in &fixture.scenario.tax_profiles {
        let representative_key = (
            profile.agent_id.clone(),
            profile.jurisdictions[0].jurisdiction_id.clone(),
        );
        let representative = *tax_facts
            .get(&representative_key)
            .expect("validated tax profile has representative facts");
        let (net_short, net_long, ordinary_offset, shared_carryforward) = net_capital_gains(
            representative.short_term_gain,
            representative.long_term_gain,
            representative.capital_loss_carryforward,
            profile.jurisdictions[0].max_capital_loss_ordinary_offset,
        )?;
        for rules in &profile.jurisdictions {
            let facts = tax_facts
                .get_mut(&(profile.agent_id.clone(), rules.jurisdiction_id.clone()))
                .expect("validated tax profile has jurisdiction facts");
            facts.short_term_gain = net_short;
            facts.long_term_gain = net_long;
            facts.capital_loss_carryforward = Money(0);
            facts.ordinary_income = facts.ordinary_income.checked_sub(ordinary_offset)?;
        }
        let mut annual = BTreeMap::new();
        for rules in &profile.jurisdictions {
            let key = (profile.agent_id.clone(), rules.jurisdiction_id.clone());
            let facts = tax_facts
                .get_mut(&key)
                .expect("validated tax profile has initialized facts");
            facts.mortgage_interest_deduction = mortgage_interest_deduction_for(
                fixture,
                mortgages,
                &profile.agent_id,
                &rules.jurisdiction_id,
            )?;
            facts.salt_deduction = Money(0);
            facts.itemized_deduction = facts.mortgage_interest_deduction;
            let facts = *facts;
            let assessment = assess(facts, rules)?;
            annual.insert(rules.jurisdiction_id.clone(), (facts, assessment));
        }
        if let Some(policy) = fixture
            .scenario
            .federal_salt_deduction_policies
            .iter()
            .find(|policy| policy.profile_id == profile.agent_id)
        {
            let state_tax = annual
                .iter()
                .filter(|(jurisdiction_id, _)| {
                    jurisdiction_id.as_str() != policy.federal_jurisdiction_id
                })
                .try_fold(Money(0), |total, (_, (_, assessment))| {
                    total.checked_add(assessment.total_tax)
                })?;
            let property_tax = annual
                .get(&policy.federal_jurisdiction_id)
                .map_or(Money(0), |(facts, _)| facts.property_tax_paid);
            let salt_total = property_tax.checked_add(state_tax)?;
            let salt_cap = salt_cap_for(policy, month / 12);
            let salt_deduction = Money(salt_total.0.min(salt_cap.0));
            let federal_rules = profile
                .jurisdictions
                .iter()
                .find(|rules| rules.jurisdiction_id == policy.federal_jurisdiction_id)
                .expect("validated SALT policy has a federal tax link");
            let (facts, assessment) = annual
                .get_mut(&policy.federal_jurisdiction_id)
                .expect("validated SALT policy has annual facts");
            facts.salt_deduction = salt_deduction;
            facts.itemized_deduction = facts
                .mortgage_interest_deduction
                .checked_add(salt_deduction)?;
            *assessment = assess(*facts, federal_rules)?;
        }
        for rules in &profile.jurisdictions {
            let key = (profile.agent_id.clone(), rules.jurisdiction_id.clone());
            let (facts, assessment) = annual
                .remove(&rules.jurisdiction_id)
                .expect("annual tax assessment exists for every jurisdiction");
            let cause_id = format!(
                "{}_{}_year_end_accrual_m{month}",
                profile.agent_id, rules.jurisdiction_id
            );
            if assessment.total_tax != Money(0) {
                recorder.apply_entry(
                    ledger,
                    JournalEntry {
                        month,
                        cause_id: cause_id.clone(),
                        postings: vec![
                            Posting {
                                account: tax_expense_account(
                                    &profile.agent_id,
                                    &rules.jurisdiction_id,
                                ),
                                amount: assessment.total_tax,
                            },
                            Posting {
                                account: tax_liability_account(
                                    &profile.agent_id,
                                    &rules.jurisdiction_id,
                                ),
                                amount: assessment.total_tax.checked_neg()?,
                            },
                        ],
                    },
                )?;
            }
            tax_liabilities.push(TaxLiabilityState {
                agent_id: profile.agent_id.clone(),
                jurisdiction_id: rules.jurisdiction_id.clone(),
                tax_year_end_month: month,
                amount_owed: assessment.total_tax,
                active: true,
            });
            recorder.record_tax_accrual(TaxAccrual {
                month,
                cause_id,
                agent_id: profile.agent_id.clone(),
                jurisdiction_id: rules.jurisdiction_id.clone(),
                tax_year_end_month: month,
                ordinary_income: facts
                    .ordinary_income
                    .checked_sub(assessment.ordinary_loss_offset)?,
                short_term_gain: assessment.short_term_gain,
                long_term_gain: assessment.long_term_gain,
                section_1250_recapture: facts.section_1250_recapture,
                rental_interest_deduction: facts.rental_interest_deduction,
                depreciation_deduction: facts.depreciation_deduction,
                standard_deduction: rules.standard_deduction,
                mortgage_interest_deduction: facts.mortgage_interest_deduction,
                salt_deduction: facts.salt_deduction,
                itemized_deduction: facts.itemized_deduction,
                ordinary_taxable: assessment.ordinary_taxable,
                long_term_capital_gain_taxable: assessment.long_term_capital_gain_taxable,
                ordinary_tax: assessment.ordinary_tax,
                capital_gain_tax: assessment.capital_gain_tax,
                section_1250_tax: assessment.section_1250_tax,
                total_tax: assessment.total_tax,
                capital_loss_carryforward: shared_carryforward,
            })?;
            tax_facts.insert(
                key,
                TaxFacts {
                    capital_loss_carryforward: shared_carryforward,
                    ..TaxFacts::default()
                },
            );
        }
    }
    Ok(())
}

fn salt_cap_for(policy: &crate::fixture::FederalSaltDeductionSpec, year_index: u32) -> Money {
    if policy.cap_schedule.is_empty() {
        return Money(i64::MAX);
    }
    policy
        .cap_schedule
        .iter()
        .filter(|entry| entry.effective_year_index <= year_index)
        .max_by_key(|entry| entry.effective_year_index)
        .map_or(Money(0), |entry| entry.cap)
}

pub(super) fn book_tax_payment(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    month: u32,
    cause_id: &str,
    profile_index: usize,
    amount: Money,
) -> Result<(), SimulationError> {
    if amount == Money(0) {
        return Ok(());
    }
    let profile = &fixture.scenario.tax_profiles[profile_index];
    recorder.apply_entry(
        ledger,
        JournalEntry {
            month,
            cause_id: cause_id.into(),
            postings: vec![
                Posting {
                    account: AccountRef::new(&profile.agent_id, &profile.payment_account_id),
                    amount: amount.checked_neg()?,
                },
                Posting {
                    account: AccountRef::new(
                        &profile.tax_authority_agent_id,
                        &profile.tax_authority_account_id,
                    ),
                    amount,
                },
                Posting {
                    account: tax_prepayment_account(&profile.agent_id),
                    amount,
                },
                Posting {
                    account: tax_authority_revenue_account(&profile.tax_authority_agent_id),
                    amount: amount.checked_neg()?,
                },
            ],
        },
    )
}

pub(super) fn settle_tax_liabilities(
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_liabilities: &mut [TaxLiabilityState],
    month: u32,
    profile: &crate::fixture::TaxProfileSpec,
    tax_year_end_month: u32,
) -> Result<Money, SimulationError> {
    let matching: Vec<usize> = tax_liabilities
        .iter()
        .enumerate()
        .filter(|(_, liability)| {
            liability.active
                && liability.agent_id == profile.agent_id
                && liability.tax_year_end_month == tax_year_end_month
        })
        .map(|(index, _)| index)
        .collect();
    let total = matching.iter().try_fold(Money(0), |sum, index| {
        sum.checked_add(tax_liabilities[*index].amount_owed)
    })?;
    if total != Money(0) {
        let mut postings = matching
            .iter()
            .filter_map(|index| {
                let liability = &tax_liabilities[*index];
                (liability.amount_owed != Money(0)).then(|| Posting {
                    account: tax_liability_account(&liability.agent_id, &liability.jurisdiction_id),
                    amount: liability.amount_owed,
                })
            })
            .collect::<Vec<_>>();
        postings.push(Posting {
            account: tax_prepayment_account(&profile.agent_id),
            amount: total.checked_neg()?,
        });
        recorder.apply_entry(
            ledger,
            JournalEntry {
                month,
                cause_id: format!(
                    "{}_tax_settlement_y{}",
                    profile.agent_id,
                    (tax_year_end_month - 11) / 12
                ),
                postings,
            },
        )?;
    }
    for index in matching {
        tax_liabilities[index].amount_owed = Money(0);
    }
    Ok(total)
}

pub(super) fn record_interest_income(
    fixture: &Fixture,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    recipient_agent_id: &str,
    issuer_jurisdiction_id: Option<&str>,
    issuer_level: Option<JurisdictionLevel>,
    amount: Money,
) -> Result<(), SimulationError> {
    for profile in fixture
        .scenario
        .tax_profiles
        .iter()
        .filter(|profile| profile.agent_id == recipient_agent_id)
    {
        for rules in &profile.jurisdictions {
            if rules.taxes_interest_from(issuer_jurisdiction_id, issuer_level) {
                let facts = tax_facts
                    .get_mut(&(profile.agent_id.clone(), rules.jurisdiction_id.clone()))
                    .expect("validated tax facts must exist for every profile jurisdiction");
                facts.interest_income = facts.interest_income.checked_add(amount)?;
            }
        }
    }
    Ok(())
}

pub(super) fn jurisdiction_level(
    fixture: &Fixture,
    issuer_jurisdiction_id: Option<&str>,
) -> Option<JurisdictionLevel> {
    let issuer = issuer_jurisdiction_id?;
    fixture
        .scenario
        .jurisdictions
        .iter()
        .find(|jurisdiction| jurisdiction.jurisdiction_id == issuer)
        .map(|jurisdiction| jurisdiction.level)
}
