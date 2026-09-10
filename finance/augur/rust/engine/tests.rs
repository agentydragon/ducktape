//! Unit tests for the engine's internals.

use crate::execution::{
    AccountSpec, BondSpec, DistributionSpec, DistributionTaxSliceSpec, HoldingPoolSpec,
    InitialLotSpec, JurisdictionIdentitySpec, LocationSpec, MortgageFinancingSpec, ObligationSpec,
    PropertyTaxPolicySpec, RecurringObligationSpec, ScenarioSpec, ScheduledPropertyPurchaseSpec,
    ScheduledSaleSpec, ScheduledTransferSpec, SeriesIndexedAmountKind, SeriesIndexedAmountSpec,
    SeriesSpec, SleeveTargetSpec, TargetAllocationPolicySpec, TaxProfileSpec,
};
use crate::tax::TaxBracket;

use super::*;

#[path = "actors_test.rs"]
mod actors;

#[path = "private_equity_test.rs"]
mod private_equity;

#[path = "trades_test.rs"]
mod trades;

#[path = "transfers_test.rs"]
mod transfers;

fn holding_pool(agent: &str, account: &str, asset: &str, scale: i64) -> HoldingPoolSpec {
    HoldingPoolSpec {
        agent_id: agent.into(),
        account_id: account.into(),
        asset_id: asset.into(),
        quantity_scale: scale,
    }
}

pub(super) fn minimal_fixture() -> ExecutionInput {
    ExecutionInput {
        schema_version: INPUT_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 1,
        scenario: ScenarioSpec {
            horizon_months: 1,
            holding_pools: vec![],
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

/// Test input account bindings, not an executable spending-policy interface.
pub(super) struct CashRoute {
    pub(super) from: AccountRef,
    pub(super) to: AccountRef,
    pub(super) cause_id: String,
}

/// Inspect already-retained configured books without supplying actions or budgets.
/// Financial work stays in the existing monthly step, including housing and PE.
fn inspect_opening_books(
    input: &ExecutionInput,
    agent_id: &str,
    mut inspect: impl FnMut(observations::ActorBooks<'_>) -> Result<(), SimulationError>,
) -> Result<SimulationOutput, SimulationError> {
    ValidatedInput::new(input)?;
    let scope = AgentHoldings::resolve(input, agent_id)?;
    let mut rollouts = Vec::new();
    for rollout in 0..input.rollout_count {
        let mut state = RolloutState::new(input, rollout, CaptureMode::Forensic, None)?;
        while !state.is_finished(input) {
            inspect(observations::ActorBooks {
                scope: &scope,
                books: observations::Books {
                    ledger: &state.ledger,
                    lots: &state.lots,
                    mortgages: &state.mortgages,
                    tax: &state.tax,
                    tax_liabilities: &state.tax_liabilities,
                    tlh_cumulative_harvest: &state.tlh_cumulative_harvest,
                },
                input,
                rollout,
                month: state.month,
            })?;
            state = state.advance_month(input, None)?;
        }
        rollouts.push(state.finish(input)?.into_output());
    }
    Ok(SimulationOutput {
        schema_version: INPUT_SCHEMA_VERSION,
        rollouts,
    })
}

pub(super) fn spending_fixture() -> (ExecutionInput, CashRoute) {
    let mut fixture = minimal_fixture();
    fixture.rollout_count = 2;
    fixture.scenario.horizon_months = 13;
    fixture.scenario.accounts[0].opening_balance = Money(100_000);
    let spending = CashRoute {
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
fn configured_indexed_consumption_retains_funding_and_tax_events() {
    let (mut fixture, spending) = spending_fixture();
    fixture.scenario.accounts[0].opening_balance = Money(0);
    fixture.scenario.holding_pools = vec![holding_pool("alice", "checking", "stock", 1_000_000)];
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
    for rollout in &scheduled.rollouts {
        assert_eq!(rollout.failed_month, None);
        assert!(!rollout.dispositions.is_empty());
        assert!(!rollout.tax_accruals.is_empty());
        assert!(!rollout.tax_payments.is_empty());
    }
}

fn policy_timing_fixture(horizon_months: u32) -> (ExecutionInput, CashRoute) {
    let (mut input, spending) = spending_fixture();
    input.rollout_count = 1;
    input.scenario.horizon_months = horizon_months;
    input.scenario.accounts[0].opening_balance = Money(10_000);
    input.scenario.holding_pools = vec![holding_pool("alice", "checking", "stock", 1_000_000)];
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
    input
        .scenario
        .holding_pools
        .push(holding_pool("alice", "checking", "second", 1_000_000));
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

fn scoped_observation_fixture() -> (ExecutionInput, CashRoute) {
    let (mut input, spending) = policy_timing_fixture(3);
    input.scenario.holding_pools = vec![
        holding_pool("alice", "checking", "stock", 10),
        holding_pool("alice", "checking", "second", 10),
        holding_pool("alice", "reserve", "stock", 10),
        holding_pool("bob", "checking", "stock", 10),
    ];
    input.scenario.accounts[0].opening_balance = Money(100);
    for (agent, account, cash) in [("alice", "reserve", 900), ("bob", "checking", 5_000)] {
        input.scenario.accounts.push(AccountSpec {
            account: AccountRef::new(agent, account),
            opening_balance: Money(cash),
        });
    }
    input.scenario.initial_lots = [
        ("half-a", "alice", "checking", "stock", 5),
        ("half-b", "alice", "checking", "stock", 5),
        ("second", "alice", "checking", "second", 4),
        ("reserve", "alice", "reserve", "stock", 5),
        ("other-actor", "bob", "checking", "stock", 1_000),
    ]
    .into_iter()
    .map(|(id, agent, account, asset, units)| InitialLotSpec {
        lot_id: id.into(),
        agent_id: agent.into(),
        account_id: account.into(),
        asset_id: asset.into(),
        purchase_month: -24,
        quantity_scale: 10,
        units: Quantity(units),
        basis: Money(0),
    })
    .collect();
    input.series[0].values = vec![1_000_000_000, 1_500_000_000, 2_000_000_000, 3_000_000_000];
    input.series[1].values = vec![1, 3, 5, 7];
    input.series.push(SeriesSpec {
        series_id: "security:second".into(),
        snapshots: 4,
        values: vec![2, 4, 6, 8],
    });
    input.scenario.target_allocation_policies[0].sleeves = ["stock", "second"]
        .into_iter()
        .map(|asset| SleeveTargetSpec {
            asset_id: asset.into(),
            weight: 1,
            quantity_scale: 10,
        })
        .collect();
    (input, spending)
}

#[test]
fn scoped_observations_match_output_at_same_marks_and_round_each_lot() {
    let (input, _) = scoped_observation_fixture();
    let metrics = simulate_product_metrics(&input, "alice").unwrap();
    // Three half-share stock lots at price 1 each round to 1; the second sleeve
    // rounds 0.8 to 1. Summing stock quantities first would instead report 3 total.
    assert_eq!(metrics.base_series[0], vec![1_000; 4]);
    assert_eq!(metrics.base_series[1], vec![4, 8, 11, 15]);
    let baseline = simulate(&input).unwrap();
    let stepped = inspect_opening_books(&input, "alice", |books| {
        let month = books.month() as usize;
        assert_eq!(books.cash()?.0, metrics.base_series[0][month]);
        assert_eq!(books.public_value()?.0, metrics.base_series[1][month]);
        assert_eq!(books.agent_id(), "alice");
        assert_eq!(books.month(), books.month());
        let accounts = books
            .accounts()
            .map(|account| {
                let account = account.unwrap();
                (account.account.account_id.as_str(), account.available)
            })
            .collect::<Vec<_>>();
        assert_eq!(
            accounts,
            [("checking", Money(100)), ("reserve", Money(900))]
        );
        let positions = books
            .public_positions()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(positions.len(), 4);
        assert_eq!(positions[0].lot_id(), "half-a");
        assert_eq!(positions[0].account_id(), "checking");
        assert_eq!(positions[0].asset_id(), "stock");
        assert_eq!(positions[0].purchase_month(), -24);
        assert_eq!(positions[0].units(), Units::new(Quantity(5), 10));
        assert_eq!(positions[0].book_basis(), Money(0));
        assert_eq!(positions[0].price, PerUnit([1, 3, 5][month]));
        assert_eq!(
            positions
                .iter()
                .map(|position| position.value().unwrap().0)
                .sum::<i64>(),
            books.public_value()?.0
        );
        assert_eq!(
            Money(2)
                .scaled_by(books.cpi()?.unwrap(), "test CPI")
                .unwrap(),
            Money([2, 3, 4][month])
        );
        Ok(())
    })
    .unwrap();
    assert_eq!(stepped, baseline);
}

#[test]
fn actor_books_do_not_read_future_prices_or_cpi() {
    let (input, _) = scoped_observation_fixture();
    let mut changed_future = input.clone();
    for series in &mut changed_future.series {
        for value in &mut series.values[2..] {
            *value *= 2;
        }
    }
    let capture = |input: &ExecutionInput| {
        let mut seen = Vec::new();
        inspect_opening_books(input, "alice", |books| {
            seen.push((
                books.month(),
                books.cash()?,
                books.public_value()?,
                books.cpi()?,
            ));
            Ok(())
        })
        .unwrap();
        seen
    };
    let baseline = capture(&input);
    let changed = capture(&changed_future);
    assert_eq!(baseline[..2], changed[..2]);
    assert_ne!(baseline[2], changed[2]);
}

#[test]
fn actor_books_follow_partial_sales_and_hide_exhausted_lots() {
    let (mut input, _) = scoped_observation_fixture();
    input.scenario.initial_lots[0].basis = Money(7);
    input.scenario.scheduled_sales.push(ScheduledSaleSpec {
        month: 0,
        cause_id: "test-partial-sale".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        asset_id: "stock".into(),
        units: Quantity(2),
        proceeds_account_id: "checking".into(),
    });
    input.scenario.scheduled_sales.push(ScheduledSaleSpec {
        month: 1,
        cause_id: "test-exhaust-two-lots".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        asset_id: "stock".into(),
        units: Quantity(8),
        proceeds_account_id: "checking".into(),
    });
    inspect_opening_books(&input, "alice", |books| {
        let positions = books.public_positions().collect::<Result<Vec<_>, _>>()?;
        if books.month() == 2 {
            assert_eq!(
                positions.iter().map(|lot| lot.lot_id()).collect::<Vec<_>>(),
                ["second", "reserve"]
            );
            return Ok(());
        }
        assert_eq!(positions.len(), 4);
        let first = &positions[0];
        assert_eq!(first.lot_id(), "half-a");
        assert_eq!(
            first.units().quantity(),
            Quantity(if books.month() == 0 { 5 } else { 3 })
        );
        // A two-fifths sale consumes 3 of the 7 basis quanta; never show original basis.
        assert_eq!(
            first.book_basis(),
            Money(if books.month() == 0 { 7 } else { 4 })
        );
        Ok(())
    })
    .unwrap();
}

#[test]
fn actor_books_reject_unpriced_public_positions_before_inspection() {
    let (mut input, _) = scoped_observation_fixture();
    input.scenario.target_allocation_policies.clear();
    input
        .series
        .retain(|series| series.series_id != "security:second");
    assert!(matches!(
        inspect_opening_books(&input, "alice", |_| {
                panic!("unpriced holdings must reject before inspecting books")
            }),
        Err(SimulationError::MissingSeries { series_id })
            if series_id == "security:second"
    ));
    assert!(matches!(
        AgentHoldings::resolve(&input, "alice"),
        Err(HoldingsError::MissingSeries { series_id }) if series_id == "security:second"
    ));
    assert!(matches!(
        AgentHoldings::resolve(&input, "absent-actor"),
        Err(HoldingsError::UnknownAgent { .. })
    ));
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
fn retained_rollouts_keep_opening_books_lots_and_tax_state_independent() {
    // Same opening books, two stipulated price paths: raising 40,000 realizes
    // gains of 20,000 or 30,000. The synthetic 10% tax is paid the next year.
    let mut input = allocation_tax_and_consumption_fixture();
    input.rollout_count = 2;
    for series in &mut input.series {
        let multiplier = if series.series_id.starts_with("security:") {
            2
        } else {
            1
        };
        series.values.extend(
            series
                .values
                .clone()
                .into_iter()
                .map(|value| value * multiplier),
        );
    }
    let validated = ValidatedInput::new(&input).unwrap();
    // Both states exist before either runs, and execution need not follow path order.
    let [second, first] = [1, 0]
        .map(|id| RolloutState::new(validated.input, id, CaptureMode::Forensic, None).unwrap());
    for (state, tax_paid, remaining_basis) in [(second, 3_000, 40_000), (first, 2_000, 30_000)] {
        let output = state.run(&input, None).unwrap().into_output();
        assert_eq!(output.failed_month, None);
        assert_eq!(output.tax_payments[0].month, 12);
        assert_eq!(output.tax_payments[0].amount_paid, Money(tax_paid));
        assert_eq!(
            output
                .journal
                .iter()
                .filter(|entry| entry.cause_id == "opening:alice:checking")
                .count(),
            1
        );
        let ending = output.months.last().unwrap();
        assert_eq!(ending.month, 13);
        assert_eq!(
            ending
                .balances
                .iter()
                .find(|row| row.account == AccountRef::new("alice", "checking"))
                .unwrap()
                .balance,
            Money(5_000 - tax_paid)
        );
        assert_eq!(
            ending
                .lots
                .iter()
                .map(|lot| lot.basis_remaining.0)
                .sum::<i64>(),
            remaining_basis
        );
    }
}

#[test]
fn month_stepping_preserves_tax_year_and_stopped_books_in_every_capture_mode() {
    for (input, year_end_tax) in [
        (allocation_tax_and_consumption_fixture(), Money(2_000)),
        (stopped_book_fixture(15, 9).0, Money(50)),
    ] {
        ValidatedInput::new(&input).unwrap();
        let product = ProductInputs::resolve(&input, "alice").unwrap();
        for capture in [
            CaptureMode::Forensic,
            CaptureMode::Dense,
            CaptureMode::Summary,
        ] {
            let full = simulate_rollout(&input, 0, capture, Some(&product)).unwrap();
            let mut state = RolloutState::new(&input, 0, capture, Some(&product)).unwrap();
            for _ in 0..12 {
                state = state.advance_month(&input, Some(&product)).unwrap();
            }
            // Assessment is retained across the pause before the next year's payment.
            assert_eq!(state.tax_liabilities[0].amount_owed, year_end_tax);
            assert!(!state.is_finished(&input));
            while !state.is_finished(&input) {
                state = state.advance_month(&input, Some(&product)).unwrap();
            }
            // Neither a completed nor a failed path processes another month's events.
            state = state.advance_month(&input, Some(&product)).unwrap();
            let stepped = state.finish(&input).unwrap();
            assert_eq!(stepped.product_metrics, full.product_metrics);
            match capture {
                CaptureMode::Summary => assert_eq!(stepped.into_summary(), full.into_summary()),
                _ => assert_eq!(stepped.into_output(), full.into_output()),
            }
        }
    }
}

#[test]
fn configured_allocation_funds_consumption_and_tax() {
    for purchases in [false, true] {
        let mut input = allocation_tax_and_consumption_fixture();
        input.scenario.target_allocation_policies[0].allow_purchases = purchases;
        let output = simulate(&input).unwrap();
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
fn zero_targets_keep_cashflow_only_and_deposit_controls() {
    let mut input = allocation_fixture(1);
    input.scenario.target_allocation_policies[0].sleeves[0].weight = 0;
    input.scenario.accounts[0].opening_balance = Money(0);
    let quiet = simulate(&input).unwrap();
    assert!(quiet.rollouts[0].dispositions.is_empty());
    assert_eq!(quiet.rollouts[0].months.last().unwrap().lots.len(), 2);

    input.scenario.accounts[0].opening_balance = Money(10_000);
    input.scenario.target_allocation_policies[0].allow_purchases = true;
    input.scenario.target_allocation_policies[0].rebalance_tolerance_ppb = Some(0);
    let deposit = simulate(&input).unwrap();
    let rollout = &deposit.rollouts[0];
    assert!(rollout.dispositions.is_empty()); // Active cash band suppresses drift exits.
    let purchased: Vec<_> = rollout
        .months
        .last()
        .unwrap()
        .lots
        .iter()
        .filter(|lot| lot.purchase_month == 0)
        .collect();
    assert_eq!(purchased.len(), 1);
    assert_eq!(purchased[0].asset_id, "security:second");
    assert_eq!(purchased[0].basis_remaining, Money(10_000));
    input.scenario.target_allocation_policies[0].sleeves[1].weight = 0;
    assert!(matches!(
        simulate(&input),
        Err(SimulationError::InvalidTargetAllocationPolicy { .. })
    ));
}

#[test]
fn zero_target_exit_and_later_sale_fund_canonical_tax_and_consumption() {
    let mut input = allocation_tax_and_consumption_fixture();
    input.scenario.scheduled_transfers.clear();
    let policy = &mut input.scenario.target_allocation_policies[0];
    policy.sleeves[0].weight = 0;
    policy.allow_purchases = true;
    policy.rebalance_tolerance_ppb = Some(0);
    let output = simulate(&input).unwrap();
    let rollout = &output.rollouts[0];
    assert_eq!(rollout.failed_month, None);
    assert_eq!(
        rollout
            .dispositions
            .iter()
            .map(|sale| (sale.month, sale.asset_id.as_str(), sale.proceeds))
            .collect::<Vec<_>>(),
        [
            (0, "security:stock", Money(40_000)),
            (1, "security:stock", Money(10_000)),
            (12, "security:second", Money(7_500))
        ]
    );
    // Month 0's funding raise stops short of the zero target. The quiet month exits
    // the remaining stock; the next tax year raises from the retained sleeve.
    assert_eq!(
        rollout.months[1].lots[0].units_remaining,
        Quantity(10_000_000)
    );
    assert_eq!(rollout.months[2].lots[0].units_remaining, Quantity(0));
    assert_eq!(rollout.tax_payments[0].amount_paid, Money(2_500));
    assert_eq!(rollout.tax_payments[0].month, 12);
    assert_eq!(
        rollout
            .obligations
            .iter()
            .filter(|claim| claim.obligation_type == "cash_spend")
            .map(|claim| claim.amount_paid.0)
            .sum::<i64>(),
        55_000
    );
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
}

#[test]
fn configured_timing_low_and_unfunded_consumption() {
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
        let mut selected = input.clone();
        // These are configured demand arms, not a live policy callback. The common
        // action-session tests cover authored cuts and ordered payment priority.
        let amount = Money(if allow_cut { 30_000 } else { 50_000 });
        selected.scenario.obligations.push(ObligationSpec {
            month: 0,
            obligation_id: spending.cause_id.clone(),
            obligation_type: "cash_spend".into(),
            from: spending.from.clone(),
            to: spending.to.clone(),
            amount_due: amount.into(),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: WIRE_RATE_SCALE,
        });
        let rollout = simulate(&selected).unwrap().rollouts.remove(0);
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
fn configured_timing_surplus_investment_reserves_tax_and_consumption() {
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
        let mut selected = input.clone();
        for (month, amount) in [(0, 50_000), (12, 5_000)] {
            selected.scenario.obligations.push(ObligationSpec {
                month,
                obligation_id: spending.cause_id.clone(),
                obligation_type: "cash_spend".into(),
                from: spending.from.clone(),
                to: spending.to.clone(),
                amount_due: Money(amount).into(),
                property_id: None,
                deduction_category: None,
                deductible_fraction_ppb: WIRE_RATE_SCALE,
            });
        }
        let rollout = inspect_opening_books(&selected, "alice", |books| {
            assert_eq!(
                books.cash()?,
                Money(if books.month() == 0 { 10_000 } else { 0 })
            );
            assert_eq!(
                books.public_value()?,
                Money(if books.month() == 0 { 100_000 } else { 60_000 })
            );
            Ok(())
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

pub(super) fn stopped_book_fixture(
    horizon: u32,
    future_multiplier: i64,
) -> (ExecutionInput, CashRoute) {
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
    input.scenario.holding_pools.push(holding_pool(
        "alice",
        "checking",
        "private_equity:test-issuer",
        1_000_000,
    ));
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
        let (mut input, component) = stopped_book_fixture(horizon, 2);
        input.scenario.obligations.push(ObligationSpec {
            month: 12,
            obligation_id: component.cause_id.clone(),
            obligation_type: "cash_spend".into(),
            from: component.from.clone(),
            to: component.to.clone(),
            amount_due: Money(300).into(),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: WIRE_RATE_SCALE,
        });
        let forensic = inspect_opening_books(&input, "alice", |books| {
            assert!(books.month() <= 12);
            // Private lots, individual bonds and housing are not public securities.
            assert_eq!(
                books.public_value()?,
                Money(if books.month() == 0 { 100_000 } else { 99_000 })
            );
            Ok(())
        })
        .unwrap();
        let metrics = simulate_product_metrics(&input, "alice").unwrap();
        let (mut different_future, _) = stopped_book_fixture(horizon, 9);
        different_future.scenario.obligations.push(ObligationSpec {
            month: 12,
            obligation_id: component.cause_id.clone(),
            obligation_type: "cash_spend".into(),
            from: component.from.clone(),
            to: component.to.clone(),
            amount_due: Money(300).into(),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: WIRE_RATE_SCALE,
        });
        assert_eq!(forensic, simulate(&different_future).unwrap());
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
        let consumption = rollout
            .obligations
            .iter()
            .find(|receipt| receipt.cause_id == format!("{}_m12", component.cause_id))
            .unwrap();
        assert_eq!(consumption.amount_due, Money(300));
        assert_eq!(consumption.amount_paid, Money(300));
        let metrics = &metrics.base_series;
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

        let endings = simulate_summaries(&input).unwrap();
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
fn actor_books_expose_only_originated_contracts_and_recorded_tax() {
    for future_multiplier in [2, 9] {
        let (mut input, component) = stopped_book_fixture(15, future_multiplier);
        input.scenario.accounts.push(AccountSpec {
            account: AccountRef::new("bob", "checking"),
            opening_balance: Money(50_000),
        });
        let mut other_taxpayer = input.scenario.tax_profiles[0].clone();
        other_taxpayer.agent_id = "bob".into();
        input.scenario.tax_profiles.push(other_taxpayer);
        let mut other_purchase = input.scenario.scheduled_property_purchases[0].clone();
        other_purchase.cause_id = "test-other-purchase".into();
        other_purchase.property_id = "test-other-home".into();
        other_purchase.buyer_agent_id = "bob".into();
        other_purchase.mortgage.as_mut().unwrap().liability_id = "test-other-loan".into();
        input
            .scenario
            .scheduled_property_purchases
            .push(other_purchase);
        input
            .scenario
            .scheduled_transfers
            .push(ScheduledTransferSpec {
                month: 0,
                cause_id: "test-other-taxpayer-income".into(),
                from: component.to.clone(),
                to: AccountRef::new("bob", "checking"),
                amount: Money(1_000).into(),
                income_category: Some(IncomeSource::Ordinary),
                deduction_category: None,
            });
        input.scenario.obligations.push(ObligationSpec {
            month: 12,
            obligation_id: component.cause_id.clone(),
            obligation_type: "cash_spend".into(),
            from: component.from.clone(),
            to: component.to.clone(),
            amount_due: Money(300).into(),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: WIRE_RATE_SCALE,
        });
        inspect_opening_books(&input, "alice", |books| {
            assert!(books.month() <= 12, "no observations after failure");
            assert_eq!(
                books.public_positions().count(),
                1,
                "private lots are not public"
            );
            let mortgages = books.mortgages().collect::<Vec<_>>();
            if books.month() == 0 {
                assert!(mortgages.is_empty(), "a planned loan has not originated");
            } else {
                assert_eq!(mortgages.len(), 1);
                let mortgage = mortgages[0];
                assert_eq!(mortgage.liability_id, "test-loan");
                assert_eq!(
                    mortgage.principal,
                    Money(9_000 - 100 * i64::from(books.month() - 1))
                );
                assert_eq!(mortgage.monthly_payment, Money(100));
            }
            assert!(
                books.income().all(|(_, amount)| amount == Money(0)),
                "Bob's income is private"
            );
            let facts = books.tax_facts().collect::<Vec<_>>();
            assert_eq!(facts.len(), 1, "only Alice's jurisdiction facts");
            assert_eq!(facts[0].0, "test-stop-tax");
            assert_eq!(
                facts[0].1.long_term_gain,
                Money(if (1..12).contains(&books.month()) {
                    500
                } else {
                    0
                })
            );
            let liabilities = books.tax_liabilities().collect::<Vec<_>>();
            if books.month() < 12 {
                assert!(
                    liabilities.is_empty(),
                    "future assessment is not a known liability"
                );
            } else {
                assert_eq!(liabilities.len(), 1);
                assert_eq!(liabilities[0].agent_id, "alice");
                assert_eq!(liabilities[0].tax_year_end_month, 11);
                assert_eq!(liabilities[0].amount_owed, Money(50));
            }
            Ok(())
        })
        .unwrap();
    }
}

#[test]
fn actor_books_keep_pool_harvest_adjustments_separate_from_lot_basis() {
    let (mut input, component) = stopped_book_fixture(15, 2);
    input.scenario.harvest_policies.push(HarvestPolicySpec {
        owner_agent_id: "alice".into(),
        account_id: "checking".into(),
        asset_id: "stock".into(),
        peak_annual_yield_ppb: 120_000_000,
        floor_annual_yield_ppb: 120_000_000,
        maturity_decay_exponent_ppb: WIRE_RATE_SCALE,
        drawdown_sensitivity_ppb: 0,
        short_term_fraction_ppb: WIRE_RATE_SCALE,
    });
    input.scenario.obligations.push(ObligationSpec {
        month: 1,
        obligation_id: component.cause_id.clone(),
        obligation_type: "cash_spend".into(),
        from: component.from.clone(),
        to: component.to.clone(),
        amount_due: Money(1_000_000).into(),
        property_id: None,
        deduction_category: None,
        deductible_fraction_ppb: WIRE_RATE_SCALE,
    });
    inspect_opening_books(&input, "alice", |books| {
        let adjustments = books.harvest_adjustments().collect::<Vec<_>>();
        assert_eq!(adjustments.len(), 1);
        assert_eq!(adjustments[0].account_id, "checking");
        assert_eq!(adjustments[0].asset_id, "stock");
        assert_eq!(
            adjustments[0].cumulative_harvest,
            Money(if books.month() == 0 { 0 } else { 990 })
        );
        let lot = books.public_positions().next().unwrap()?;
        assert_eq!(
            lot.book_basis(),
            Money(if books.month() == 0 { 50_000 } else { 49_500 })
        );
        // Stop in m1, after observing one month's harvest of 1% of 99,000.
        Ok(())
    })
    .unwrap();
}

#[test]
fn configured_claims_use_current_contracts_and_assessments_without_settling() {
    for future_multiplier in [2, 9] {
        let (input, _) = stopped_book_fixture(15, future_multiplier);
        // A configured purchase has not yet originated its mortgage at opening m0.
        assert!(
            claims::assemble(&input, 0, 0, &[], &[], &[])
                .unwrap()
                .entries
                .is_empty()
        );
        let output = simulate(&input).unwrap();
        let opening = &output.rollouts[0].months[12];
        let demands = claims::assemble(
            &input,
            0,
            12,
            &opening.properties,
            &opening.mortgages,
            &opening.tax_liabilities,
        )
        .unwrap();
        assert_eq!(
            observations::due_claims(&demands, "alice")
                .map(|claim| (claim.cause_id, claim.amount_due))
                .collect::<Vec<_>>(),
            [
                ("unfunded-extra_m12", Money(950)),
                ("test-loan_payment_m12", Money(100)),
                ("alice_tax_true_up_y0", Money(50)),
            ]
        );
        assert!(matches!(
            demands.entries[1].effect,
            ObligationEffect::Mortgage {
                interest: Money(0),
                principal: Money(100),
                ..
            }
        ));
        assert!(matches!(
            demands.entries[2].effect,
            ObligationEffect::TaxTrueUp {
                tax_year_end_month: 11,
                ..
            }
        ));
        assert_eq!(opening.mortgages[0].principal, Money(7_900));
        assert_eq!(opening.tax_liabilities[0].amount_owed, Money(50));
        assert_eq!(
            opening
                .balances
                .iter()
                .find(|balance| balance.account == AccountRef::new("alice", "checking"))
                .unwrap()
                .balance,
            Money(1_000)
        );
    }
}

#[test]
fn claim_views_keep_assembled_amount_identity_and_payer_scope() {
    let mut obligations = claims::Claims {
        month: 3,
        entries: vec![
            ActiveObligation {
                paid: false,
                cause_id: "test-rent-m3".into(),
                obligation_type: "rent".into(),
                from: AccountRef::new("alice", "checking"),
                to: AccountRef::new("landlord", "checking"),
                amount_due: Money(700),
                effect: ObligationEffect::None,
            },
            ActiveObligation {
                paid: false,
                cause_id: "test-tax-m3".into(),
                obligation_type: "estimated_tax".into(),
                from: AccountRef::new("alice", "reserve"),
                to: AccountRef::new("authority", "checking"),
                amount_due: Money(300),
                effect: ObligationEffect::TaxPayment { profile_index: 0 },
            },
            ActiveObligation {
                paid: false,
                cause_id: "test-other-actor-m3".into(),
                obligation_type: "rent".into(),
                from: AccountRef::new("bob", "checking"),
                to: AccountRef::new("landlord", "checking"),
                amount_due: Money(9_000),
                effect: ObligationEffect::None,
            },
        ],
    };
    let claims = observations::due_claims(&obligations, "alice").collect::<Vec<_>>();
    assert_eq!(claims.len(), 2);
    assert_eq!(claims[0].cause_id, "test-rent-m3");
    assert_eq!(claims[0].obligation_type, "rent");
    assert_eq!(claims[0].from, &AccountRef::new("alice", "checking"));
    assert_eq!(claims[0].to, &AccountRef::new("landlord", "checking"));
    assert_eq!(claims[0].amount_due, Money(700));
    assert_eq!(claims[0].due_month, 3);
    assert_eq!(claims[1].from, &AccountRef::new("alice", "reserve"));
    assert_eq!(claims[1].amount_due, Money(300));
    assert!(
        observations::due_claims(&obligations, "landlord")
            .next()
            .is_none()
    );
    // A new view reads the canonical amount; there is no synchronized claim copy.
    obligations.entries[0].amount_due = Money(725);
    assert_eq!(
        observations::due_claims(&obligations, "alice")
            .next()
            .unwrap()
            .amount_due,
        Money(725)
    );
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
    fixture.scenario.holding_pools = vec![holding_pool("alice", "brokerage", "bnd", 1_000_000)];
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
    fixture.series.push(SeriesSpec {
        series_id: "security:bnd".into(),
        snapshots: 2,
        values: vec![1, 1],
    });
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
    fixture.scenario.holding_pools = vec![holding_pool("alice", "brokerage", "vti", 1_000_000)];
    fixture.series.push(SeriesSpec {
        series_id: "security:vti".into(),
        snapshots: 2,
        values: vec![100, 100],
    });
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
            holding_pools: vec![holding_pool("alice", "brokerage", "vti", 1_000_000)],
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
            holding_pools: vec![holding_pool("alice", "brokerage", "vti", 1_000_000)],
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
            holding_pools: vec![],
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
            holding_pools: vec![],
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
    fixture.scenario.holding_pools = vec![holding_pool("alice", "brokerage", "stock", 1_000_000)];
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
