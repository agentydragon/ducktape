//! The property lifecycle: purchase, primary-residence assignment, rented-fraction and
//! capital-improvement events, depreciation accrual, and sale.

use super::*;

pub(super) fn execute_primary_residence_events(
    fixture: &Fixture,
    recorder: &mut Recorder,
    primary_residence_by_agent: &mut BTreeMap<String, Option<String>>,
    month: u32,
) -> Result<(), SimulationError> {
    let mut events: Vec<_> = fixture
        .scenario
        .primary_residence_events
        .iter()
        .filter(|event| event.month == month)
        .collect();
    events.sort_by_key(|event| &event.agent_id);
    for event in events {
        primary_residence_by_agent.insert(event.agent_id.clone(), event.property_id.clone());
        recorder.record_primary_residence(PrimaryResidenceOutcome {
            month,
            agent_id: event.agent_id.clone(),
            property_id: event.property_id.clone(),
            is_primary_residence: event.property_id.is_some(),
        })?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub(super) fn execute_property_lifecycle_events(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &mut [PropertyState],
    mortgages: &mut [MortgageState],
    primary_residence_by_agent: &mut BTreeMap<String, Option<String>>,
    month: u32,
) -> Result<(), SimulationError> {
    let property_ids: BTreeSet<_> = fixture
        .scenario
        .property_rented_fraction_events
        .iter()
        .filter(|event| event.month == month)
        .map(|event| event.property_id.as_str())
        .chain(
            fixture
                .scenario
                .capital_improvement_events
                .iter()
                .filter(|event| event.month == month)
                .map(|event| event.property_id.as_str()),
        )
        .chain(
            fixture
                .scenario
                .property_sales
                .iter()
                .filter(|event| event.month == month)
                .map(|event| event.property_id.as_str()),
        )
        .collect();
    for property_id in property_ids {
        for event in fixture
            .scenario
            .property_rented_fraction_events
            .iter()
            .filter(|event| event.month == month && event.property_id == property_id)
        {
            let Some(property) = properties
                .iter_mut()
                .find(|property| property.property_id == event.property_id && property.active)
            else {
                continue;
            };
            property.rented_fraction_ppb = event.rented_fraction_ppb;
            recorder.record_property_rented_fraction(PropertyRentedFractionOutcome {
                month,
                property_id: event.property_id.clone(),
                rented_fraction_ppb: event.rented_fraction_ppb,
            })?;
        }
        for event in fixture
            .scenario
            .capital_improvement_events
            .iter()
            .filter(|event| event.month == month && event.property_id == property_id)
        {
            let Some(property) = properties
                .iter_mut()
                .find(|property| property.property_id == event.property_id && property.active)
            else {
                continue;
            };
            let purchase = fixture
                .scenario
                .scheduled_property_purchases
                .iter()
                .find(|purchase| purchase.property_id == event.property_id)
                .expect("validated improvement has a purchase");
            recorder.apply_entry(
                ledger,
                JournalEntry {
                    month,
                    cause_id: format!("capital-improvement:{}:{month}", event.property_id),
                    postings: vec![
                        Posting {
                            account: AccountRef::new(
                                &purchase.buyer_agent_id,
                                &purchase.buyer_account_id,
                            ),
                            amount: event.amount.checked_neg()?,
                        },
                        Posting {
                            account: property_asset_account(
                                &purchase.buyer_agent_id,
                                &purchase.property_id,
                            ),
                            amount: event.amount,
                        },
                    ],
                },
            )?;
            property.building_basis = property.building_basis.checked_add(event.amount)?;
            recorder.record_capital_improvement(CapitalImprovementOutcome {
                month,
                property_id: event.property_id.clone(),
                amount: event.amount,
                // The existing lifecycle codec currently emits an empty
                // description for compiled improvement rows.
                description: String::new(),
            })?;
        }
        execute_property_sales(
            fixture,
            rollout_id,
            ledger,
            recorder,
            tax_facts,
            properties,
            mortgages,
            primary_residence_by_agent,
            month,
            property_id,
        )?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn execute_property_sales(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &mut [PropertyState],
    mortgages: &mut [MortgageState],
    primary_residence_by_agent: &mut BTreeMap<String, Option<String>>,
    month: u32,
    property_id: &str,
) -> Result<(), SimulationError> {
    let mut sales: Vec<&PropertySaleSpec> = fixture
        .scenario
        .property_sales
        .iter()
        .filter(|sale| sale.month == month && sale.property_id == property_id)
        .collect();
    sales.sort_by_key(|sale| &sale.property_id);
    for sale in sales {
        let Some(property_index) = properties
            .iter()
            .position(|property| property.property_id == sale.property_id && property.active)
        else {
            continue;
        };
        let property = &properties[property_index];
        let purchase = fixture
            .scenario
            .scheduled_property_purchases
            .iter()
            .find(|purchase| purchase.property_id == sale.property_id)
            .expect("validated property sale has a purchase");
        let series_id = format!("home_value:{}", purchase.location_id);
        let base_value = series_value(fixture, &series_id, rollout_id, 0)?;
        let sale_value = series_value(fixture, &series_id, rollout_id, month)?;
        let market_value = Money(mul_div_round_half_up(
            purchase.purchase_price.0,
            sale_value,
            base_value,
            "property market value",
        )?);
        let gross_proceeds = Money(mul_div_round_half_up(
            market_value.0,
            i64::from(10_000 - sale.closing_cost_bps),
            10_000,
            "property sale proceeds",
        )?);
        let mortgage_indices: Vec<_> = mortgages
            .iter()
            .enumerate()
            .filter(|(_, mortgage)| mortgage.property_id == sale.property_id && mortgage.active)
            .map(|(index, _)| index)
            .collect();
        let mortgage_payoff = mortgage_indices.iter().try_fold(Money(0), |total, index| {
            total.checked_add(mortgages[*index].principal)
        })?;
        let net_cash = gross_proceeds.checked_sub(mortgage_payoff)?;
        // Match the legacy contract exactly: capitalized buyer closing costs
        // enter the depreciable building basis, but the sale-gain formula uses
        // purchase price + later capex - cumulative depreciation.
        let capital_improvements = property
            .building_basis
            .checked_sub(property.building_basis_initial)?;
        let tax_adjusted_basis = purchase
            .purchase_price
            .checked_add(capital_improvements)?
            .checked_sub(property.cumulative_depreciation)?;
        let realized_gain = gross_proceeds.checked_sub(tax_adjusted_basis)?;
        let depreciation_recapture = Money(
            realized_gain
                .0
                .max(0)
                .min(property.cumulative_depreciation.0),
        );
        let post_recapture_gain =
            Money(realized_gain.checked_sub(depreciation_recapture)?.0.max(0));
        let qualifies_for_section_121 = property
            .owner_occupied_window
            .iter()
            .filter(|occupied| **occupied)
            .count()
            >= SECTION_121_MIN_QUALIFYING_MONTHS;
        let exclusion_cap = fixture
            .scenario
            .tax_profiles
            .iter()
            .find(|profile| profile.agent_id == purchase.buyer_agent_id)
            .map_or(Money(0), |profile| profile.section_121_exclusion);
        let section_121_exclusion = if qualifies_for_section_121 {
            Money(post_recapture_gain.0.min(exclusion_cap.0))
        } else {
            Money(0)
        };
        let long_term_capital_gain = post_recapture_gain.checked_sub(section_121_exclusion)?;
        let property_asset_balance = property.adjusted_basis.checked_add(capital_improvements)?;
        let basis_writeoff = property
            .adjusted_basis
            .checked_sub(purchase.purchase_price)?
            .checked_add(property.cumulative_depreciation)?;
        let mut postings = vec![
            Posting {
                account: AccountRef::new(&purchase.buyer_agent_id, &purchase.buyer_account_id),
                amount: net_cash,
            },
            Posting {
                account: property_asset_account(&purchase.buyer_agent_id, &purchase.property_id),
                amount: property_asset_balance.checked_neg()?,
            },
            Posting {
                account: property_basis_writeoff_account(
                    &purchase.buyer_agent_id,
                    &purchase.property_id,
                ),
                amount: basis_writeoff,
            },
            Posting {
                account: realized_gain_account(&purchase.buyer_agent_id),
                amount: realized_gain.checked_neg()?,
            },
        ];
        for index in &mortgage_indices {
            let mortgage = &mortgages[*index];
            postings.extend([
                Posting {
                    account: mortgage_liability_account(&mortgage.agent_id, &mortgage.liability_id),
                    amount: mortgage.principal,
                },
                Posting {
                    account: mortgage_receivable_account(
                        &mortgage.counterparty_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal.checked_neg()?,
                },
                Posting {
                    account: mortgage_funding_account(
                        &mortgage.counterparty_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal,
                },
            ]);
        }
        recorder.apply_entry(
            ledger,
            JournalEntry {
                month,
                cause_id: format!("property-sale:{}", sale.property_id),
                postings,
            },
        )?;
        properties[property_index].active = false;
        properties[property_index].rented_fraction_ppb = 0;
        properties[property_index].building_basis = Money(0);
        if primary_residence_by_agent
            .get(&purchase.buyer_agent_id)
            .is_some_and(|assignment| assignment.as_deref() == Some(&sale.property_id))
        {
            primary_residence_by_agent.insert(purchase.buyer_agent_id.clone(), None);
        }
        for index in mortgage_indices {
            mortgages[index].principal = Money(0);
            mortgages[index].active = false;
        }
        record_capital_gain(
            tax_facts,
            &purchase.buyer_agent_id,
            long_term_capital_gain,
            true,
        )?;
        record_section_1250_recapture(tax_facts, &purchase.buyer_agent_id, depreciation_recapture)?;
        recorder.record_property_sale(PropertySaleOutcome {
            month,
            property_id: sale.property_id.clone(),
            gross_proceeds,
            mortgage_payoff,
            net_cash_to_owner: net_cash,
            realized_gain,
            depreciation_recapture,
            section_121_exclusion,
            long_term_capital_gain,
        })?;
    }
    Ok(())
}

pub(super) fn accrue_primary_residence_occupancy(
    primary_residence_by_agent: &BTreeMap<String, Option<String>>,
    properties: &mut [PropertyState],
    month: u32,
) -> Result<(), SimulationError> {
    let window_index = usize::try_from(month).map_err(|_| ArithmeticError::Overflow {
        operation: "primary-residence window index",
    })? % SECTION_121_LOOKBACK_MONTHS;
    for property in properties {
        let occupied = property.active
            && property.rented_fraction_ppb < RATE_SCALE_PPB
            && primary_residence_by_agent
                .get(&property.owner_agent_id)
                .and_then(|assignment| assignment.as_deref())
                == Some(property.property_id.as_str());
        property.owner_occupied_window[window_index] = occupied;
        if occupied {
            property.owner_occupied_months =
                property
                    .owner_occupied_months
                    .checked_add(1)
                    .ok_or(ArithmeticError::Overflow {
                        operation: "primary-residence occupied-month count",
                    })?;
        }
    }
    Ok(())
}

pub(super) fn execute_property_purchases(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    properties: &mut Vec<PropertyState>,
    mortgages: &mut Vec<MortgageState>,
    month: u32,
) -> Result<(), SimulationError> {
    for purchase in fixture
        .scenario
        .scheduled_property_purchases
        .iter()
        .filter(|purchase| purchase.month == month)
    {
        let principal = purchase
            .mortgage
            .as_ref()
            .map_or(Money(0), |mortgage| mortgage.principal);
        let adjusted_basis = purchase
            .purchase_price
            .checked_add(purchase.buyer_closing_cost)?;
        let building_basis_initial = Money(mul_div_round_half_up(
            purchase.purchase_price.0,
            RATE_SCALE_PPB - purchase.land_value_fraction_ppb,
            RATE_SCALE_PPB,
            "property building basis",
        )?)
        .checked_add(purchase.buyer_closing_cost)?;
        let stake = purchase
            .down_payment
            .checked_add(purchase.buyer_closing_cost)?;
        let equity = purchase.purchase_price.checked_sub(principal)?;
        let buyer_cash = AccountRef::new(&purchase.buyer_agent_id, &purchase.buyer_account_id);
        let seller_cash = AccountRef::new(&purchase.seller_agent_id, &purchase.seller_account_id);
        let mut postings = vec![
            Posting {
                account: buyer_cash,
                amount: stake.checked_neg()?,
            },
            Posting {
                account: seller_cash,
                amount: stake,
            },
            Posting {
                account: property_asset_account(&purchase.buyer_agent_id, &purchase.property_id),
                amount: adjusted_basis,
            },
            Posting {
                account: property_sale_clearing_account(
                    &purchase.seller_agent_id,
                    &purchase.property_id,
                ),
                amount: stake.checked_neg()?,
            },
        ];
        let mut origination = None;
        if let Some(mortgage) = &purchase.mortgage {
            let monthly_payment = mortgage_monthly_payment(
                mortgage.principal,
                mortgage.annual_interest_rate_ppb,
                mortgage.term_months,
            )?;
            postings.extend([
                Posting {
                    account: mortgage_liability_account(
                        &purchase.buyer_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal.checked_neg()?,
                },
                Posting {
                    account: mortgage_receivable_account(
                        &mortgage.lender_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal,
                },
                Posting {
                    account: mortgage_funding_account(
                        &mortgage.lender_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal.checked_neg()?,
                },
            ]);
            mortgages.push(MortgageState {
                liability_id: mortgage.liability_id.clone(),
                property_id: purchase.property_id.clone(),
                agent_id: purchase.buyer_agent_id.clone(),
                payment_account_id: purchase.buyer_account_id.clone(),
                counterparty_agent_id: mortgage.lender_agent_id.clone(),
                counterparty_account_id: mortgage.lender_account_id.clone(),
                origination_month: month,
                annual_interest_rate_ppb: mortgage.annual_interest_rate_ppb,
                term_months: mortgage.term_months,
                monthly_payment,
                principal: mortgage.principal,
                interest_paid_ytd: Money(0),
                rental_interest_paid_ytd: Money(0),
                active: true,
            });
            origination = Some(MortgageOriginationOutcome {
                month,
                cause_id: format!("{}_mortgage_origination", purchase.cause_id),
                liability_id: mortgage.liability_id.clone(),
                agent_id: purchase.buyer_agent_id.clone(),
                payment_account_id: purchase.buyer_account_id.clone(),
                counterparty_agent_id: mortgage.lender_agent_id.clone(),
                counterparty_account_id: mortgage.lender_account_id.clone(),
                property_id: purchase.property_id.clone(),
                principal: mortgage.principal,
                annual_interest_rate_ppb: mortgage.annual_interest_rate_ppb,
                term_months: mortgage.term_months,
                monthly_payment,
            });
        } else {
            postings.push(Posting {
                account: property_sale_clearing_account(
                    &purchase.seller_agent_id,
                    &purchase.property_id,
                ),
                amount: principal,
            });
        }
        recorder.apply_entry(
            ledger,
            JournalEntry {
                month,
                cause_id: purchase.cause_id.clone(),
                postings,
            },
        )?;
        if stake.0 > 0 {
            recorder.record_transfer(TransferOutcome {
                month,
                cause_id: format!("{}_buyer_cash", purchase.cause_id),
                from: AccountRef::new(&purchase.buyer_agent_id, &purchase.buyer_account_id),
                to: AccountRef::new(&purchase.seller_agent_id, &purchase.seller_account_id),
                amount: stake,
                income_category: None,
            });
        }
        properties.push(PropertyState {
            property_id: purchase.property_id.clone(),
            location_id: purchase.location_id.clone(),
            owner_agent_id: purchase.buyer_agent_id.clone(),
            purchase_month: month,
            adjusted_basis,
            rented_fraction_ppb: purchase.rented_fraction_ppb,
            building_basis_initial,
            building_basis: building_basis_initial,
            cumulative_depreciation: Money(0),
            depreciation_ytd: Money(0),
            owner_occupied_months: 0,
            owner_occupied_window: vec![false; SECTION_121_LOOKBACK_MONTHS],
            contribution_used: stake,
            equity_ledger: equity,
            active: true,
        });
        recorder.record_property_purchase(
            PropertyPurchaseOutcome {
                month,
                cause_id: purchase.cause_id.clone(),
                property_id: purchase.property_id.clone(),
                location_id: purchase.location_id.clone(),
                buyer_agent_id: purchase.buyer_agent_id.clone(),
                purchase_price: purchase.purchase_price,
                closing_cost: purchase.buyer_closing_cost,
                adjusted_basis,
                stake_contribution: stake,
                equity_ledger: equity,
            },
            origination,
        )?;
    }
    Ok(())
}

pub(super) fn accrue_property_depreciation(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &mut [PropertyState],
) -> Result<(), SimulationError> {
    for property in properties
        .iter_mut()
        .filter(|property| property.active && property.rented_fraction_ppb > 0)
    {
        let monthly_factor_ppb = mul_div_round_half_up(
            property.rented_fraction_ppb,
            1,
            330,
            "property monthly depreciation factor",
        )?;
        let depreciation = Money(mul_div_round_half_up(
            property.building_basis.0,
            monthly_factor_ppb,
            RATE_SCALE_PPB,
            "property monthly depreciation",
        )?);
        property.cumulative_depreciation =
            property.cumulative_depreciation.checked_add(depreciation)?;
        property.depreciation_ytd = property.depreciation_ytd.checked_add(depreciation)?;
        for ((taxpayer, _), facts) in tax_facts.iter_mut() {
            if taxpayer == &property.owner_agent_id {
                facts.ordinary_income = facts.ordinary_income.checked_sub(depreciation)?;
                facts.depreciation_deduction =
                    facts.depreciation_deduction.checked_add(depreciation)?;
            }
        }
    }
    Ok(())
}

pub(super) fn reset_property_tax_year_state(
    properties: &mut [PropertyState],
    mortgages: &mut [MortgageState],
) {
    for property in properties {
        property.depreciation_ytd = Money(0);
    }
    for mortgage in mortgages {
        mortgage.interest_paid_ytd = Money(0);
        mortgage.rental_interest_paid_ytd = Money(0);
    }
}
