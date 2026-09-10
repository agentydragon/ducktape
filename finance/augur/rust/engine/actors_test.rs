//! Deterministic action controls, not market forecasts or statutory tax examples.

use super::*;
use crate::engine::actors::{self, Action, Observation, Outcome, Stop};
use crate::engine::{trades, transfers};

/// Scalar-style story rules use an experiment-side adapter over the one batch API.
fn run<Make, Policy>(
    input: &ExecutionInput,
    actor: &str,
    ids: &[u32],
    make: Make,
) -> Result<Vec<actors::Rollout>, SimulationError>
where
    Make: Fn(u32) -> Policy,
    Policy: FnMut(Observation) -> Result<Vec<Action>, SimulationError>,
{
    let mut policies: BTreeMap<_, _> = ids.iter().map(|&id| (id, make(id))).collect();
    actors::simulate(input, actor, ids, |batch| {
        batch
            .into_iter()
            .rev()
            .map(|decision| {
                let month = decision.observation.books.month();
                let actions =
                    policies.get_mut(&decision.rollout_id).unwrap()(decision.observation)?;
                Ok(actors::DecisionActions {
                    rollout_id: decision.rollout_id,
                    month,
                    actions,
                })
            })
            .collect()
    })
}

fn input(horizon: u32, paths: u32) -> ExecutionInput {
    let (mut input, _) = policy_timing_fixture(horizon);
    input.rollout_count = paths;
    input.scenario.target_allocation_policies.clear();
    for series in &mut input.series {
        series.values = series.values.repeat(paths as usize);
    }
    input.scenario.accounts.push(AccountSpec {
        account: AccountRef::new("alice", "reserve"),
        opening_balance: Money(0),
    });
    input
}

fn sell(units: i64) -> Action {
    Action::Sell(trades::SaleRequest {
        cause_id: "chosen-sale".into(),
        agent_id: "alice".into(),
        proceeds_account_id: "checking".into(),
        asset_id: "stock".into(),
        lots: vec![trades::LotSale {
            account_id: "checking".into(),
            lot_id: "timing-stock".into(),
            units: Quantity(units),
        }],
    })
}

fn buy(id: &str, cash: &str, units: i64) -> Action {
    Action::Buy(trades::PurchaseRequest {
        cause_id: id.into(),
        agent_id: "alice".into(),
        cash_account_id: cash.into(),
        holding_account_id: "checking".into(),
        asset_id: "stock".into(),
        lot_id: id.into(),
        quantity_scale: 1_000_000,
        units: Quantity(units),
    })
}

fn cash_only_input() -> ExecutionInput {
    let mut input = input(2, 1);
    input.scenario.initial_lots.clear();
    input.scenario.accounts[0].opening_balance = Money(2_500);
    input.scenario.holding_pools[0].account_id = "empty-brokerage".into();
    input
        .scenario
        .holding_pools
        .push(holding_pool("world", "other-brokerage", "stock", 1_000_000));
    input.series[1].values = vec![1_000, 2_000, 3_000];
    input
}

#[test]
fn cash_only_actor_observes_and_purchases_an_unheld_declared_asset() {
    let input = cash_only_input();
    let output = run(&input, "alice", &[0], |_| {
        |observation| {
            let pools: Vec<_> = observation.books.holding_pools().collect();
            assert_eq!(pools.len(), 1);
            assert_eq!(pools[0].account_id, "empty-brokerage");
            let price = observation.books.public_price(&pools[0].asset_id)?;
            if observation.books.month() == 0 {
                assert_eq!(price, PerUnit(1_000));
                assert_eq!(observation.books.public_positions().count(), 0);
                Ok(vec![Action::Buy(trades::PurchaseRequest {
                    cause_id: "first-purchase".into(),
                    agent_id: observation.books.agent_id().into(),
                    cash_account_id: "checking".into(),
                    holding_account_id: pools[0].account_id.clone(),
                    asset_id: pools[0].asset_id.clone(),
                    lot_id: "new-position".into(),
                    quantity_scale: pools[0].quantity_scale,
                    units: Quantity(2_000_000),
                })])
            } else {
                assert_eq!(price, PerUnit(2_000));
                assert_eq!(observation.books.public_value()?, Money(4_000));
                assert_eq!(observation.books.cash()?, Money(500));
                Ok(vec![])
            }
        }
    })
    .unwrap();
    let rollout = &output[0];
    assert!(rollout.stop.is_none());
    assert_eq!(rollout.receipts.len(), 1);
    let lot = &rollout.financial.months.last().unwrap().lots[0];
    assert_eq!(lot.units_remaining, Quantity(2_000_000));
    assert_eq!(lot.basis_remaining, Money(2_000));
    assert_eq!(lot.account_id, "empty-brokerage");
    for journal in &rollout.financial.journal {
        assert_eq!(
            journal
                .postings
                .iter()
                .map(|posting| posting.amount.0)
                .sum::<i64>(),
            0
        );
    }
}

#[test]
fn declaring_an_empty_pool_does_not_invest_cash_without_an_action() {
    let input = cash_only_input();
    let output = run(&input, "alice", &[0], |_| |_| Ok(vec![])).unwrap();
    assert!(output[0].receipts.is_empty());
    assert!(output[0].stop.is_none());
    let ending = output[0].financial.months.last().unwrap();
    assert!(ending.lots.is_empty());
    assert_eq!(
        ending
            .balances
            .iter()
            .find(|balance| balance.account == AccountRef::new("alice", "checking"))
            .unwrap()
            .balance,
        Money(2_500)
    );
}

#[test]
fn an_empty_pool_purchase_rejects_wrong_account_or_scale_without_mutation() {
    for wrong_scale in [false, true] {
        let input = cash_only_input();
        let output = run(&input, "alice", &[0], |_| {
            move |_| {
                Ok(vec![Action::Buy(trades::PurchaseRequest {
                    cause_id: "invalid-purchase".into(),
                    agent_id: "alice".into(),
                    cash_account_id: "checking".into(),
                    holding_account_id: if wrong_scale {
                        "empty-brokerage"
                    } else {
                        "other-brokerage"
                    }
                    .into(),
                    asset_id: "stock".into(),
                    lot_id: "must-not-exist".into(),
                    quantity_scale: if wrong_scale { 10 } else { 1_000_000 },
                    units: Quantity(1),
                })])
            }
        })
        .unwrap();
        assert!(matches!(
            output[0].stop,
            Some(Stop::RejectedAction {
                month: 0,
                action_index: 0
            })
        ));
        assert!(output[0].financial.months.last().unwrap().lots.is_empty());
        assert!(
            output[0]
                .financial
                .journal
                .iter()
                .all(|entry| entry.cause_id != "invalid-purchase")
        );
    }
}

#[test]
fn declarations_reject_missing_prices_and_do_not_fall_back_to_initial_lots() {
    let mut input = input(1, 1);
    input.scenario.holding_pools.clear();
    assert!(matches!(
        ValidatedInput::new(&input),
        Err(SimulationError::InvalidHoldingPool { .. })
    ));
    input.scenario.initial_lots.clear();
    input.scenario.holding_pools = vec![holding_pool("alice", "empty", "unpriced", 1_000_000)];
    assert!(matches!(
        ValidatedInput::new(&input),
        Err(SimulationError::MissingSeries { .. })
    ));
    input.scenario.holding_pools[0].asset_id = "stock".into();
    input
        .scenario
        .holding_pools
        .push(input.scenario.holding_pools[0].clone());
    assert!(matches!(
        ValidatedInput::new(&input),
        Err(SimulationError::InvalidHoldingPool { .. })
    ));
}

fn transfer(amount: i64) -> Action {
    Action::Transfer(transfers::TransferRequest {
        cause_id: "move-cash".into(),
        from: AccountRef::new("alice", "checking"),
        to: AccountRef::new("alice", "reserve"),
        amount: Money(amount),
    })
}

fn consume(amount: i64) -> Action {
    Action::Consume(payments::Consume {
        request_id: 3,
        cause_id: "chosen-consumption".into(),
        component_id: "flex-budget".into(),
        from: AccountRef::new("alice", "checking"),
        to: AccountRef::new("world", "checking"),
        amount: Money(amount),
    })
}

fn add_bill(input: &mut ExecutionInput, amount: i64) {
    input.scenario.obligations.push(ObligationSpec {
        month: 0,
        obligation_id: "bill".into(),
        obligation_type: "rent".into(),
        from: AccountRef::new("alice", "checking"),
        to: AccountRef::new("world", "checking"),
        amount_due: Money(amount).into(),
        property_id: None,
        deduction_category: None,
        deductible_fraction_ppb: WIRE_RATE_SCALE,
    });
}

#[test]
fn cashflows_and_claims_precede_one_call_and_taxes_follow_canonical_sales() {
    let mut input = input(13, 1);
    input.scenario.accounts[0].opening_balance = Money(0);
    add_bill(&mut input, 50_000);
    input
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 0,
            cause_id: "current-contribution".into(),
            from: AccountRef::new("world", "checking"),
            to: AccountRef::new("alice", "checking"),
            amount: Money(20_000).into(),
            income_category: None,
            deduction_category: None,
        });
    input.scenario.tax_profiles.push(TaxProfileSpec {
        agent_id: "alice".into(),
        tax_authority_agent_id: "world".into(),
        payment_account_id: "checking".into(),
        tax_authority_account_id: "checking".into(),
        prior_year_tax: Money(0),
        section_121_exclusion: Money(0),
        jurisdictions: vec![TaxRules {
            jurisdiction_id: "synthetic-flat".into(),
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
    let results = run(&input, "alice", &[0], |_| {
        let mut calls = 0;
        move |observation: Observation| {
            assert_eq!(observation.books.month(), calls);
            calls += 1;
            let month = observation.books.month();
            assert_eq!(
                observation.books.cash().unwrap(),
                Money(if month == 0 { 20_000 } else { 0 })
            );
            assert_eq!(
                observation.previous_receipts.len(),
                if month == 1 { 2 } else { 0 }
            );
            let mut actions = match month {
                0 => vec![sell(30_000_000)],
                12 => vec![sell(1_500_000)],
                _ => Vec::new(),
            };
            for claim in observation.claims() {
                assert_eq!(claim.due_month, month);
                assert_eq!(
                    claim.amount_due,
                    Money(if month == 0 { 50_000 } else { 1_500 })
                );
                actions.push(Action::PayClaim(payments::PayClaim {
                    request_id: 7,
                    cause_id: format!("pay-{}", claim.cause_id),
                    claim: claim.id,
                    from: claim.from.clone(),
                    amount: claim.amount_due,
                }));
            }
            Ok(actions)
        }
    })
    .unwrap();
    let result = &results[0];
    assert!(result.stop.is_none());
    assert_eq!(result.receipts.len(), 4);
    assert_eq!(result.financial.months.len(), 14);
    assert_eq!(result.financial.dispositions[0].proceeds, Money(30_000));
    assert_eq!(result.financial.dispositions[0].basis, Money(15_000));
    assert_eq!(
        result.financial.dispositions[0].realized_gain,
        Money(15_000)
    );
    assert_eq!(result.financial.tax_payments[0].amount_paid, Money(1_500));
    assert_eq!(
        result
            .financial
            .obligations
            .iter()
            .map(|row| row.amount_paid)
            .collect::<Vec<_>>(),
        [Money(50_000), Money(1_500)]
    );
    assert!(result.financial.journal.iter().all(|entry| {
        entry
            .postings
            .iter()
            .map(|posting| i128::from(posting.amount.0))
            .sum::<i128>()
            == 0
    }));
}

#[test]
fn ordered_actions_can_buy_before_transferring_and_buy_again() {
    let input = input(2, 1);
    let result = run(&input, "alice", &[0], |_| {
        |observation: Observation| {
            if observation.books.month() == 0 {
                Ok(vec![
                    sell(20_000_000),
                    buy("first-buy", "checking", 10_000_000),
                    transfer(15_000),
                    buy("second-buy", "reserve", 15_000_000),
                    consume(5_000),
                ])
            } else {
                assert_eq!(observation.previous_receipts.len(), 5);
                assert!(
                    observation
                        .previous_receipts
                        .iter()
                        .all(|receipt| matches!(receipt.outcome, Outcome::Executed))
                );
                assert_eq!(observation.books.cash().unwrap(), Money(0));
                assert_eq!(observation.books.public_value().unwrap(), Money(105_000));
                Ok(Vec::new())
            }
        }
    })
    .unwrap();
    assert!(result[0].stop.is_none());
    let causes: Vec<_> = result[0]
        .financial
        .journal
        .iter()
        .filter(|entry| entry.month == 0)
        .map(|entry| entry.cause_id.as_str())
        .collect();
    let selected: Vec<_> = causes
        .into_iter()
        .filter(|id| {
            [
                "chosen-sale",
                "first-buy",
                "move-cash",
                "second-buy",
                "chosen-consumption",
            ]
            .contains(id)
        })
        .collect();
    assert_eq!(
        selected,
        [
            "chosen-sale",
            "first-buy",
            "move-cash",
            "second-buy",
            "chosen-consumption"
        ]
    );
}

#[test]
fn failed_action_preserves_successful_prefix_and_independent_paths_continue() {
    let input = input(3, 2);
    let make = |id| {
        let mut calls = 0;
        move |observation: Observation| {
            assert_eq!(observation.books.month(), calls);
            calls += 1;
            if id == 0 {
                assert_eq!(calls, 1, "failed path must never be called again");
                Ok(vec![
                    sell(10_000_000),
                    buy("impossible", "checking", 1_000_000_000),
                    transfer(1),
                    consume(1),
                ])
            } else {
                Ok(vec![consume(1_000)])
            }
        }
    };
    let results = run(&input, "alice", &[1, 0], make).unwrap();
    assert!(results[0].stop.is_none());
    assert_eq!(results[0].financial.months.len(), 4);
    assert_eq!(results[0].receipts.len(), 3);
    assert!(matches!(
        results[1].stop,
        Some(Stop::RejectedAction {
            month: 0,
            action_index: 1
        })
    ));
    assert_eq!(results[1].receipts.len(), 2);
    assert_eq!(results[1].financial.dispositions.len(), 1);
    assert!(results[1].financial.transfers.is_empty());
    assert_eq!(results[1].financial.months.len(), 2);
    assert_eq!(results[1].financial.months[1].lots.len(), 1);
    assert_eq!(
        results[1].financial.months[1].lots[0].units_remaining,
        Quantity(90_000_000)
    );
    assert_eq!(
        results[1].financial.months[1].lots[0].basis_remaining,
        Money(45_000)
    );
    for result in results {
        let replay = run(&input, "alice", &[result.financial.rollout_id], make).unwrap();
        assert_eq!(
            serde_json::to_value(&replay[0]).unwrap(),
            serde_json::to_value(result).unwrap()
        );
    }
}

#[test]
fn ignoring_claims_stops_without_any_hidden_sale_or_payment() {
    let mut input = input(3, 1);
    add_bill(&mut input, 20_000);
    let result = run(&input, "alice", &[0], |_| {
        |observation: Observation| {
            assert_eq!(observation.books.month(), 0);
            assert_eq!(observation.claims().count(), 1);
            Ok(Vec::new())
        }
    })
    .unwrap();
    assert!(
        matches!(&result[0].stop, Some(Stop::UnpaidClaims { month: 0, claims }) if claims.len() == 1)
    );
    assert!(result[0].receipts.is_empty());
    assert!(result[0].financial.dispositions.is_empty());
    assert!(result[0].financial.transfers.is_empty());
    assert_eq!(result[0].financial.obligations[0].amount_paid, Money(0));
    assert_eq!(result[0].financial.months.len(), 2);
}

#[test]
fn input_overlap_is_rejected_before_policy_not_silently_ignored() {
    let (input, _) = policy_timing_fixture(1);
    assert!(matches!(
        run(&input, "alice", &[0], |_| |_| panic!(
            "unsupported configured allocation"
        )),
        Err(SimulationError::UnsupportedActorInput { .. })
    ));
}

#[test]
fn payment_capture_names_the_actual_selected_source() {
    let mut input = input(1, 1);
    add_bill(&mut input, 5_000);
    let result = run(&input, "alice", &[0], |_| {
        |observation: Observation| {
            let claim = observation.claims().next().unwrap();
            Ok(vec![
                transfer(5_000),
                Action::PayClaim(payments::PayClaim {
                    request_id: 11,
                    cause_id: "pay-from-reserve".into(),
                    claim: claim.id,
                    from: AccountRef::new("alice", "reserve"),
                    amount: claim.amount_due,
                }),
            ])
        }
    })
    .unwrap();
    assert!(result[0].stop.is_none());
    assert_eq!(result[0].financial.obligations.len(), 1);
    assert_eq!(
        result[0].financial.obligations[0].from.account_id,
        "reserve"
    );
    assert_eq!(result[0].financial.obligations[0].amount_paid, Money(5_000));
}

#[test]
fn batch_native_memory_and_scalar_adaptation_share_routing_and_stops() {
    let input = input(3, 3);
    let mut calls = BTreeMap::<u32, u32>::new();
    let mut sizes = Vec::new();
    let batch = actors::simulate(&input, "alice", &[2, 0, 1], |decisions| {
        sizes.push(decisions.len());
        Ok(decisions
            .into_iter()
            .rev()
            .map(|decision| {
                let month = decision.observation.books.month();
                let count = calls.entry(decision.rollout_id).or_default();
                assert_eq!(month, *count);
                *count += 1;
                let fail = (decision.rollout_id == 0 && month == 0)
                    || (decision.rollout_id == 2 && month == 1);
                actors::DecisionActions {
                    rollout_id: decision.rollout_id,
                    month,
                    actions: vec![consume(if fail { 1_000_000 } else { 1_000 })],
                }
            })
            .collect())
    })
    .unwrap();
    assert_eq!(sizes, [3, 2, 1]);
    assert_eq!(calls, BTreeMap::from([(0, 1), (1, 3), (2, 2)]));
    for result in batch {
        let id = result.financial.rollout_id;
        let adapted = run(&input, "alice", &[id], |id| {
            move |observation: Observation| {
                let month = observation.books.month();
                let fail = (id == 0 && month == 0) || (id == 2 && month == 1);
                Ok(vec![consume(if fail { 1_000_000 } else { 1_000 })])
            }
        })
        .unwrap();
        assert_eq!(
            serde_json::to_value(result).unwrap(),
            serde_json::to_value(&adapted[0]).unwrap()
        );
    }
}

#[test]
fn malformed_batch_keys_are_simulator_errors_not_action_failures() {
    let input = input(2, 2);
    for keys in [
        vec![(0, 0)],
        vec![(0, 0), (0, 0)],
        vec![(0, 1), (1, 0)],
        vec![(0, 0), (2, 0)],
    ] {
        let mut calls = 0;
        let result = actors::simulate(&input, "alice", &[0, 1], |decisions| {
            calls += 1;
            assert_eq!(decisions.len(), 2);
            Ok(keys
                .iter()
                .map(|&(rollout_id, month)| actors::DecisionActions {
                    rollout_id,
                    month,
                    actions: Vec::new(),
                })
                .collect())
        });
        assert!(matches!(
            result,
            Err(SimulationError::InvalidActorResponses)
        ));
        assert_eq!(calls, 1);
    }
}
