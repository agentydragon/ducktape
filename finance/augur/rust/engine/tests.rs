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
    let compact = spending::simulate_summary(&fixture, &spending, |_| {
        |observation| Ok(Money(1_000).scaled_by(observation.price_level, "fixed real spending")?)
    })
    .unwrap();
    assert_eq!(compact.product_metrics, scheduled_metrics);
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
    let summary = spending::simulate_summary(&fixture, &spending, make_policy).unwrap();
    assert_eq!(
        summary.consumption_requested[0],
        vec![Money(1), Money(2), Money(3)]
    );
    assert_eq!(
        summary.consumption_paid[0],
        vec![Money(1), Money(2), Money(0)]
    );
    assert_eq!(summary.consumption_requested[1], vec![Money(0); 13]);
    assert_eq!(summary.consumption_paid[1], vec![Money(0); 13]);
    let compact = &summary.product_metrics;
    assert_eq!(compact.failed_month, vec![2, -1]);
    assert_eq!(compact.rollout_count, fixture.rollout_count);
    assert_eq!(compact.snapshot_count, fixture.scenario.horizon_months + 1);
    for rollout in &forensic.rollouts {
        let path = rollout.rollout_id as usize;
        let observed_months = rollout
            .failed_month
            .map_or(fixture.scenario.horizon_months, |month| month + 1);
        assert_eq!(
            summary.consumption_requested[path].len(),
            observed_months as usize
        );
        assert_eq!(
            summary.consumption_paid[path].len(),
            observed_months as usize
        );
        for month in 0..observed_months {
            let receipt = rollout.obligations.iter().find(|row| row.month == month);
            assert_eq!(
                summary.consumption_requested[path][month as usize],
                receipt.map_or(Money(0), |row| row.amount_due)
            );
            assert_eq!(
                summary.consumption_paid[path][month as usize],
                receipt.map_or(Money(0), |row| row.amount_paid)
            );
        }
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
        spending::simulate_summary(&fixture, &spending, |_| |_| Ok(Money(0)))
            .unwrap()
            .product_metrics,
        simulate_product_metrics(&fixture, &spending.from.agent_id).unwrap()
    );
    assert!(matches!(
        spending::simulate_summary(&fixture, &spending, |_| |_| Ok(Money(-1))),
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
        spending::simulate_summary(
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

#[test]
fn compact_consumption_uses_its_receipt_when_another_funding_group_fails() {
    let (mut input, spending) = spending_fixture();
    input.scenario.accounts[0].opening_balance = Money(20);
    input.scenario.accounts.push(AccountSpec {
        account: AccountRef::new("other", "checking"),
        opening_balance: Money(0),
    });
    for (from, amount, id) in [
        (spending.from.clone(), 7, "consumption"),
        (AccountRef::new("other", "checking"), 1, "other-demand"),
    ] {
        input.scenario.obligations.push(ObligationSpec {
            month: 0,
            obligation_id: id.into(),
            obligation_type: "cash_spend".into(),
            from,
            to: spending.to.clone(),
            amount_due: Money(amount).into(),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: WIRE_RATE_SCALE,
        });
    }
    let make_policy = |_| {
        |observation: spending::Observation| {
            assert_eq!(
                observation.month, 0,
                "no callback after the other group's failure"
            );
            Ok(Money(3))
        }
    };
    let summary = spending::simulate_summary(&input, &spending, make_policy).unwrap();
    let forensic = spending::simulate(&input, &spending, make_policy).unwrap();
    assert_eq!(summary.product_metrics.failed_month, vec![0, 0]);
    assert_eq!(summary.consumption_requested, vec![vec![Money(3)]; 2]);
    assert_eq!(summary.consumption_paid, vec![vec![Money(3)]; 2]);
    assert_eq!(summary.component.cause_id, spending.cause_id);
    for rollout in forensic.rollouts {
        // The configured claim deliberately shares both category and cause ID with
        // the callback; the compact result must identify the actual demand itself.
        assert_eq!(
            rollout.obligations[0].cause_id,
            rollout.obligations[1].cause_id
        );
        assert_eq!(rollout.obligations[0].amount_paid, Money(3));
        assert_eq!(rollout.obligations[1].amount_paid, Money(7));
        assert_eq!(rollout.obligations[2].amount_paid, Money(0));
    }
}

#[test]
fn spending_selected_trace_preserves_original_path_and_factory_identity() {
    let (input, spending) = spending_fixture();
    let make_policy = |rollout| {
        let mut requests = 0;
        move |observation: spending::Observation| {
            assert_eq!(requests, observation.month);
            requests += 1;
            Money(i64::from((rollout + 1) * requests))
                .scaled_by(observation.price_level, "test request")
                .map_err(Into::into)
        }
    };
    let population = spending::simulate(&input, &spending, make_policy).unwrap();
    for rollout in population.rollouts {
        let selected =
            spending::trace_rollout(&input, &spending, rollout.rollout_id, make_policy).unwrap();
        assert_eq!(
            serde_json::to_value(selected).unwrap(),
            serde_json::to_value(rollout).unwrap()
        );
    }
    assert!(matches!(
        spending::trace_rollout(
            &input,
            &spending,
            input.rollout_count,
            |_| -> fn(spending::Observation) -> Result<Money, SimulationError> {
                panic!("invalid selection must not construct a policy")
            }
        ),
        Err(SimulationError::UnknownRollout { .. })
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

/// $100 cash and two $500 sleeves, each with $250 basis, priced at $10/share.
fn allocation_fixture(horizon_months: u32) -> ExecutionInput {
    let (mut input, _) = policy_timing_fixture(horizon_months);
    input.scenario.initial_lots[0].units = Quantity(50_000_000);
    input.scenario.initial_lots[0].basis = Money(25_000);
    let mut second = input.scenario.initial_lots[0].clone();
    second.lot_id = "test-second-lot".into();
    second.asset_id = "second".into();
    input.scenario.initial_lots.push(second);
    input.series.push(SeriesSpec {
        series_id: "security:second".into(),
        snapshots: horizon_months + 1,
        values: vec![1_000; horizon_months as usize + 1],
    });
    input.scenario.target_allocation_policies[0]
        .sleeves
        .push(SleeveTargetSpec {
            asset_id: "second".into(),
            weight: 1,
            quantity_scale: 1_000_000,
        });
    input
}

fn allocation_tax_and_consumption_fixture() -> ExecutionInput {
    let mut input = allocation_fixture(13);
    input
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 12,
            cause_id: "test-contribution".into(),
            from: AccountRef::new("world", "checking"),
            to: AccountRef::new("alice", "checking"),
            amount: Money(10_000).into(),
            income_category: None,
            deduction_category: None,
        });
    for (month, amount) in [(0, 50_000), (12, 5_000)] {
        input.scenario.obligations.push(ObligationSpec {
            month,
            obligation_id: "test-consumption".into(),
            obligation_type: "cash_spend".into(),
            from: AccountRef::new("alice", "checking"),
            to: AccountRef::new("world", "checking"),
            amount_due: Money(amount).into(),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: WIRE_RATE_SCALE,
        });
    }
    input.scenario.tax_profiles.push(TaxProfileSpec {
        agent_id: "alice".into(),
        tax_authority_agent_id: "world".into(),
        payment_account_id: "checking".into(),
        tax_authority_account_id: "checking".into(),
        prior_year_tax: Money(0),
        section_121_exclusion: Money(0),
        jurisdictions: vec![TaxRules {
            jurisdiction_id: "test-allocation".into(),
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
    input
}

#[test]
fn constant_allocation_function_has_static_receipt_lot_and_tax_parity() {
    for purchases in [false, true] {
        let mut input = allocation_tax_and_consumption_fixture();
        input.scenario.target_allocation_policies[0].allow_purchases = purchases;
        let before = input.clone();
        let output =
            allocation::simulate(&input, &AccountRef::new("alice", "checking"), &[0], |_| {
                |observation| {
                    assert_eq!(
                        observation.cash,
                        Money(if observation.month == 0 { 10_000 } else { 0 })
                    );
                    assert_eq!(
                        observation.sleeve_values,
                        vec![
                            Money(if observation.month == 0 {
                                50_000
                            } else {
                                30_000
                            });
                            2
                        ]
                    );
                    Ok(vec![1, 1])
                }
            })
            .unwrap();
        assert_eq!(input, before);
        assert_eq!(output, simulate(&input).unwrap());
        assert_eq!(output.rollouts[0].tax_payments[0].amount_paid, Money(2_000));
        assert_eq!(
            output.rollouts[0]
                .obligations
                .iter()
                .filter(|row| row.obligation_type == "cash_spend")
                .map(|row| row.amount_paid.0)
                .sum::<i64>(),
            55_000
        );
    }
}

#[test]
fn allocation_review_changes_a_quiet_band_target_not_prior_months() {
    let mut input = allocation_fixture(3);
    let policy = &mut input.scenario.target_allocation_policies[0];
    policy.allow_purchases = true;
    policy.cash_ceiling = Money(10_000).into();
    policy.rebalance_tolerance_ppb = Some(0);
    let output = allocation::simulate(&input, &AccountRef::new("alice", "checking"), &[0], |_| {
        |observation| {
            Ok(if observation.month < 2 {
                vec![1, 1]
            } else {
                vec![3, 2]
            })
        }
    })
    .unwrap();
    let rollout = &output.rollouts[0];
    assert_eq!(rollout.dispositions.len(), 1);
    assert_eq!(rollout.dispositions[0].month, 2);
    assert_eq!(rollout.dispositions[0].asset_id, "security:second");
    assert_eq!(rollout.dispositions[0].proceeds, Money(10_000));
    let final_lots = &rollout.months.last().unwrap().lots;
    for (asset, units) in [
        ("security:stock", 60_000_000),
        ("security:second", 40_000_000),
    ] {
        assert_eq!(
            final_lots
                .iter()
                .filter(|lot| lot.asset_id == asset)
                .map(|lot| lot.units_remaining.0)
                .sum::<i64>(),
            units
        );
    }
    assert!(
        rollout.months[..=2]
            .iter()
            .all(|month| month.lots.len() == 2)
    );
}

#[test]
fn allocation_funding_and_tax_month_invests_only_surplus_toward_new_target() {
    let mut input = allocation_tax_and_consumption_fixture();
    input.scenario.target_allocation_policies[0].allow_purchases = true;
    input.scenario.target_allocation_policies[0].rebalance_tolerance_ppb = Some(0);
    let rollout = allocation::simulate(&input, &AccountRef::new("alice", "checking"), &[0], |_| {
        |observation| {
            if observation.month == 12 {
                assert_eq!(observation.cash, Money(0)); // The contribution arrives after review.
                assert_eq!(observation.sleeve_values, vec![Money(30_000); 2]);
            }
            Ok(if observation.month < 12 {
                vec![1, 1]
            } else {
                vec![3, 2]
            })
        }
    })
    .unwrap()
    .rollouts
    .remove(0);
    assert_eq!(rollout.failed_month, None);
    assert!(rollout.dispositions.iter().all(|row| row.month == 0));
    assert_eq!(rollout.tax_payments[0].amount_paid, Money(2_000));
    let consumption = rollout
        .obligations
        .iter()
        .find(|row| row.month == 12 && row.obligation_type == "cash_spend")
        .unwrap();
    assert_eq!(consumption.amount_paid, Money(5_000));
    let lots = &rollout.months.last().unwrap().lots;
    let purchased: Vec<_> = lots.iter().filter(|lot| lot.purchase_month == 12).collect();
    assert_eq!(purchased.len(), 1);
    assert_eq!(purchased[0].asset_id, "security:stock");
    assert_eq!(purchased[0].basis_remaining, Money(3_000));
    // Cash-band investment suppresses a simultaneous full drift rebalance: $330/$300,
    // not the $378/$252 that an immediate 60/40 rebalance would produce.
    assert_eq!(
        lots.iter()
            .filter(|lot| lot.asset_id == "security:stock")
            .map(|lot| lot.units_remaining.0)
            .sum::<i64>(),
        33_000_000
    );
    assert_eq!(
        lots.iter()
            .filter(|lot| lot.asset_id == "security:second")
            .map(|lot| lot.units_remaining.0)
            .sum::<i64>(),
        30_000_000
    );
}

#[test]
fn allocation_functions_are_isolated_under_selection_reordering_and_replay() {
    let mut input = allocation_fixture(4);
    input.rollout_count = 3;
    for series in &mut input.series {
        series.values = series.values.repeat(3);
    }
    input.series[1].values = [vec![1_000; 5], vec![2_000; 5], vec![500; 5]].concat();
    input.scenario.accounts.push(AccountSpec {
        account: AccountRef::new("alice", "outside-pool"),
        opening_balance: Money(1_000_000),
    });
    let mut outside = input.scenario.initial_lots[0].clone();
    outside.lot_id = "test-outside-pool".into();
    outside.account_id = "outside-pool".into();
    input.scenario.initial_lots.push(outside);
    input.scenario.target_allocation_policies[0].allow_purchases = true;
    input.scenario.target_allocation_policies[0].rebalance_tolerance_ppb = Some(0);
    let make_policy = |rollout_id: u32| {
        let mut calls = 0;
        move |observation: allocation::Observation| {
            assert_eq!(observation.month, calls);
            if calls == 0 {
                assert_eq!(observation.cash, Money(10_000));
                assert_eq!(
                    observation.sleeve_values[0],
                    Money([50_000, 100_000, 25_000][rollout_id as usize])
                );
            }
            calls += 1;
            Ok(vec![i64::from(calls), 2])
        }
    };
    let account = AccountRef::new("alice", "checking");
    let population = allocation::simulate(&input, &account, &[0, 1, 2], make_policy).unwrap();
    for ids in [&[2, 0][..], &[1][..], &[2][..]] {
        let selected = allocation::simulate(&input, &account, ids, make_policy).unwrap();
        for (&id, rollout) in ids.iter().zip(&selected.rollouts) {
            assert_eq!(rollout, &population.rollouts[id as usize]);
        }
    }
}

#[test]
fn allocation_functions_stop_after_failure_and_reject_invalid_decisions() {
    let mut input = allocation_fixture(4);
    input
        .scenario
        .recurring_obligations
        .push(RecurringObligationSpec {
            start_month: 0,
            end_month: None,
            obligation_id: "test-consumption".into(),
            obligation_type: "cash_spend".into(),
            from: AccountRef::new("alice", "checking"),
            to: AccountRef::new("world", "checking"),
            amount_due: Money(50_000).into(),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: WIRE_RATE_SCALE,
        });
    let account = AccountRef::new("alice", "checking");
    let output = allocation::simulate(&input, &account, &[0], |_| {
        |observation| {
            assert!(observation.month <= 2);
            Ok(vec![1, 1])
        }
    })
    .unwrap();
    assert_eq!(output.rollouts[0].failed_month, Some(2));
    for weights in [vec![], vec![1], vec![1, 0], vec![-1, 2]] {
        assert!(matches!(
            allocation::simulate(&input, &account, &[0], |_| |_| Ok(weights.clone())),
            Err(SimulationError::Allocation(_))
        ));
    }
    for ids in [&[][..], &[1][..], &[0, 0][..]] {
        assert!(matches!(
            allocation::simulate(
                &input,
                &account,
                ids,
                |_| -> fn(allocation::Observation) -> Result<Vec<i64>, SimulationError> {
                    panic!("invalid selection")
                }
            ),
            Err(SimulationError::InvalidRolloutSelection)
        ));
    }
    assert!(matches!(
        allocation::simulate(
            &input,
            &AccountRef::new("world", "checking"),
            &[0],
            |_| -> fn(allocation::Observation) -> Result<Vec<i64>, SimulationError> {
                panic!("unbound account")
            }
        ),
        Err(SimulationError::MissingTargetAllocationPolicy { .. })
    ));
}

#[test]
fn allocation_missing_prices_are_not_zero_valued_holdings() {
    let mut input = allocation_fixture(1);
    input
        .series
        .retain(|series| series.series_id != "security:second");
    assert!(matches!(
        simulate(&input),
        Err(SimulationError::MissingSeries { .. })
    ));
    assert!(matches!(
        allocation::simulate(
            &input,
            &AccountRef::new("alice", "checking"),
            &[0],
            |_| -> fn(allocation::Observation) -> Result<Vec<i64>, SimulationError> {
                panic!("missing price")
            }
        ),
        Err(SimulationError::MissingSeries { .. })
    ));
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
        let make_policy = |_| {
            move |observation: spending::Observation| {
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
        };
        let summary = spending::simulate_summary(&input, &spending, make_policy).unwrap();
        let rollout = spending::simulate(&input, &spending, make_policy)
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
        assert_eq!(
            summary.consumption_requested[0],
            vec![consumption.amount_due]
        );
        assert_eq!(summary.consumption_paid[0], vec![consumption.amount_paid]);
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
        let make_policy = |_| {
            |observation: spending::Observation| {
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
        };
        let summary = spending::simulate_summary(&input, &spending, make_policy).unwrap();
        let rollout = spending::simulate(&input, &spending, make_policy)
            .unwrap()
            .rollouts
            .remove(0);
        assert_eq!(rollout.failed_month, None);
        for month in 0..13 {
            let receipt = rollout.obligations.iter().find(|row| row.month == month);
            assert_eq!(
                summary.consumption_requested[0][month as usize],
                receipt.map_or(Money(0), |row| row.amount_due)
            );
            assert_eq!(
                summary.consumption_paid[0][month as usize],
                receipt.map_or(Money(0), |row| row.amount_paid)
            );
        }
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

fn stopped_book_fixture(
    horizon: u32,
    future_multiplier: i64,
) -> (ExecutionInput, spending::Spending) {
    let (mut input, mut component) = policy_timing_fixture(horizon);
    input.scenario.accounts[0].opening_balance = Money(2_100);
    input.scenario.target_allocation_policies.clear();
    component.from = AccountRef::new("alice", "budget");
    input.scenario.accounts.push(AccountSpec {
        account: component.from.clone(),
        opening_balance: Money(500),
    });
    input.scenario.scheduled_sales.push(ScheduledSaleSpec {
        month: 0,
        cause_id: "gain-for-tax".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        asset_id: "stock".into(),
        units: Quantity(1_000_000),
        proceeds_account_id: "checking".into(),
    });
    input.scenario.locations.push(LocationSpec {
        location_id: "test-place".into(),
        display_name: "Test place".into(),
        jurisdiction_ids: vec![],
        annual_property_tax_rate_ppb: 0,
        annual_special_assessment: Money(0),
    });
    input
        .scenario
        .scheduled_property_purchases
        .push(ScheduledPropertyPurchaseSpec {
            month: 0,
            cause_id: "buy-test-home".into(),
            property_id: "test-home".into(),
            location_id: "test-place".into(),
            buyer_agent_id: "alice".into(),
            buyer_account_id: "checking".into(),
            seller_agent_id: "world".into(),
            seller_account_id: "checking".into(),
            purchase_price: Money(10_000),
            down_payment: Money(1_000),
            buyer_closing_cost: Money(0),
            rented_fraction_ppb: 0,
            land_value_fraction_ppb: 200_000_000,
            mortgage: Some(MortgageFinancingSpec {
                liability_id: "test-loan".into(),
                lender_agent_id: "world".into(),
                lender_account_id: "checking".into(),
                principal: Money(9_000),
                annual_interest_rate_ppb: 0,
                term_months: 90,
            }),
        });
    input.scenario.initial_lots.push(InitialLotSpec {
        lot_id: "private-test-lot".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        asset_id: "private_equity:test-issuer".into(),
        purchase_month: -24,
        quantity_scale: 1_000_000,
        units: Quantity(1_000_000),
        basis: Money(50),
    });
    input.scenario.initial_bonds.push(BondSpec {
        bond_id: "test-indexed-bond".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        issuer_jurisdiction_id: None,
        face_value: Money(1_000),
        purchase_price: Money(1_000),
        annual_coupon_rate_ppb: 0,
        coupon_period_months: 6,
        inflation_indexed: true,
        purchase_month_index: -5,
        maturity_month_index: 13,
    });
    input
        .scenario
        .income_sources
        .push(IncomeSource::interest(None));
    input.scenario.tax_profiles.push(TaxProfileSpec {
        agent_id: "alice".into(),
        tax_authority_agent_id: "world".into(),
        payment_account_id: "checking".into(),
        tax_authority_account_id: "checking".into(),
        prior_year_tax: Money(0),
        section_121_exclusion: Money(0),
        jurisdictions: vec![TaxRules {
            jurisdiction_id: "test-stop-tax".into(),
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
    input.scenario.obligations.push(ObligationSpec {
        month: 12,
        obligation_id: "unfunded-extra".into(),
        obligation_type: "cash_spend".into(),
        from: AccountRef::new("alice", "checking"),
        to: component.to.clone(),
        amount_due: Money(950).into(),
        property_id: None,
        deduction_category: None,
        deductible_fraction_ppb: WIRE_RATE_SCALE,
    });
    for (series_id, value) in [
        ("home_value:test-place", 10_000),
        ("private_equity_mark:test-issuer", 100),
    ] {
        input.series.push(SeriesSpec {
            series_id: series_id.into(),
            snapshots: horizon + 1,
            values: vec![value; horizon as usize + 1],
        });
    }
    for series in &mut input.series {
        for value in &mut series.values[13..] {
            *value *= future_multiplier;
        }
    }
    for (channel, value) in [
        ("regime", 1),
        ("event_kind", 0),
        ("sale_opportunity", 0),
        ("sale_capacity", 0),
        ("eligible", 0),
        ("forced_sale", 0),
        ("liquidity_blocked", 0),
        ("forced_recovery", 0),
        ("company_valuation", 0),
    ] {
        input.series.push(SeriesSpec {
            series_id: private_equity_series_id(channel, "test-issuer"),
            snapshots: horizon + 1,
            values: vec![value; horizon as usize + 1],
        });
    }
    (input, component)
}

#[test]
fn stopped_books_preserve_positions_debt_tax_and_other_group_consumption() {
    // Synthetic integer-money control: sell 1,000 with 500 gain, accrue 50 tax;
    // pay 1,000 down and eleven 100 principal payments. At m12, cash 1,000 cannot
    // fund 950 + mortgage 100 + tax 50, while the separate budget can pay 300.
    for horizon in [13, 15] {
        let make_policy = |_| {
            |observation: spending::Observation| {
                assert!(observation.month <= 12);
                Ok(Money(if observation.month == 12 { 300 } else { 0 }))
            }
        };
        let (input, component) = stopped_book_fixture(horizon, 2);
        let forensic = spending::simulate(&input, &component, make_policy).unwrap();
        let summary = spending::simulate_summary(&input, &component, make_policy).unwrap();
        let (different_future, _) = stopped_book_fixture(horizon, 9);
        assert_eq!(
            forensic,
            spending::simulate(&different_future, &component, make_policy).unwrap()
        );
        let rollout = &forensic.rollouts[0];
        assert_eq!(rollout.failed_month, Some(12));
        assert_eq!(rollout.months.len(), 14);
        let stopped = rollout.months.last().unwrap();
        assert_eq!(stopped.month, 13);
        let cash = |account: &str| {
            stopped
                .balances
                .iter()
                .find(|row| row.account == AccountRef::new("alice", account))
                .unwrap()
                .balance
        };
        assert_eq!(cash("checking"), Money(1_000));
        assert_eq!(cash("budget"), Money(200));
        assert_eq!(stopped.lots[0].units_remaining, Quantity(99_000_000));
        assert_eq!(stopped.lots[0].basis_remaining, Money(49_500));
        assert_eq!(stopped.properties[0].adjusted_basis, Money(10_000));
        assert_eq!(stopped.mortgages[0].principal, Money(7_900));
        assert_eq!(stopped.tax_liabilities[0].amount_owed, Money(50));
        assert_eq!(stopped.bonds[0].principal, Money(1_000));
        assert!(stopped.bonds[0].active); // Redemption at m13 has not happened.
        assert_eq!(summary.consumption_requested[0][12], Money(300));
        assert_eq!(summary.consumption_paid[0][12], Money(300));
        let metrics = &summary.product_metrics.base_series;
        for (name, expected) in [
            ("cash_quanta", 1_200),
            ("holding_value_quanta", 99_000),
            ("private_equity_value_quanta", 100),
            ("property_value_quanta", 10_000),
            ("mortgage_balance_quanta", 7_900),
            ("bond_value_quanta", 1_000),
            ("shortfall_quanta", 1_100),
        ] {
            let slot = crate::product::BASE_METRIC_NAMES
                .iter()
                .position(|item| *item == name)
                .unwrap();
            assert_eq!(metrics[slot][13], expected, "{name}");
        }
        assert!(rollout.journal.iter().all(|entry| entry.month <= 12));
        assert!(
            rollout
                .obligations
                .iter()
                .all(|receipt| receipt.month <= 12)
        );
        assert_eq!(
            stopped
                .balances
                .iter()
                .map(|row| i128::from(row.balance.0))
                .sum::<i128>(),
            0
        );

        let mut scheduled = input.clone();
        scheduled.scenario.obligations.push(ObligationSpec {
            month: 12,
            obligation_id: "scheduled-budget-control".into(),
            obligation_type: "cash_spend".into(),
            from: component.from.clone(),
            to: component.to.clone(),
            amount_due: Money(300).into(),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: WIRE_RATE_SCALE,
        });
        let endings = simulate_summaries(&scheduled).unwrap();
        let ending = &endings.rollouts[0];
        assert_eq!(ending.failed_month, rollout.failed_month);
        assert_eq!(ending.ending_balances, stopped.balances);
        assert_eq!(ending.ending_properties, stopped.properties);
        assert_eq!(ending.ending_mortgages, stopped.mortgages);
        assert_eq!(ending.ending_bonds, stopped.bonds);
        assert_eq!(ending.ending_tax_liabilities, stopped.tax_liabilities);
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

fn mid_horizon_property_fixture(financed: bool, closing_cost_ppb: i64) -> ExecutionInput {
    let mut input = minimal_fixture();
    input.rollout_count = 2;
    input.scenario.horizon_months = 6;
    input.scenario.accounts[0].opening_balance = Money(200_000);
    input.scenario.accounts.push(AccountSpec {
        account: AccountRef::new("world", "checking"),
        opening_balance: Money(0),
    });
    input.scenario.locations.push(LocationSpec {
        location_id: "test-market".into(),
        display_name: "Test market".into(),
        jurisdiction_ids: vec![],
        annual_property_tax_rate_ppb: 0,
        annual_special_assessment: Money(0),
    });
    input
        .scenario
        .scheduled_property_purchases
        .push(ScheduledPropertyPurchaseSpec {
            month: 2,
            cause_id: "test-mid-horizon-purchase".into(),
            property_id: "test-home".into(),
            location_id: "test-market".into(),
            buyer_agent_id: "alice".into(),
            buyer_account_id: "checking".into(),
            seller_agent_id: "world".into(),
            seller_account_id: "checking".into(),
            purchase_price: Money(100_000),
            down_payment: Money(if financed { 40_000 } else { 100_000 }),
            buyer_closing_cost: Money(0),
            rented_fraction_ppb: 0,
            land_value_fraction_ppb: 200_000_000,
            mortgage: financed.then(|| MortgageFinancingSpec {
                liability_id: "test-mortgage".into(),
                lender_agent_id: "world".into(),
                lender_account_id: "checking".into(),
                principal: Money(60_000),
                annual_interest_rate_ppb: 0,
                term_months: 60,
            }),
        });
    input.scenario.property_sales.push(PropertySaleSpec {
        month: 5,
        property_id: "test-home".into(),
        closing_cost_ppb,
    });
    input.series.push(SeriesSpec {
        series_id: "home_value:test-market".into(),
        snapshots: 7,
        // Same purchase and later marks, different pre-purchase histories.
        values: vec![
            50, 100, 200, 240, 300, 360, 800, 500, 7, 200, 240, 300, 360, 800,
        ],
    });
    input
}

#[test]
fn mid_horizon_property_mark_and_sale_share_the_purchase_anchor() {
    for financed in [false, true] {
        for closing_cost_ppb in [0, 100_000_000] {
            let input = mid_horizon_property_fixture(financed, closing_cost_ppb);
            let output = simulate(&input).unwrap();
            let metrics = simulate_product_metrics(&input, "alice").unwrap();
            let property_slot = crate::product::BASE_METRIC_NAMES
                .iter()
                .position(|name| *name == "property_value_quanta")
                .unwrap();
            for rollout in &output.rollouts {
                assert_eq!(rollout.failed_month, None);
                assert_eq!(rollout.property_purchases[0].purchase_price, Money(100_000));
                assert_eq!(
                    rollout.months[3].properties[0].adjusted_basis,
                    Money(100_000)
                );
                // Buy at index 200; the held property follows 240, 300 and 360.
                // Snapshot 5 is the opening sale-month book, before disposal.
                for (snapshot, expected) in [0, 0, 0, 120_000, 150_000, 180_000, 0]
                    .into_iter()
                    .enumerate()
                {
                    assert_eq!(
                        metrics.base_series[property_slot]
                            [snapshot * 2 + rollout.rollout_id as usize],
                        expected
                    );
                }
                let sale = &rollout.property_sales[0];
                let seller_cost = if closing_cost_ppb == 0 { 0 } else { 18_000 };
                let payoff = if financed { 58_000 } else { 0 };
                // The outcome's gross_proceeds is AFTER seller costs, before debt payoff.
                assert_eq!(sale.gross_proceeds, Money(180_000 - seller_cost));
                assert_eq!(sale.mortgage_payoff, Money(payoff));
                assert_eq!(
                    sale.net_cash_to_owner,
                    Money(180_000 - seller_cost - payoff)
                );
                assert_eq!(sale.realized_gain, Money(80_000 - seller_cost));
                assert_eq!(sale.depreciation_recapture, Money(0));
                assert_eq!(sale.section_121_exclusion, Money(0));
                let reported_mark =
                    metrics.base_series[property_slot][5 * 2 + rollout.rollout_id as usize];
                assert_eq!(sale.gross_proceeds.0 + seller_cost, reported_mark);
                let cash = rollout.months[6]
                    .balances
                    .iter()
                    .find(|balance| balance.account == AccountRef::new("alice", "checking"))
                    .unwrap()
                    .balance;
                assert_eq!(cash, Money(280_000 - seller_cost));
                assert!(rollout.journal.iter().all(|entry| {
                    entry
                        .postings
                        .iter()
                        .map(|posting| i128::from(posting.amount.0))
                        .sum::<i128>()
                        == 0
                }));
            }
        }
    }
}

#[test]
fn stopped_mid_horizon_property_uses_only_the_failure_month_mark() {
    let mut input = mid_horizon_property_fixture(true, 0);
    input.scenario.obligations.push(ObligationSpec {
        month: 4,
        obligation_id: "test-unfundable-demand".into(),
        obligation_type: "cash_spend".into(),
        from: AccountRef::new("alice", "checking"),
        to: AccountRef::new("world", "checking"),
        amount_due: Money(999_999).into(),
        property_id: None,
        deduction_category: None,
        deductible_fraction_ppb: 0,
    });
    // A different unobserved future must not change either stopped book.
    input.series[0].values[12..].copy_from_slice(&[9_000, 1]);
    let output = simulate(&input).unwrap();
    let metrics = simulate_product_metrics(&input, "alice").unwrap();
    let property_slot = crate::product::BASE_METRIC_NAMES
        .iter()
        .position(|name| *name == "property_value_quanta")
        .unwrap();
    assert_eq!(metrics.failed_month, vec![4, 4]);
    for rollout in &output.rollouts {
        assert_eq!(rollout.months.len(), 6);
        assert_eq!(rollout.months[5].month, 5);
        assert!(rollout.months[5].properties[0].active);
        assert!(rollout.property_sales.is_empty());
        assert_eq!(
            metrics.base_series[property_slot][5 * 2 + rollout.rollout_id as usize],
            150_000
        );
    }
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
fn failure_stops_future_actions_and_preserves_the_observed_book() {
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
    assert_eq!(rollout.months.len(), 2);
    assert_eq!(rollout.months[1].balances, rollout.months[0].balances);
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
