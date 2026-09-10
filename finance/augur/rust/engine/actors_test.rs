//! Financial-world controls. Python's real session tests own batch routing and receipts.

use super::*;
use crate::engine::{
    actors::{Action, Outcome},
    trades, transfers,
    world::{Prepared, World},
};

fn input(horizon: u32, paths: u32) -> ExecutionInput {
    let (mut input, _) = policy_timing_fixture(horizon);
    input.rollout_count = paths;
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

fn taxed_input() -> ExecutionInput {
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
    input
}

fn world(input: ExecutionInput, capture: CaptureMode) -> World {
    Prepared::new(input)
        .unwrap()
        .world(0, Vec::new(), capture, Some("alice"), None)
        .unwrap()
}

#[test]
fn cash_only_actor_observes_and_purchases_an_unheld_declared_asset() {
    let mut world = world(cash_only_input(), CaptureMode::Forensic);
    world.prepare_month(0).unwrap();
    let scope = world.scope("alice").unwrap();
    let observation = world.observe(&scope);
    let pools: Vec<_> = observation.books.holding_pools().collect();
    assert_eq!(pools.len(), 1);
    assert_eq!(pools[0].account_id, "empty-brokerage");
    assert_eq!(
        observation.books.public_price("stock").unwrap(),
        PerUnit(1_000)
    );
    assert_eq!(observation.books.public_positions().count(), 0);
    assert!(matches!(
        world
            .apply(
                "alice",
                &Action::Buy(trades::PurchaseRequest {
                    cause_id: "first-purchase".into(),
                    agent_id: "alice".into(),
                    cash_account_id: "checking".into(),
                    holding_account_id: "empty-brokerage".into(),
                    asset_id: "stock".into(),
                    lot_id: "new-position".into(),
                    quantity_scale: 1_000_000,
                    units: Quantity(2_000_000),
                }),
                0
            )
            .unwrap(),
        Outcome::Executed
    ));
    world.close_month(false, Money(0)).unwrap();
    world.prepare_month(1).unwrap();
    let observation = world.observe(&scope);
    assert_eq!(
        observation.books.public_price("stock").unwrap(),
        PerUnit(2_000)
    );
    assert_eq!(observation.books.public_value().unwrap(), Money(4_000));
    assert_eq!(observation.books.cash().unwrap(), Money(500));
    world.close_month(false, Money(0)).unwrap();
    let financial = world.finish().unwrap().financial.unwrap();
    let lot = &financial.months.last().unwrap().lots[0];
    assert_eq!(lot.units_remaining, Quantity(2_000_000));
    assert_eq!(lot.basis_remaining, Money(2_000));
    assert_eq!(lot.account_id, "empty-brokerage");
    assert!(financial.journal.iter().all(|entry| {
        entry
            .postings
            .iter()
            .map(|posting| i128::from(posting.amount.0))
            .sum::<i128>()
            == 0
    }));
}

#[test]
fn declaring_an_empty_pool_does_not_invest_cash_without_an_action() {
    let mut world = world(cash_only_input(), CaptureMode::Forensic);
    for month in 0..2 {
        world.prepare_month(month).unwrap();
        world.close_month(false, Money(0)).unwrap();
    }
    let summary = world.finish().unwrap().summary.unwrap();
    assert!(summary.ending_book.lots.is_empty());
    assert_eq!(summary.cash[0].values, [Money(2_500); 3]);
}

#[test]
fn an_empty_pool_purchase_rejects_wrong_account_or_scale_without_mutation() {
    for wrong_scale in [false, true] {
        let mut world = world(cash_only_input(), CaptureMode::Forensic);
        world.prepare_month(0).unwrap();
        let rejected = world
            .apply(
                "alice",
                &Action::Buy(trades::PurchaseRequest {
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
                }),
                0,
            )
            .unwrap();
        assert!(matches!(rejected, Outcome::Rejected(_)));
        assert_eq!(
            world.account_balance("alice", "checking").unwrap(),
            Some(Money(2_500))
        );
        world.close_month(true, Money(0)).unwrap();
        let financial = world.finish().unwrap().financial.unwrap();
        assert!(financial.months.last().unwrap().lots.is_empty());
        assert!(
            financial
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

#[test]
fn cashflows_claims_sales_and_cross_year_tax_share_financial_books() {
    let mut world = world(taxed_input(), CaptureMode::Forensic);
    let scope = world.scope("alice").unwrap();
    for month in 0..13 {
        world.prepare_month(month).unwrap();
        let observation = world.observe(&scope);
        assert_eq!(
            observation.books.cash().unwrap(),
            Money(if month == 0 { 20_000 } else { 0 })
        );
        let claims: Vec<_> = observation
            .claims()
            .map(|claim| {
                assert_eq!(claim.due_month, month);
                assert_eq!(
                    claim.amount_due,
                    Money(if month == 0 { 50_000 } else { 1_500 })
                );
                Action::PayClaim(payments::PayClaim {
                    request_id: 7,
                    cause_id: format!("pay-{}", claim.cause_id),
                    claim: claim.id,
                    from: claim.from.clone(),
                    amount: claim.amount_due,
                })
            })
            .collect();
        let units = match month {
            0 => 30_000_000,
            12 => 1_500_000,
            _ => 0,
        };
        if units != 0 {
            assert!(matches!(
                world.apply("alice", &sell(units), 0).unwrap(),
                Outcome::Executed
            ));
        }
        for (index, claim) in claims.iter().enumerate() {
            assert!(matches!(
                world.apply("alice", claim, index + 1).unwrap(),
                Outcome::Executed
            ));
        }
        assert!(world.unpaid_claims("alice").is_empty());
        world.close_month(false, Money(0)).unwrap();
    }
    let output = world.finish().unwrap();
    let financial = output.financial.unwrap();
    assert_eq!(financial.months.len(), 14);
    assert_eq!(financial.dispositions[0].proceeds, Money(30_000));
    assert_eq!(financial.dispositions[0].basis, Money(15_000));
    assert_eq!(financial.dispositions[0].realized_gain, Money(15_000));
    assert_eq!(financial.tax_payments[0].amount_paid, Money(1_500));
    assert_eq!(
        financial
            .obligations
            .iter()
            .map(|row| row.amount_paid)
            .collect::<Vec<_>>(),
        [Money(50_000), Money(1_500)]
    );
    assert!(financial.journal.iter().all(|entry| {
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
    let mut world = world(input(2, 1), CaptureMode::Forensic);
    world.prepare_month(0).unwrap();
    for (index, action) in [
        sell(20_000_000),
        buy("first-buy", "checking", 10_000_000),
        transfer(15_000),
        buy("second-buy", "reserve", 15_000_000),
        consume(5_000),
    ]
    .iter()
    .enumerate()
    {
        assert!(matches!(
            world.apply("alice", action, index).unwrap(),
            Outcome::Executed
        ));
    }
    world.close_month(false, Money(0)).unwrap();
    world.prepare_month(1).unwrap();
    let scope = world.scope("alice").unwrap();
    let observation = world.observe(&scope);
    assert_eq!(observation.books.cash().unwrap(), Money(0));
    assert_eq!(observation.books.public_value().unwrap(), Money(105_000));
    world.close_month(false, Money(0)).unwrap();
    let financial = world.finish().unwrap().financial.unwrap();
    let chosen = [
        "chosen-sale",
        "first-buy",
        "move-cash",
        "second-buy",
        "chosen-consumption",
    ];
    assert_eq!(
        financial
            .journal
            .iter()
            .filter(|entry| chosen.contains(&entry.cause_id.as_str()))
            .map(|entry| entry.cause_id.as_str())
            .collect::<Vec<_>>(),
        chosen
    );
}

#[test]
fn rejected_financial_request_preserves_prior_sale_and_independent_world() {
    let prepared = Prepared::new(input(3, 2)).unwrap();
    let mut failed = prepared
        .world(0, Vec::new(), CaptureMode::Forensic, Some("alice"), None)
        .unwrap();
    let mut live = prepared
        .world(1, Vec::new(), CaptureMode::Forensic, Some("alice"), None)
        .unwrap();
    failed.prepare_month(0).unwrap();
    assert!(matches!(
        failed.apply("alice", &sell(10_000_000), 0).unwrap(),
        Outcome::Executed
    ));
    assert!(matches!(
        failed
            .apply("alice", &buy("impossible", "checking", 1_000_000_000), 1)
            .unwrap(),
        Outcome::Rejected(_)
    ));
    failed.close_month(true, Money(0)).unwrap();
    for month in 0..3 {
        live.prepare_month(month).unwrap();
        assert!(matches!(
            live.apply("alice", &consume(1_000), 0).unwrap(),
            Outcome::Executed
        ));
        live.close_month(false, Money(0)).unwrap();
    }
    let failed = failed.finish().unwrap().financial.unwrap();
    let live = live.finish().unwrap().financial.unwrap();
    assert_eq!(failed.dispositions.len(), 1);
    assert!(failed.transfers.is_empty());
    assert_eq!(failed.months.len(), 2);
    assert_eq!(
        failed.months[1].lots[0].units_remaining,
        Quantity(90_000_000)
    );
    assert_eq!(failed.months[1].lots[0].basis_remaining, Money(45_000));
    assert_eq!(live.months.len(), 4);
    assert_eq!(live.obligations.len(), 3);
}

#[test]
fn payment_capture_names_the_actual_selected_source() {
    let mut input = input(1, 1);
    add_bill(&mut input, 5_000);
    let mut world = world(input, CaptureMode::Forensic);
    world.prepare_month(0).unwrap();
    let scope = world.scope("alice").unwrap();
    let claim = world.observe(&scope).claims().next().unwrap().id;
    assert!(matches!(
        world.apply("alice", &transfer(5_000), 0).unwrap(),
        Outcome::Executed
    ));
    assert!(matches!(
        world
            .apply(
                "alice",
                &Action::PayClaim(payments::PayClaim {
                    request_id: 11,
                    cause_id: "pay-from-reserve".into(),
                    claim,
                    from: AccountRef::new("alice", "reserve"),
                    amount: Money(5_000),
                }),
                1
            )
            .unwrap(),
        Outcome::Executed
    ));
    world.close_month(false, Money(0)).unwrap();
    let output = world.finish().unwrap();
    let financial = output.financial.unwrap();
    assert_eq!(financial.obligations.len(), 1);
    assert_eq!(financial.obligations[0].from.account_id, "reserve");
    assert_eq!(financial.obligations[0].amount_paid, Money(5_000));
    assert_eq!(
        output.summary.unwrap().payments[0].from.account_id,
        "reserve"
    );
}

#[test]
fn compact_capture_replays_observed_prefixes_and_canonical_payment_identity() {
    let mut input = input(3, 1);
    input
        .series
        .iter_mut()
        .find(|series| series.series_id == "security:stock")
        .unwrap()
        .values = vec![1_000, 2_000, 9_000, 10_000];
    let mut outputs = Vec::new();
    for capture in [
        CaptureMode::Summary,
        CaptureMode::Dense,
        CaptureMode::Forensic,
    ] {
        let mut world = world(input.clone(), capture);
        for month in 0..2 {
            world.prepare_month(month).unwrap();
            assert!(matches!(
                world.apply("alice", &sell(1_000_000), 0).unwrap(),
                Outcome::Executed
            ));
            let outcome = world
                .apply(
                    "alice",
                    &consume(if month == 1 { 1_000_000 } else { 1_000 }),
                    1,
                )
                .unwrap();
            assert_eq!(matches!(outcome, Outcome::Rejected(_)), month == 1);
            world.close_month(month == 1, Money(0)).unwrap();
        }
        outputs.push(world.finish().unwrap());
    }
    let compact = outputs[0].summary.as_ref().unwrap();
    assert!(outputs[0].financial.is_none());
    for output in &outputs[1..] {
        assert_eq!(
            serde_json::to_value(compact).unwrap(),
            serde_json::to_value(output.summary.as_ref().unwrap()).unwrap()
        );
        let financial = output.financial.as_ref().unwrap();
        assert_eq!(&compact.ending_book, financial.months.last().unwrap());
        for payment in &compact.payments {
            assert_eq!(payment.action_index, 1);
            assert_eq!(payment.receipt.request_id, 3);
            assert_eq!(payment.from, AccountRef::new("alice", "checking"));
            assert_eq!(
                payment.target.as_ref().unwrap().to,
                AccountRef::new("world", "checking")
            );
            assert_eq!(
                payment.receipt.target,
                payments::Target::Consumption {
                    component_id: "flex-budget".into()
                }
            );
            let paid = financial
                .obligations
                .iter()
                .filter(|row| row.month == payment.month)
                .map(|row| row.amount_paid.0)
                .sum();
            assert_eq!(payment.receipt.amount_paid(), Money(paid));
        }
    }
    assert_eq!(compact.ending_book.month, 2);
    assert_eq!(compact.ending_mark_month, 1);
    assert_eq!(
        compact.public_holdings[0].values,
        [Money(100_000), Money(198_000), Money(196_000)]
    );
    assert!(compact.cash.iter().all(|cash| cash.values.len() == 3));
    assert_eq!(
        compact.ending_book.lots[0].units_remaining,
        Quantity(98_000_000)
    );
    assert_eq!(compact.payments[1].receipt.amount_paid(), Money(0));
    assert_eq!(compact.cash[0].values.last(), Some(&Money(12_000)));
}

#[test]
fn unpaid_claims_keep_occurrence_and_source_without_hidden_sales() {
    let mut input = input(3, 1);
    add_bill(&mut input, 5_000);
    add_bill(&mut input, 7_000);
    for capture in [CaptureMode::Summary, CaptureMode::Forensic] {
        let mut world = world(input.clone(), capture);
        world.prepare_month(0).unwrap();
        let unpaid = world.unpaid_claims("alice");
        assert_eq!(unpaid.len(), 2);
        assert_ne!(unpaid[0].id, unpaid[1].id);
        world.close_month(true, Money(0)).unwrap();
        let output = world.finish().unwrap();
        let summary = output.summary.unwrap();
        let claims = &summary.unpaid_claims;
        assert_ne!(claims[0].id, claims[1].id);
        assert_eq!(claims[0].cause_id, claims[1].cause_id);
        assert_eq!(claims[0].from, AccountRef::new("alice", "checking"));
        assert_eq!(claims[0].to, AccountRef::new("world", "checking"));
        assert_eq!(
            (claims[0].amount_due, claims[1].amount_due),
            (Money(5_000), Money(7_000))
        );
        assert!(summary.payments.is_empty());
        assert_eq!(summary.cash[0].values, [Money(10_000), Money(10_000)]);
        if let Some(financial) = output.financial {
            assert!(financial.dispositions.is_empty());
            assert!(financial.transfers.is_empty());
            assert!(
                financial
                    .obligations
                    .iter()
                    .all(|row| row.amount_paid == Money(0))
            );
        }
    }
}
