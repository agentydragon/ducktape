//! Unit tests for the engine's internals.

use crate::execution::{
    AccountSpec, BondSpec, DistributionSpec, DistributionTaxSliceSpec, InitialLotSpec,
    JurisdictionIdentitySpec, LocationSpec, MortgageFinancingSpec, ObligationSpec,
    PropertyTaxPolicySpec, RecurringObligationSpec, ScenarioSpec, ScheduledPropertyPurchaseSpec,
    ScheduledSaleSpec, ScheduledTransferSpec, SeriesIndexedAmountKind, SeriesIndexedAmountSpec,
    SeriesSpec, SleeveTargetSpec, TargetAllocationPolicySpec, TaxProfileSpec,
};
use crate::tax::TaxBracket;

use super::*;

fn minimal_fixture() -> ExecutionInput {
    ExecutionInput {
        schema_version: INPUT_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 1,
        scenario: ScenarioSpec {
            horizon_months: 1,
            accounts: vec![AccountSpec {
                account: AccountRef::new("alice", "checking"),
                opening_balance: Money(0),
            }],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![],
            recurring_obligations: vec![],
            initial_lots: vec![],
            initial_bonds: vec![],
            scheduled_sales: vec![],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
            income_sources: vec![IncomeSource::Ordinary],
        },
        series: vec![],
    }
}

fn spending_fixture() -> (ExecutionInput, spending::Spending) {
    let mut fixture = minimal_fixture();
    fixture.rollout_count = 2;
    fixture.scenario.horizon_months = 13;
    fixture.scenario.accounts[0].opening_balance = Money(100_000);
    let spending = spending::Spending {
        from: fixture.scenario.accounts[0].account.clone(),
        to: AccountRef::new("world", "checking"),
        cause_id: "consumption".into(),
    };
    fixture.scenario.accounts.push(AccountSpec {
        account: spending.to.clone(),
        opening_balance: Money(0),
    });
    fixture.series.push(SeriesSpec {
        series_id: "inflation".into(),
        snapshots: 14,
        values: [
            vec![1_000_000_000; 14],
            vec![1_000_000_000; 12],
            vec![1_250_000_000; 2],
        ]
        .concat(),
    });
    (fixture, spending)
}

#[test]
fn executable_spending_matches_scheduled_funding_and_tax_events() {
    let (mut fixture, spending) = spending_fixture();
    fixture.scenario.accounts[0].opening_balance = Money(0);
    fixture.scenario.initial_lots.push(InitialLotSpec {
        lot_id: "stock".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        asset_id: "stock".into(),
        purchase_month: -24,
        quantity_scale: 1_000_000,
        units: Quantity(100_000_000),
        basis: Money(50_000),
    });
    fixture.series.push(SeriesSpec {
        series_id: "security:stock".into(),
        snapshots: 14,
        values: vec![1_000; 28],
    });
    fixture
        .scenario
        .target_allocation_policies
        .push(TargetAllocationPolicySpec {
            agent_id: "alice".into(),
            account_id: "checking".into(),
            source_account_ids: vec!["checking".into()],
            sleeves: vec![SleeveTargetSpec {
                asset_id: "stock".into(),
                weight: 1,
                quantity_scale: 1_000_000,
            }],
            cash_floor: Money(0).into(),
            cash_ceiling: Money(0).into(),
            cause_id_prefix: "fund".into(),
            allow_purchases: false,
            rebalance_tolerance_ppb: None,
        });
    // Deliberately synthetic flat tax: checks engine integration, not a jurisdiction's statute.
    fixture.scenario.tax_profiles.push(TaxProfileSpec {
        agent_id: "alice".into(),
        tax_authority_agent_id: "world".into(),
        payment_account_id: "checking".into(),
        tax_authority_account_id: "checking".into(),
        prior_year_tax: Money(0),
        section_121_exclusion: Money(0),
        jurisdictions: vec![TaxRules {
            jurisdiction_id: "test".into(),
            exempt_interest_from_levels: vec![],
            exempts_own_issue: false,
            ordinary_brackets: vec![TaxBracket {
                upper: None,
                rate_ppb: 200_000_000,
            }],
            long_term_capital_gain_brackets: vec![TaxBracket {
                upper: None,
                rate_ppb: 100_000_000,
            }],
            standard_deduction: Money(0),
            max_capital_loss_ordinary_offset: Money(0),
            section_1250_rate_ppb: 0,
        }],
    });
    fixture
        .scenario
        .recurring_obligations
        .push(RecurringObligationSpec {
            start_month: 0,
            end_month: None,
            obligation_id: spending.cause_id.clone(),
            obligation_type: "cash_spend".into(),
            from: spending.from.clone(),
            to: spending.to.clone(),
            amount_due: AmountSpec::SeriesIndexed(SeriesIndexedAmountSpec {
                kind: SeriesIndexedAmountKind::SeriesIndexed,
                base_amount: Money(1_000),
                series_id: "inflation".into(),
                base_month_index: 0,
                adjustment_period_months: 1,
            }),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: WIRE_RATE_SCALE,
        });
    let scheduled = simulate(&fixture).unwrap();
    let scheduled_metrics = simulate_product_metrics(&fixture, &spending.from.agent_id).unwrap();
    fixture.scenario.recurring_obligations.clear();
    let executable = spending::simulate(&fixture, &spending, |_| {
        |observation| Ok(Money(1_000).scaled_by(observation.price_level, "fixed real spending")?)
    })
    .unwrap();
    assert_eq!(
        serde_json::to_value(&executable).unwrap(),
        serde_json::to_value(&scheduled).unwrap()
    );
    let compact = spending::simulate_product_metrics(&fixture, &spending, |_| {
        |observation| Ok(Money(1_000).scaled_by(observation.price_level, "fixed real spending")?)
    })
    .unwrap();
    assert_eq!(compact, scheduled_metrics);
    for rollout in &executable.rollouts {
        assert_eq!(rollout.failed_month, None);
        assert!(!rollout.dispositions.is_empty());
        assert!(!rollout.tax_accruals.is_empty());
        assert!(!rollout.tax_payments.is_empty());
    }
}

#[test]
fn spending_functions_have_rollout_local_memory_and_stop_at_failure() {
    let (mut fixture, spending) = spending_fixture();
    fixture.scenario.accounts[0].opening_balance = Money(5);
    let output = spending::simulate(&fixture, &spending, |_| {
        let mut requested = 0;
        move |observation| {
            assert!(observation.month <= 2, "no decisions after failure");
            assert_eq!(observation.public_holdings, Money(0));
            assert_eq!(observation.cash, Money(5 - requested * (requested + 1) / 2));
            requested += 1;
            Ok(Money(requested))
        }
    })
    .unwrap();
    for rollout in output.rollouts {
        assert_eq!(rollout.failed_month, Some(2));
        assert_eq!(
            rollout
                .obligations
                .iter()
                .map(|o| o.amount_due)
                .collect::<Vec<_>>(),
            vec![Money(1), Money(2), Money(3)]
        );
    }
}

#[test]
fn compact_spending_matches_forensic_cash_and_shortfall_on_live_and_failed_paths() {
    let (mut fixture, spending) = spending_fixture();
    fixture.scenario.accounts[0].opening_balance = Money(5);
    let make_policy = |rollout| {
        let mut decisions = 0;
        move |observation: spending::Observation| {
            decisions += 1;
            assert_eq!(decisions, observation.month + 1);
            if rollout == 0 {
                assert!(observation.month <= 2, "no decisions after failure");
                Ok(Money(i64::from(decisions)))
            } else {
                Ok(Money(0))
            }
        }
    };
    let forensic = spending::simulate(&fixture, &spending, make_policy).unwrap();
    let compact = spending::simulate_product_metrics(&fixture, &spending, make_policy).unwrap();
    assert_eq!(compact.failed_month, vec![2, -1]);
    assert_eq!(compact.rollout_count, fixture.rollout_count);
    assert_eq!(compact.snapshot_count, fixture.scenario.horizon_months + 1);
    for rollout in &forensic.rollouts {
        assert_eq!(
            compact.failed_month[rollout.rollout_id as usize],
            rollout.failed_month.map_or(-1, i64::from)
        );
        for snapshot in &rollout.months {
            let index = (snapshot.month * compact.rollout_count + rollout.rollout_id) as usize;
            for (metric, values) in crate::product::BASE_METRIC_NAMES
                .iter()
                .zip(&compact.base_series)
            {
                let expected = match *metric {
                    "cash_quanta" => {
                        snapshot
                            .balances
                            .iter()
                            .find(|balance| balance.account == spending.from)
                            .unwrap()
                            .balance
                            .0
                    }
                    "shortfall_quanta" => rollout
                        .obligations
                        .iter()
                        .filter(|obligation| obligation.month + 1 == snapshot.month)
                        .map(|obligation| obligation.shortfall.0)
                        .sum(),
                    _ => 0,
                };
                assert_eq!(
                    values[index], expected,
                    "{metric} at snapshot {} rollout {}",
                    snapshot.month, rollout.rollout_id
                );
            }
        }
    }
}

#[test]
fn spending_rejects_invalid_inputs_and_negative_requests_but_allows_zero() {
    let (mut fixture, mut spending) = spending_fixture();
    let unchanged = spending::simulate(&fixture, &spending, |_| |_| Ok(Money(0))).unwrap();
    assert_eq!(
        serde_json::to_value(unchanged).unwrap(),
        serde_json::to_value(simulate(&fixture).unwrap()).unwrap()
    );
    assert!(matches!(
        spending::simulate(&fixture, &spending, |_| |_| Ok(Money(-1))),
        Err(SimulationError::InvalidAmount { .. })
    ));
    assert_eq!(
        spending::simulate_product_metrics(&fixture, &spending, |_| |_| Ok(Money(0))).unwrap(),
        simulate_product_metrics(&fixture, &spending.from.agent_id).unwrap()
    );
    assert!(matches!(
        spending::simulate_product_metrics(&fixture, &spending, |_| |_| Ok(Money(-1))),
        Err(SimulationError::InvalidAmount { .. })
    ));
    spending.cause_id = " ".into();
    assert!(matches!(
        spending::simulate(&fixture, &spending, |_| |_| panic!("invalid identifier")),
        Err(SimulationError::EmptyIdentifier { .. })
    ));
    spending.cause_id = "consumption".into();
    fixture.series[0].values[12] = 0;
    assert!(matches!(
        spending::simulate(&fixture, &spending, |_| |_| panic!(
            "invalid paths must be rejected before execution"
        )),
        Err(SimulationError::NonPositiveSeriesAmountLevel { .. })
    ));
    assert!(matches!(
        spending::simulate_product_metrics(
            &fixture,
            &spending,
            |_| -> fn(spending::Observation) -> Result<Money, SimulationError> {
                panic!("invalid paths must be rejected before constructing a policy")
            }
        ),
        Err(SimulationError::NonPositiveSeriesAmountLevel { .. })
    ));
    spending.to = AccountRef::new("absent", "checking");
    assert!(matches!(
        spending::simulate(&fixture, &spending, |_| |_| Ok(Money(1))),
        Err(SimulationError::UnknownAccountReference { .. })
    ));
}

/// One deterministic path: $100 cash and $1,000 of stock with $500 basis.
/// Prices/CPI are constant; no fees, distributions, housing or borrowing.
fn policy_timing_fixture(horizon_months: u32) -> (ExecutionInput, spending::Spending) {
    let (mut input, spending) = spending_fixture();
    input.rollout_count = 1;
    input.scenario.horizon_months = horizon_months;
    input.scenario.accounts[0].opening_balance = Money(10_000);
    input.series[0].snapshots = horizon_months + 1;
    input.series[0].values = vec![WIRE_RATE_SCALE; horizon_months as usize + 1];
    input.scenario.initial_lots.push(InitialLotSpec {
        lot_id: "timing-stock".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        asset_id: "stock".into(),
        purchase_month: -24,
        quantity_scale: 1_000_000,
        units: Quantity(100_000_000),
        basis: Money(50_000),
    });
    input.series.push(SeriesSpec {
        series_id: "security:stock".into(),
        snapshots: horizon_months + 1,
        values: vec![1_000; horizon_months as usize + 1],
    });
    input
        .scenario
        .target_allocation_policies
        .push(TargetAllocationPolicySpec {
            agent_id: "alice".into(),
            account_id: "checking".into(),
            source_account_ids: vec!["checking".into()],
            sleeves: vec![SleeveTargetSpec {
                asset_id: "stock".into(),
                weight: 1,
                quantity_scale: 1_000_000,
            }],
            cash_floor: Money(0).into(),
            cash_ceiling: Money(0).into(),
            cause_id_prefix: "timing-funding".into(),
            allow_purchases: false,
            rebalance_tolerance_ppb: None,
        });
    (input, spending)
}

#[test]
fn policy_timing_guardrail_and_unpaid_consumption() {
    let (mut input, spending) = policy_timing_fixture(1);
    input.scenario.obligations.push(ObligationSpec {
        month: 0,
        obligation_id: "existing-rent".into(),
        obligation_type: "outside_rent".into(),
        from: spending.from.clone(),
        to: spending.to.clone(),
        amount_due: Money(70_000).into(),
        property_id: None,
        deduction_category: None,
        deductible_fraction_ppb: WIRE_RATE_SCALE,
    });
    for allow_cut in [false, true] {
        let rollout = spending::simulate(&input, &spending, |_| {
            move |observation| {
                assert_eq!(observation.month, 0);
                assert_eq!(observation.cash, Money(10_000));
                assert_eq!(observation.public_holdings, Money(100_000));
                let gross_wealth = observation.cash.checked_add(observation.public_holdings)?;
                Ok(if allow_cut && gross_wealth < Money(120_000) {
                    Money(30_000)
                } else {
                    Money(50_000)
                })
            }
        })
        .unwrap()
        .rollouts
        .remove(0);
        assert_eq!(rollout.failed_month, if allow_cut { None } else { Some(0) });
        let consumption = rollout
            .obligations
            .iter()
            .find(|row| row.cause_id == "consumption_m0")
            .unwrap();
        assert_eq!(
            consumption.amount_due,
            Money(if allow_cut { 30_000 } else { 50_000 })
        );
        assert_eq!(
            consumption.amount_paid,
            Money(if allow_cut { 30_000 } else { 0 })
        );
        assert_eq!(
            consumption.shortfall,
            Money(if allow_cut { 0 } else { 50_000 })
        );
        let rent = rollout
            .obligations
            .iter()
            .find(|row| row.cause_id == "existing-rent_m0")
            .unwrap();
        assert_eq!(rent.amount_due, Money(70_000));
        assert_eq!(rent.amount_paid, Money(if allow_cut { 70_000 } else { 0 }));
        // Funding sales precede the group decision and are not rolled back on failure.
        assert_eq!(
            rollout
                .dispositions
                .iter()
                .map(|row| row.proceeds.0)
                .sum::<i64>(),
            if allow_cut { 90_000 } else { 100_000 }
        );
        assert!(rollout.tax_payments.is_empty());
    }
}

#[test]
fn policy_timing_surplus_investment_reserves_tax_and_consumption() {
    let (mut input, spending) = policy_timing_fixture(13);
    input
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 12,
            cause_id: "test-cash-contribution".into(),
            from: spending.to.clone(),
            to: spending.from.clone(),
            amount: Money(10_000).into(),
            income_category: None,
            deduction_category: None,
        });
    // Synthetic flat taxes test timing, not any real jurisdiction's rules.
    input.scenario.tax_profiles.push(TaxProfileSpec {
        agent_id: "alice".into(),
        tax_authority_agent_id: "world".into(),
        payment_account_id: "checking".into(),
        tax_authority_account_id: "checking".into(),
        prior_year_tax: Money(0),
        section_121_exclusion: Money(0),
        jurisdictions: vec![TaxRules {
            jurisdiction_id: "test-timing".into(),
            exempt_interest_from_levels: vec![],
            exempts_own_issue: false,
            ordinary_brackets: vec![TaxBracket {
                upper: None,
                rate_ppb: 200_000_000,
            }],
            long_term_capital_gain_brackets: vec![TaxBracket {
                upper: None,
                rate_ppb: 100_000_000,
            }],
            standard_deduction: Money(0),
            max_capital_loss_ordinary_offset: Money(0),
            section_1250_rate_ppb: 0,
        }],
    });
    for reinvest_surplus in [false, true] {
        input.scenario.target_allocation_policies[0].allow_purchases = reinvest_surplus;
        let rollout = spending::simulate(&input, &spending, |_| {
            |observation| {
                assert_eq!(
                    observation.cash,
                    Money(if observation.month == 0 { 10_000 } else { 0 })
                );
                assert_eq!(
                    observation.public_holdings,
                    Money(if observation.month == 0 {
                        100_000
                    } else {
                        60_000
                    })
                );
                // The month-12 observation precedes that month's $100 contribution.
                Ok(Money(match observation.month {
                    0 => 50_000,
                    12 => 5_000,
                    _ => 0,
                }))
            }
        })
        .unwrap()
        .rollouts
        .remove(0);
        assert_eq!(rollout.failed_month, None);
        assert_eq!(
            rollout
                .dispositions
                .iter()
                .map(|row| (row.month, row.proceeds, row.basis, row.realized_gain))
                .collect::<Vec<_>>(),
            vec![(0, Money(40_000), Money(20_000), Money(20_000))]
        );
        assert_eq!(
            rollout
                .tax_accruals
                .iter()
                .map(|row| (row.month, row.total_tax))
                .collect::<Vec<_>>(),
            vec![(11, Money(2_000))]
        );
        assert_eq!(
            rollout
                .tax_payments
                .iter()
                .map(|row| (row.month, row.amount_paid))
                .collect::<Vec<_>>(),
            vec![(12, Money(2_000))]
        );
        assert_eq!(
            rollout
                .obligations
                .iter()
                .filter(|row| row.obligation_type == "cash_spend")
                .map(|row| (row.month, row.amount_due, row.amount_paid))
                .collect::<Vec<_>>(),
            vec![
                (0, Money(50_000), Money(50_000)),
                (12, Money(5_000), Money(5_000))
            ]
        );
        let final_month = rollout.months.last().unwrap();
        assert_eq!(
            final_month
                .balances
                .iter()
                .find(|row| row.account == spending.from)
                .unwrap()
                .balance,
            Money(if reinvest_surplus { 0 } else { 3_000 })
        );
        let new_lots = final_month
            .lots
            .iter()
            .filter(|lot| lot.purchase_month == 12)
            .collect::<Vec<_>>();
        if reinvest_surplus {
            assert_eq!(new_lots.len(), 1);
            assert_eq!(new_lots[0].units_remaining, Quantity(3_000_000));
            assert_eq!(new_lots[0].basis_remaining, Money(3_000));
        } else {
            assert!(new_lots.is_empty());
        }
    }
}

#[test]
fn rejects_invalid_fixture_metadata() {
    let mut fixture = minimal_fixture();
    fixture.rollout_count = 0;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::EmptyRollouts)
    ));

    let mut fixture = minimal_fixture();
    fixture.scenario.horizon_months = 0;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::EmptyHorizon)
    ));

    let mut fixture = minimal_fixture();
    fixture.currency_code = "usd".into();
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidCurrencyCode { .. })
    ));

    let mut fixture = minimal_fixture();
    fixture.currency_quantum = "0".into();
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidCurrencyQuantum { .. })
    ));
}

#[test]
fn series_indexed_amounts_follow_rollout_specific_reset_boundaries() {
    let mut fixture = minimal_fixture();
    fixture.rollout_count = 2;
    fixture.scenario.horizon_months = 13;
    fixture.scenario.accounts.push(AccountSpec {
        account: AccountRef::new("landlord", "checking"),
        opening_balance: Money(0),
    });
    fixture.scenario.accounts[0].opening_balance = Money(100_000);
    fixture.scenario.recurring_obligations = vec![RecurringObligationSpec {
        start_month: 0,
        end_month: Some(12),
        obligation_id: "rent".into(),
        obligation_type: "cash_spend".into(),
        from: AccountRef::new("alice", "checking"),
        to: AccountRef::new("landlord", "checking"),
        amount_due: AmountSpec::SeriesIndexed(SeriesIndexedAmountSpec {
            kind: SeriesIndexedAmountKind::SeriesIndexed,
            base_amount: Money(1_001),
            series_id: "rent:test".into(),
            base_month_index: 0,
            adjustment_period_months: 12,
        }),
        property_id: None,
        deduction_category: None,
        deductible_fraction_ppb: WIRE_RATE_SCALE,
    }];
    fixture.series = vec![SeriesSpec {
        series_id: "rent:test".into(),
        snapshots: 14,
        values: [
            vec![1_000_000_000; 12],
            vec![1_100_000_000, 1_100_000_000],
            vec![1_000_000_000; 12],
            vec![1_250_000_000, 1_250_000_000],
        ]
        .concat(),
    }];

    let output = simulate(&fixture).unwrap();
    assert_eq!(output.rollouts[0].obligations[11].amount_due, Money(1_001));
    assert_eq!(output.rollouts[0].obligations[12].amount_due, Money(1_101));
    assert_eq!(output.rollouts[1].obligations[11].amount_due, Money(1_001));
    assert_eq!(output.rollouts[1].obligations[12].amount_due, Money(1_251));
}

#[test]
fn series_indexed_amount_validation_rejects_invalid_paths() {
    let amount = AmountSpec::SeriesIndexed(SeriesIndexedAmountSpec {
        kind: SeriesIndexedAmountKind::SeriesIndexed,
        base_amount: Money(1),
        series_id: "inflation".into(),
        base_month_index: 1,
        adjustment_period_months: 12,
    });
    let mut fixture = minimal_fixture();
    fixture.scenario.horizon_months = 2;
    fixture
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 0,
            cause_id: "too-early".into(),
            from: AccountRef::new("alice", "checking"),
            to: AccountRef::new("alice", "checking"),
            amount,
            income_category: None,
            deduction_category: None,
        });
    fixture.series.push(SeriesSpec {
        series_id: "inflation".into(),
        snapshots: 3,
        values: vec![1_000_000_000, 1_000_000_000, 1_000_000_000],
    });
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::SeriesAmountBeforeBase {
            month: 0,
            base_month: 1,
            ..
        })
    ));

    fixture.scenario.scheduled_transfers[0].month = 1;
    fixture.series[0].values[1] = 0;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::NonPositiveSeriesAmountLevel {
            month: 1,
            value: 0,
            ..
        })
    ));

    fixture.series[0].values[1] = 1_000_000_000;
    if let AmountSpec::SeriesIndexed(amount) = &mut fixture.scenario.scheduled_transfers[0].amount {
        amount.adjustment_period_months = 0;
    }
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidSeriesIndexedAmount { .. })
    ));

    if let AmountSpec::SeriesIndexed(amount) = &mut fixture.scenario.scheduled_transfers[0].amount {
        amount.adjustment_period_months = 1;
        amount.series_id = "security:vti".into();
    }
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnsupportedAmountSeries { .. })
    ));

    if let AmountSpec::SeriesIndexed(amount) = &mut fixture.scenario.scheduled_transfers[0].amount {
        amount.series_id = "inflation".into();
    }
    fixture.series[0].values[1] = (1_i64 << 53) + 1;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InexactSeriesAmountLevel { month: 1, .. })
    ));
}

#[test]
fn nominal_and_indexed_bonds_follow_coupon_redemption_and_accretion_contracts() {
    let mut fixture = minimal_fixture();
    fixture
        .scenario
        .income_sources
        .push(IncomeSource::interest(Some("federal_us")));
    fixture.rollout_count = 2;
    fixture.scenario.horizon_months = 13;
    fixture.scenario.jurisdictions = vec![JurisdictionIdentitySpec {
        jurisdiction_id: "federal_us".into(),
        level: JurisdictionLevel::Federal,
    }];
    fixture.scenario.initial_bonds = vec![
        BondSpec {
            bond_id: "treasury".into(),
            agent_id: "alice".into(),
            account_id: "checking".into(),
            issuer_jurisdiction_id: Some("federal_us".into()),
            face_value: Money(100_000_000),
            purchase_price: Money(100_000_000),
            annual_coupon_rate_ppb: 50_000_000,
            coupon_period_months: 6,
            inflation_indexed: false,
            purchase_month_index: -1,
            maturity_month_index: 11,
        },
        BondSpec {
            bond_id: "tips".into(),
            agent_id: "alice".into(),
            account_id: "checking".into(),
            issuer_jurisdiction_id: Some("federal_us".into()),
            face_value: Money(100_000_000),
            purchase_price: Money(100_000_000),
            annual_coupon_rate_ppb: 40_000_000,
            coupon_period_months: 6,
            inflation_indexed: true,
            purchase_month_index: -1,
            maturity_month_index: 11,
        },
        BondSpec {
            bond_id: "expired".into(),
            agent_id: "alice".into(),
            account_id: "checking".into(),
            issuer_jurisdiction_id: Some("federal_us".into()),
            face_value: Money(100_000_000),
            purchase_price: Money(100_000_000),
            annual_coupon_rate_ppb: 50_000_000,
            coupon_period_months: 6,
            inflation_indexed: false,
            purchase_month_index: -13,
            maturity_month_index: -1,
        },
    ];
    fixture.series = vec![SeriesSpec {
        series_id: "inflation".into(),
        snapshots: 14,
        values: [
            vec![1_000_000_000; 6],
            vec![2_000_000_000; 8],
            vec![1_000_000_000; 6],
            vec![1_500_000_000; 8],
        ]
        .concat(),
    }];

    let output = simulate(&fixture).unwrap();
    let first = &output.rollouts[0];
    let first_by_bond_month: BTreeMap<_, _> = first
        .bond_cashflows
        .iter()
        .map(|flow| ((flow.bond_id.as_str(), flow.month), flow))
        .collect();
    assert_eq!(
        first_by_bond_month[&("treasury", 5)].coupon,
        Money(2_500_000)
    );
    assert_eq!(
        first_by_bond_month[&("treasury", 11)].redemption,
        Money(100_000_000)
    );
    assert_eq!(first_by_bond_month[&("tips", 5)].coupon, Money(2_000_000));
    assert_eq!(
        first_by_bond_month[&("tips", 6)].accretion,
        Money(100_000_000)
    );
    assert_eq!(first_by_bond_month[&("tips", 11)].coupon, Money(4_000_000));
    assert_eq!(
        first_by_bond_month[&("tips", 11)].redemption,
        Money(200_000_000)
    );
    assert_eq!(first.months[5].bonds[1].principal, Money(100_000_000));
    assert_eq!(first.months[6].bonds[1].principal, Money(200_000_000));
    assert!(!first.months[12].bonds[1].active);
    assert_eq!(first.months[12].bonds[1].principal, Money(0));
    assert!(first.bond_cashflows.iter().all(|flow| flow.month <= 11));
    assert!(
        first
            .bond_cashflows
            .iter()
            .all(|flow| flow.bond_id != "expired")
    );

    let second = &output.rollouts[1];
    let second_tips = second
        .bond_cashflows
        .iter()
        .filter(|flow| flow.bond_id == "tips")
        .collect::<Vec<_>>();
    assert_eq!(second_tips[1].accretion, Money(50_000_000));
    assert_eq!(second_tips.last().unwrap().redemption, Money(150_000_000));
    assert!(
        output
            .rollouts
            .iter()
            .all(|rollout| rollout.journal.iter().all(|entry| {
                entry
                    .postings
                    .iter()
                    .map(|posting| i128::from(posting.amount.0))
                    .sum::<i128>()
                    == 0
            }))
    );
}

#[test]
fn bond_validation_rejects_non_par_and_missing_index_paths() {
    let mut fixture = minimal_fixture();
    fixture.scenario.income_sources.extend([
        IncomeSource::interest(None),
        IncomeSource::interest(Some("federal_us")),
    ]);
    fixture.scenario.initial_bonds = vec![BondSpec {
        bond_id: "bad".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        issuer_jurisdiction_id: None,
        face_value: Money(100),
        purchase_price: Money(99),
        annual_coupon_rate_ppb: 50_000_000,
        coupon_period_months: 6,
        inflation_indexed: false,
        purchase_month_index: -6,
        maturity_month_index: 6,
    }];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidBondTerms { .. })
    ));

    fixture.scenario.initial_bonds[0].purchase_price = Money(100);
    fixture.scenario.initial_bonds[0].inflation_indexed = true;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::MissingBondInflationSeries { .. })
    ));

    fixture.scenario.initial_bonds[0].inflation_indexed = false;
    fixture.scenario.initial_bonds[0].issuer_jurisdiction_id = Some("federal_us".into());
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnknownBondIssuer { .. })
    ));
}

#[test]
fn nominal_bond_coupon_rounds_the_full_rational_once() {
    let mut bond = BondSpec {
        bond_id: "rounding".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        issuer_jurisdiction_id: None,
        face_value: Money(600),
        purchase_price: Money(600),
        annual_coupon_rate_ppb: 10_000_000,
        coupon_period_months: 1,
        inflation_indexed: false,
        purchase_month_index: 0,
        maturity_month_index: 12,
    };
    assert_eq!(bond_coupon(bond.face_value, &bond).unwrap(), Money(1));

    bond.face_value = Money(180);
    bond.purchase_price = Money(180);
    bond.annual_coupon_rate_ppb = 33_333_333;
    assert_eq!(bond_coupon(bond.face_value, &bond).unwrap(), Money(0));

    bond.face_value = Money(1_250_627);
    bond.purchase_price = Money(1_250_627);
    bond.annual_coupon_rate_ppb = 37_000_000;
    bond.coupon_period_months = 5;
    bond.maturity_month_index = 60;
    assert_eq!(bond_coupon(bond.face_value, &bond).unwrap(), Money(19_280));
}

#[test]
fn rejects_invalid_references_before_rollout_execution() {
    let mut fixture = minimal_fixture();
    fixture
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 0,
            cause_id: "unknown-source".into(),
            from: AccountRef::new("missing", "checking"),
            to: AccountRef::new("alice", "checking"),
            amount: Money(1).into(),
            income_category: None,
            deduction_category: None,
        });
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnknownAccountReference { .. })
    ));
}

#[test]
fn rejects_income_from_a_source_the_scenario_did_not_declare() {
    // The income ledger holds one row per taxpayer per declared source and accrues to
    // nothing else, so an undeclared source would drop the income silently.
    let mut fixture = minimal_fixture();
    fixture
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 0,
            cause_id: "muni-coupon".into(),
            from: AccountRef::new("alice", "checking"),
            to: AccountRef::new("alice", "brokerage"),
            amount: Money(1).into(),
            income_category: Some(IncomeSource::interest(Some("california"))),
            deduction_category: None,
        });
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UndeclaredIncomeSource { income_source }) if income_source == "interest:california"
    ));
}

#[test]
fn distribution_tax_character_requires_a_complete_known_issuer_split() {
    let mut fixture = minimal_fixture();
    fixture.scenario.income_sources.extend([
        IncomeSource::interest(None),
        IncomeSource::interest(Some("federal_us")),
    ]);
    fixture.scenario.initial_lots = vec![InitialLotSpec {
        lot_id: "bnd".into(),
        agent_id: "alice".into(),
        account_id: "brokerage".into(),
        asset_id: "bnd".into(),
        purchase_month: -1,
        quantity_scale: 1_000_000,
        units: Quantity(1_000_000),
        basis: Money(1),
    }];
    fixture.scenario.distributions = vec![DistributionSpec {
        agent_id: "alice".into(),
        holding_account_id: "brokerage".into(),
        asset_id: "bnd".into(),
        to_account_id: "checking".into(),
        tax_character: vec![DistributionTaxSliceSpec {
            fraction_ppb: 400_000_000,
            issuer_jurisdiction_id: None,
        }],
    }];
    fixture.series = vec![SeriesSpec {
        series_id: "security_distribution:bnd".into(),
        snapshots: 2,
        values: vec![1, 1],
    }];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidDistributionTaxCharacter { .. })
    ));

    fixture.scenario.distributions[0].tax_character = vec![DistributionTaxSliceSpec {
        fraction_ppb: WIRE_RATE_SCALE,
        issuer_jurisdiction_id: Some("federal_us".into()),
    }];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnknownDistributionIssuer { .. })
    ));
}

#[test]
fn rejects_invalid_property_contracts_before_rollout_execution() {
    let mut fixture = minimal_fixture();
    fixture.scenario.accounts.push(AccountSpec {
        account: AccountRef::new("seller", "checking"),
        opening_balance: Money(0),
    });
    fixture.scenario.scheduled_property_purchases = vec![ScheduledPropertyPurchaseSpec {
        month: 0,
        cause_id: "buy-home".into(),
        property_id: "home".into(),
        location_id: "missing".into(),
        buyer_agent_id: "alice".into(),
        buyer_account_id: "checking".into(),
        seller_agent_id: "seller".into(),
        seller_account_id: "checking".into(),
        purchase_price: Money(10),
        down_payment: Money(10),
        buyer_closing_cost: Money(0),
        rented_fraction_ppb: 0,
        land_value_fraction_ppb: 200_000_000,
        mortgage: None,
    }];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnknownLocation { .. })
    ));

    fixture.scenario.locations.push(LocationSpec {
        location_id: "missing".into(),
        display_name: "Known now".into(),
        jurisdiction_ids: vec![],
        annual_property_tax_rate_ppb: 0,
        annual_special_assessment: Money(0),
    });
    fixture.scenario.scheduled_property_purchases[0].down_payment = Money(9);
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidPropertyTerms { .. })
    ));
}

#[test]
fn rejects_mixed_quantity_scales_and_invalid_security_prices() {
    let mut fixture = minimal_fixture();
    fixture.scenario.initial_lots = vec![
        InitialLotSpec {
            lot_id: "a".into(),
            agent_id: "alice".into(),
            account_id: "brokerage".into(),
            asset_id: "vti".into(),
            purchase_month: -2,
            quantity_scale: 1_000_000,
            units: Quantity(1_000_000),
            basis: Money(1),
        },
        InitialLotSpec {
            lot_id: "b".into(),
            agent_id: "alice".into(),
            account_id: "brokerage".into(),
            asset_id: "vti".into(),
            purchase_month: -1,
            quantity_scale: 1_000,
            units: Quantity(1_000),
            basis: Money(1),
        },
    ];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::MixedQuantityScale { .. })
    ));

    let mut fixture = minimal_fixture();
    fixture.series.push(SeriesSpec {
        series_id: "security:vti".into(),
        snapshots: 2,
        values: vec![100, -1],
    });
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidSecurityPrice {
            index: 1,
            value: -1,
            ..
        })
    ));
}

#[test]
fn zero_distribution_is_valid_but_negative_distribution_and_zero_price_are_not() {
    let mut fixture = minimal_fixture();
    fixture.series.push(SeriesSpec {
        series_id: "security_distribution:example".into(),
        snapshots: 2,
        values: vec![0, 0],
    });
    assert!(simulate(&fixture).is_ok());
    fixture.series[0].values[1] = -1;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::NegativeSecurityDistribution {
            index: 1,
            value: -1,
            ..
        })
    ));
    fixture.series[0].series_id = "security:example".into();
    fixture.series[0].values = vec![100, 0];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidSecurityPrice {
            index: 1,
            value: 0,
            ..
        })
    ));
}

#[test]
fn transfer_and_fifo_sale_remain_balanced() {
    let alice_cash = AccountRef::new("alice", "checking");
    let bob_cash = AccountRef::new("bob", "checking");
    let fixture = ExecutionInput {
        schema_version: INPUT_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 2,
        scenario: ScenarioSpec {
            horizon_months: 2,
            accounts: vec![
                AccountSpec {
                    account: alice_cash.clone(),
                    opening_balance: Money(1_000),
                },
                AccountSpec {
                    account: bob_cash.clone(),
                    opening_balance: Money(2_000),
                },
            ],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![ScheduledTransferSpec {
                month: 0,
                cause_id: "gift".into(),
                from: bob_cash,
                to: alice_cash,
                amount: Money(500).into(),
                income_category: None,
                deduction_category: None,
            }],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![],
            recurring_obligations: vec![],
            initial_lots: vec![InitialLotSpec {
                lot_id: "lot-1".into(),
                agent_id: "alice".into(),
                account_id: "brokerage".into(),
                asset_id: "vti".into(),
                purchase_month: -12,
                quantity_scale: 1_000_000,
                units: Quantity(2_000_000),
                basis: Money(20_000),
            }],
            initial_bonds: vec![],
            scheduled_sales: vec![ScheduledSaleSpec {
                month: 1,
                cause_id: "sell-vti".into(),
                agent_id: "alice".into(),
                account_id: "brokerage".into(),
                asset_id: "vti".into(),
                units: Quantity(1_000_000),
                proceeds_account_id: "checking".into(),
            }],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
            income_sources: vec![IncomeSource::Ordinary],
        },
        series: vec![SeriesSpec {
            series_id: "security:vti".into(),
            snapshots: 3,
            values: vec![10_000, 15_000, 15_000, 10_000, 20_000, 20_000],
        }],
    };
    let output = simulate(&fixture).unwrap();
    let dense = simulate_dense(&fixture).unwrap();
    let summaries = simulate_summaries(&fixture).unwrap();
    assert_eq!(output.rollouts.len(), 2);
    assert_eq!(dense.rollouts.len(), 2);
    assert_eq!(summaries.rollouts.len(), 2);
    assert_eq!(output.rollouts[0].dispositions[0].proceeds, Money(15_000));
    assert_eq!(output.rollouts[1].dispositions[0].proceeds, Money(20_000));
    for (forensic, dense) in output.rollouts.iter().zip(&dense.rollouts) {
        let mut expected = forensic.clone();
        expected.journal.clear();
        assert_eq!(&expected, dense);
        assert!(dense.journal.is_empty());
    }
    for (rollout, summary) in output.rollouts.iter().zip(&summaries.rollouts) {
        assert_eq!(summary.rollout_id, rollout.rollout_id);
        assert_eq!(
            summary.ending_balances,
            rollout.months.last().unwrap().balances
        );
        assert_eq!(
            summary.ending_properties,
            rollout.months.last().unwrap().properties
        );
        assert_eq!(summary.ending_bonds, rollout.months.last().unwrap().bonds);
        assert_eq!(
            summary.ending_mortgages,
            rollout.months.last().unwrap().mortgages
        );
        assert_eq!(summary.journal_entry_count, rollout.journal.len() as u64);
        assert_eq!(summary.disposition_count, rollout.dispositions.len() as u64);
        assert_eq!(summary.tax_accrual_count, rollout.tax_accruals.len() as u64);
        assert_eq!(
            summary.bond_cashflow_count,
            rollout.bond_cashflows.len() as u64
        );
        assert_eq!(
            summary.distribution_count,
            rollout.distributions.len() as u64
        );
        assert_eq!(
            summary.property_purchase_count,
            rollout.property_purchases.len() as u64
        );
        assert_eq!(
            summary.mortgage_payment_count,
            rollout.mortgage_payments.len() as u64
        );
        assert_eq!(summary.failed_month, rollout.failed_month);
    }
    for rollout in output.rollouts {
        for entry in rollout.journal {
            assert_eq!(
                entry
                    .postings
                    .iter()
                    .map(|posting| i128::from(posting.amount.0))
                    .sum::<i128>(),
                0
            );
        }
    }
}

#[test]
fn financed_property_purchase_and_first_monthly_carry_match_contract() {
    let mut fixture = minimal_fixture();
    fixture.scenario.horizon_months = 2;
    fixture.scenario.accounts = vec![
        AccountSpec {
            account: AccountRef::new("alice", "checking"),
            opening_balance: Money(12_000_000),
        },
        AccountSpec {
            account: AccountRef::new("seller", "checking"),
            opening_balance: Money(0),
        },
        AccountSpec {
            account: AccountRef::new("bank", "checking"),
            opening_balance: Money(0),
        },
        AccountSpec {
            account: AccountRef::new("county", "checking"),
            opening_balance: Money(0),
        },
    ];
    fixture.scenario.locations = vec![LocationSpec {
        location_id: "sf".into(),
        display_name: "San Francisco".into(),
        jurisdiction_ids: vec![],
        annual_property_tax_rate_ppb: 11_800_000,
        annual_special_assessment: Money(0),
    }];
    fixture.scenario.scheduled_property_purchases = vec![ScheduledPropertyPurchaseSpec {
        month: 0,
        cause_id: "alice-buys-home".into(),
        property_id: "home".into(),
        location_id: "sf".into(),
        buyer_agent_id: "alice".into(),
        buyer_account_id: "checking".into(),
        seller_agent_id: "seller".into(),
        seller_account_id: "checking".into(),
        purchase_price: Money(50_000_000),
        down_payment: Money(10_000_000),
        buyer_closing_cost: Money(1_000_000),
        rented_fraction_ppb: 0,
        land_value_fraction_ppb: 200_000_000,
        mortgage: Some(MortgageFinancingSpec {
            liability_id: "home-mortgage".into(),
            lender_agent_id: "bank".into(),
            lender_account_id: "checking".into(),
            principal: Money(40_000_000),
            annual_interest_rate_ppb: 60_000_000,
            term_months: 360,
        }),
    }];
    fixture.scenario.property_tax_policies = vec![PropertyTaxPolicySpec {
        property_id: "home".into(),
        owner_agent_id: "alice".into(),
        from_account_id: "checking".into(),
        tax_authority_agent_id: "county".into(),
        tax_authority_account_id: "checking".into(),
        annual_tax_rate_ppb: Some(12_000_000),
        start_month: 0,
        end_month: None,
    }];

    let rollout = simulate(&fixture).unwrap().rollouts.remove(0);
    let month_zero = &rollout.months[1];
    assert_eq!(month_zero.properties[0].adjusted_basis, Money(51_000_000));
    assert_eq!(
        month_zero.properties[0].contribution_used,
        Money(11_000_000)
    );
    assert_eq!(month_zero.properties[0].equity_ledger, Money(10_000_000));
    assert_eq!(month_zero.mortgages[0].monthly_payment, Money(239_820));
    assert_eq!(month_zero.mortgages[0].principal, Money(40_000_000));

    let final_month = &rollout.months[2];
    assert_eq!(final_month.mortgages[0].interest_paid_ytd, Money(200_000));
    assert_eq!(final_month.mortgages[0].principal, Money(39_960_180));
    let cash: BTreeMap<_, _> = final_month
        .balances
        .iter()
        .filter(|balance| balance.account.account_id == "checking")
        .map(|balance| (balance.account.agent_id.as_str(), balance.balance))
        .collect();
    assert_eq!(cash["alice"], Money(710_180));
    assert_eq!(cash["seller"], Money(11_000_000));
    assert_eq!(cash["bank"], Money(239_820));
    assert_eq!(cash["county"], Money(50_000));
    assert_eq!(rollout.property_purchases.len(), 1);
    assert_eq!(rollout.mortgage_originations.len(), 1);
    assert_eq!(rollout.mortgage_payments.len(), 1);
    assert!(rollout.journal.iter().all(|entry| {
        entry
            .postings
            .iter()
            .map(|posting| i128::from(posting.amount.0))
            .sum::<i128>()
            == 0
    }));
}

#[test]
fn oversell_is_rejected_before_any_disposition() {
    let alice_cash = AccountRef::new("alice", "checking");
    let fixture = ExecutionInput {
        schema_version: INPUT_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 1,
        scenario: ScenarioSpec {
            horizon_months: 1,
            accounts: vec![AccountSpec {
                account: alice_cash,
                opening_balance: Money(0),
            }],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![],
            recurring_obligations: vec![],
            initial_lots: vec![InitialLotSpec {
                lot_id: "lot-1".into(),
                agent_id: "alice".into(),
                account_id: "brokerage".into(),
                asset_id: "vti".into(),
                purchase_month: -1,
                quantity_scale: 1_000_000,
                units: Quantity(1_000_000),
                basis: Money(10_000),
            }],
            initial_bonds: vec![],
            scheduled_sales: vec![ScheduledSaleSpec {
                month: 0,
                cause_id: "oversell".into(),
                agent_id: "alice".into(),
                account_id: "brokerage".into(),
                asset_id: "vti".into(),
                units: Quantity(1_000_001),
                proceeds_account_id: "checking".into(),
            }],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
            income_sources: vec![IncomeSource::Ordinary],
        },
        series: vec![SeriesSpec {
            series_id: "security:vti".into(),
            snapshots: 2,
            values: vec![10_000, 10_000],
        }],
    };
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InsufficientLotUnits {
            requested: 1_000_001,
            available: 1_000_000,
            ..
        })
    ));
}

#[test]
fn failure_stops_future_actions_and_zeroes_value_state() {
    let alice_cash = AccountRef::new("alice", "checking");
    let bob_cash = AccountRef::new("bob", "checking");
    let fixture = ExecutionInput {
        schema_version: INPUT_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 1,
        scenario: ScenarioSpec {
            horizon_months: 2,
            accounts: vec![
                AccountSpec {
                    account: alice_cash.clone(),
                    opening_balance: Money(100),
                },
                AccountSpec {
                    account: bob_cash.clone(),
                    opening_balance: Money(0),
                },
            ],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![ScheduledTransferSpec {
                month: 1,
                cause_id: "must-not-run".into(),
                from: alice_cash.clone(),
                to: bob_cash.clone(),
                amount: Money(1).into(),
                income_category: None,
                deduction_category: None,
            }],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![ObligationSpec {
                month: 0,
                obligation_id: "too-large".into(),
                obligation_type: "cash_spend".into(),
                from: alice_cash,
                to: bob_cash,
                amount_due: Money(101).into(),
                property_id: None,
                deduction_category: None,
                deductible_fraction_ppb: WIRE_RATE_SCALE,
            }],
            recurring_obligations: vec![],
            initial_lots: vec![],
            initial_bonds: vec![],
            scheduled_sales: vec![],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
            income_sources: vec![IncomeSource::Ordinary],
        },
        series: vec![],
    };
    let rollout = simulate(&fixture).unwrap().rollouts.remove(0);
    assert_eq!(rollout.failed_month, Some(0));
    assert!(!rollout.months[0].failed);
    assert!(rollout.months[1].failed);
    assert!(rollout.months[2].failed);
    assert!(
        rollout.months[1..]
            .iter()
            .flat_map(|month| &month.balances)
            .all(|balance| balance.balance == Money(0))
    );
    assert!(
        rollout
            .journal
            .iter()
            .all(|entry| entry.cause_id != "must-not-run")
    );
}

#[test]
fn same_source_recurring_obligations_settle_all_or_none() {
    let alice_cash = AccountRef::new("alice", "checking");
    let landlord_cash = AccountRef::new("landlord", "checking");
    let utility_cash = AccountRef::new("utility", "checking");
    let fixture = ExecutionInput {
        schema_version: INPUT_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 1,
        scenario: ScenarioSpec {
            horizon_months: 3,
            accounts: vec![
                AccountSpec {
                    account: alice_cash.clone(),
                    opening_balance: Money(100_000),
                },
                AccountSpec {
                    account: landlord_cash.clone(),
                    opening_balance: Money(0),
                },
                AccountSpec {
                    account: utility_cash.clone(),
                    opening_balance: Money(0),
                },
            ],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![],
            recurring_obligations: vec![
                RecurringObligationSpec {
                    start_month: 0,
                    end_month: Some(2),
                    obligation_id: "rent".into(),
                    obligation_type: "cash_spend".into(),
                    from: alice_cash.clone(),
                    to: landlord_cash,
                    amount_due: Money(60_000).into(),
                    property_id: None,
                    deduction_category: None,
                    deductible_fraction_ppb: WIRE_RATE_SCALE,
                },
                RecurringObligationSpec {
                    start_month: 1,
                    end_month: Some(2),
                    obligation_id: "utility".into(),
                    obligation_type: "cash_spend".into(),
                    from: alice_cash,
                    to: utility_cash,
                    amount_due: Money(1).into(),
                    property_id: None,
                    deduction_category: None,
                    deductible_fraction_ppb: WIRE_RATE_SCALE,
                },
            ],
            initial_lots: vec![],
            initial_bonds: vec![],
            scheduled_sales: vec![],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
            income_sources: vec![IncomeSource::Ordinary],
        },
        series: vec![],
    };
    let rollout = simulate(&fixture).unwrap().rollouts.remove(0);
    assert_eq!(rollout.failed_month, Some(1));
    assert_eq!(
        rollout
            .journal
            .iter()
            .filter(|entry| entry.cause_id.starts_with("rent_m"))
            .map(|entry| entry.cause_id.as_str())
            .collect::<Vec<_>>(),
        vec!["rent_m0"]
    );
    assert!(
        rollout
            .journal
            .iter()
            .all(|entry| entry.cause_id != "utility_m1")
    );
    assert_eq!(rollout.obligations.len(), 3);
    assert_eq!(rollout.obligations[0].amount_paid, Money(60_000));
    assert_eq!(rollout.obligations[0].shortfall, Money(0));
    assert_eq!(rollout.obligations[1].obligation_id, "rent_m1");
    assert_eq!(rollout.obligations[1].amount_paid, Money(0));
    assert_eq!(rollout.obligations[1].shortfall, Money(60_000));
    assert!(rollout.obligations[1].failure_active);
    assert_eq!(rollout.obligations[2].obligation_id, "utility_m1");
    assert_eq!(rollout.obligations[2].amount_paid, Money(0));
    assert_eq!(rollout.obligations[2].shortfall, Money(1));
    assert!(rollout.obligations[2].failure_active);
}

fn buying_fixture(horizon_months: u32) -> ExecutionInput {
    let mut fixture = minimal_fixture();
    fixture.scenario.horizon_months = horizon_months;
    fixture.scenario.accounts[0].opening_balance = Money(20_000);
    fixture.scenario.target_allocation_policies = vec![TargetAllocationPolicySpec {
        agent_id: "alice".into(),
        account_id: "checking".into(),
        source_account_ids: vec!["brokerage".into()],
        sleeves: vec![SleeveTargetSpec {
            asset_id: "stock".into(),
            weight: 1,
            quantity_scale: 1_000_000,
        }],
        cash_floor: Money(0).into(),
        cash_ceiling: Money(0).into(),
        cause_id_prefix: "test-buy".into(),
        allow_purchases: true,
        rebalance_tolerance_ppb: None,
    }];
    fixture.series = vec![SeriesSpec {
        series_id: "security:stock".into(),
        snapshots: horizon_months + 1,
        values: vec![10_000; horizon_months as usize + 1],
    }];
    fixture
}

#[test]
fn empty_buyable_pool_distributes_zero_until_its_first_purchase_settles() {
    let mut fixture = buying_fixture(2);
    fixture
        .scenario
        .income_sources
        .push(IncomeSource::interest(None));
    fixture.scenario.distributions = vec![DistributionSpec {
        agent_id: "alice".into(),
        holding_account_id: "brokerage".into(),
        asset_id: "stock".into(),
        to_account_id: "checking".into(),
        tax_character: vec![DistributionTaxSliceSpec {
            fraction_ppb: WIRE_RATE_SCALE,
            issuer_jurisdiction_id: None,
        }],
    }];
    fixture.series.push(SeriesSpec {
        series_id: "security_distribution:stock".into(),
        snapshots: 3,
        values: vec![100 * WIRE_RATE_SCALE; 3],
    });
    let rollout = simulate(&fixture).unwrap().rollouts.remove(0);
    assert_eq!(rollout.failed_month, None);
    assert!(rollout.months[0].lots.is_empty());
    assert_eq!(rollout.months[1].lots.len(), 1);
    assert_eq!(
        rollout.months[1].lots[0].units_remaining,
        Quantity(2_000_000)
    );
    assert_eq!(rollout.months[1].lots[0].basis_remaining, Money(20_000));
    assert_eq!(
        rollout
            .distributions
            .iter()
            .map(|row| (row.month, row.units, row.amount))
            .collect::<Vec<_>>(),
        vec![
            (0, Quantity(0), Money(0)),
            (1, Quantity(2_000_000), Money(200))
        ]
    );
}

#[test]
fn fifo_uses_purchase_month_across_policies_and_preserves_each_lots_tax_basis() {
    let mut fixture = buying_fixture(13);
    fixture.scenario.accounts[0].opening_balance = Money(0);
    fixture.scenario.accounts.extend([
        AccountSpec {
            account: AccountRef::new("alice", "early-cash"),
            opening_balance: Money(20_000),
        },
        AccountSpec {
            account: AccountRef::new("alice", "proceeds"),
            opening_balance: Money(0),
        },
        AccountSpec {
            account: AccountRef::new("world", "checking"),
            opening_balance: Money(30_000),
        },
    ]);
    let mut early_policy = fixture.scenario.target_allocation_policies[0].clone();
    early_policy.account_id = "early-cash".into();
    early_policy.cause_id_prefix = "early".into();
    fixture
        .scenario
        .target_allocation_policies
        .push(early_policy);
    // Policy 1 buys first; policy 0 buys a month later into the same brokerage pool.
    fixture
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 1,
            cause_id: "later-deposit".into(),
            from: AccountRef::new("world", "checking"),
            to: AccountRef::new("alice", "checking"),
            amount: Money(30_000).into(),
            income_category: None,
            deduction_category: None,
        });
    fixture.series[0].values = vec![20_000; 14];
    fixture.series[0].values[0] = 10_000;
    fixture.series[0].values[1] = 15_000;
    fixture.scenario.scheduled_sales.push(ScheduledSaleSpec {
        month: 12,
        cause_id: "fifo-sale".into(),
        agent_id: "alice".into(),
        account_id: "brokerage".into(),
        asset_id: "stock".into(),
        units: Quantity(3_000_000),
        proceeds_account_id: "proceeds".into(),
    });
    // Synthetic zero-rate rules expose holding-period classification without payment effects.
    fixture.scenario.tax_profiles.push(TaxProfileSpec {
        agent_id: "alice".into(),
        tax_authority_agent_id: "world".into(),
        payment_account_id: "proceeds".into(),
        tax_authority_account_id: "checking".into(),
        prior_year_tax: Money(0),
        section_121_exclusion: Money(0),
        jurisdictions: vec![TaxRules {
            jurisdiction_id: "test".into(),
            exempt_interest_from_levels: vec![],
            exempts_own_issue: false,
            ordinary_brackets: vec![TaxBracket {
                upper: None,
                rate_ppb: 0,
            }],
            long_term_capital_gain_brackets: vec![TaxBracket {
                upper: None,
                rate_ppb: 0,
            }],
            standard_deduction: Money(0),
            max_capital_loss_ordinary_offset: Money(0),
            section_1250_rate_ppb: 0,
        }],
    });
    let rollout = simulate(&fixture).unwrap().rollouts.remove(0);
    assert_eq!(rollout.failed_month, None);
    assert_eq!(
        rollout
            .dispositions
            .iter()
            .map(|row| (
                row.purchase_month,
                row.units,
                row.basis,
                row.proceeds,
                row.realized_gain
            ))
            .collect::<Vec<_>>(),
        vec![
            (
                0,
                Quantity(2_000_000),
                Money(20_000),
                Money(40_000),
                Money(20_000)
            ),
            (
                1,
                Quantity(1_000_000),
                Money(15_000),
                Money(20_000),
                Money(5_000)
            ),
        ]
    );
    let gains = &rollout.months.last().unwrap().capital_gains[0];
    assert_eq!(gains.long_term_gain, Money(20_000));
    assert_eq!(gains.short_term_gain, Money(5_000));
}

#[test]
fn future_purchase_lot_ids_are_reserved_before_execution() {
    let mut fixture = buying_fixture(1);
    // Reservation covers future purchase numbers, not only those reachable in this horizon.
    let reserved = "test-buy_buy_p0_s0_1000000";
    fixture.scenario.initial_lots.push(InitialLotSpec {
        lot_id: reserved.into(),
        agent_id: "alice".into(),
        account_id: "brokerage".into(),
        asset_id: "stock".into(),
        purchase_month: -1,
        quantity_scale: 1_000_000,
        units: Quantity(1_000_000),
        basis: Money(10_000),
    });
    assert!(matches!(
        ValidatedInput::new(&fixture),
        Err(SimulationError::DuplicateLot { lot_id }) if lot_id == reserved
    ));
    fixture.scenario.target_allocation_policies[0].allow_purchases = false;
    assert!(ValidatedInput::new(&fixture).is_ok());
    fixture.scenario.target_allocation_policies[0].allow_purchases = true;
    fixture.scenario.initial_lots[0].lot_id.push('x');
    assert!(ValidatedInput::new(&fixture).is_ok());
}
