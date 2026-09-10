//! Explicit posting facts exercise settlement, not a second mortgage servicing model.

use super::*;

fn snapshot(principal: i64) -> MortgageSnapshot {
    MortgageSnapshot {
        liability_id: "test-mortgage".into(),
        property_id: "test-home".into(),
        agent_id: "alice".into(),
        payment_account_id: "checking".into(),
        counterparty_agent_id: "world".into(),
        counterparty_account_id: "checking".into(),
        origination_month: 2,
        annual_interest_rate_ppb: 0,
        term_months: 60,
        monthly_payment: Money(1_000),
        principal: Money(principal),
        interest_paid_ytd: Money(0),
        rental_interest_paid_ytd: Money(0),
        active: principal > 0,
    }
}

#[test]
fn mortgage_postings_use_selected_cash_and_ledger_principal_through_payoff() {
    let mut input = tests::mid_horizon_property_fixture(true, 0);
    input.scenario.accounts.push(crate::execution::AccountSpec {
        account: AccountRef::new("alice", "reserve"),
        opening_balance: Money(3_000),
    });
    input
        .scenario
        .obligations
        .push(crate::execution::ObligationSpec {
            month: 3,
            obligation_id: "ordinary".into(),
            obligation_type: "rent".into(),
            from: AccountRef::new("alice", "checking"),
            to: AccountRef::new("world", "checking"),
            amount_due: Money(2).into(),
            property_id: None,
            deduction_category: None,
            deductible_fraction_ppb: 0,
        });
    input
        .scenario
        .property_tax_policies
        .push(crate::execution::PropertyTaxPolicySpec {
            property_id: "test-home".into(),
            owner_agent_id: "alice".into(),
            from_account_id: "checking".into(),
            tax_authority_agent_id: "world".into(),
            tax_authority_account_id: "checking".into(),
            annual_tax_rate_ppb: Some(12_000_000),
            start_month: 3,
            end_month: None,
        });
    let prepared = world::Prepared::new(input).unwrap();
    let mut world = prepared
        .world(0, vec![], CaptureMode::Forensic, None, None)
        .unwrap();
    assert_eq!(world.mortgage_principal("test-mortgage").unwrap(), Money(0));
    for (month, ending_principal) in [0, 0, 60_000, 59_000, 58_000, 0].into_iter().enumerate() {
        let month = month as u32;
        let originations = if month == 2 {
            vec![mortgages::Origination {
                liability_id: "test-mortgage".into(),
                monthly_payment: Money(1_000),
            }]
        } else {
            vec![]
        };
        let payoffs = if month == 5 {
            vec![mortgages::Payoff {
                liability_id: "test-mortgage".into(),
                principal: Money(58_000),
            }]
        } else {
            vec![]
        };
        let events = world.prepare_month(month, &originations, &payoffs).unwrap();
        assert_eq!(
            events.originated,
            if month == 2 {
                vec!["test-mortgage"]
            } else {
                vec![]
            }
        );
        assert_eq!(
            events.paid_off,
            if month == 5 {
                vec!["test-mortgage"]
            } else {
                vec![]
            }
        );
        let installments = if matches!(month, 3 | 4) {
            vec![mortgages::Installment {
                liability_id: "test-mortgage".into(),
                interest: Money(0),
                principal: Money(1_000),
                rental_interest: Money(0),
            }]
        } else {
            vec![]
        };
        world.assemble_claims(&installments).unwrap();
        if matches!(month, 3 | 4) {
            let scope = world.scope("alice").unwrap();
            let observation = world.observe(&scope);
            let kinds = observation
                .claims()
                .map(|claim| claim.obligation_type)
                .collect::<Vec<_>>();
            assert_eq!(
                kinds,
                if month == 3 {
                    vec!["rent", "mortgage_payment", "property_tax"]
                } else {
                    vec!["mortgage_payment", "property_tax"]
                }
            );
            let claim = observation
                .claims()
                .find(|claim| claim.obligation_type == "mortgage_payment")
                .unwrap()
                .id;
            let checking = world.account_balance("alice", "checking").unwrap();
            assert!(world.paid_mortgages().is_empty());
            let action = actors::Action::PayClaim(payments::PayClaim {
                request_id: 1,
                cause_id: "reserve-payment".into(),
                claim,
                from: AccountRef::new("alice", "reserve"),
                amount: Money(1_000),
            });
            assert!(matches!(
                world.apply("alice", &action, 0).unwrap(),
                actors::Outcome::Executed
            ));
            assert_eq!(world.paid_mortgages(), ["test-mortgage"]);
            assert_eq!(
                world.account_balance("alice", "checking").unwrap(),
                checking
            );
            assert!(!world.settle_claims().unwrap().failed);
        }
        assert_eq!(
            world.mortgage_principal("test-mortgage").unwrap(),
            Money(ending_principal)
        );
        let snapshots = if month >= 2 {
            vec![snapshot(ending_principal)]
        } else {
            vec![]
        };
        let interest = if month >= 2 {
            vec![mortgages::Interest {
                liability_id: "test-mortgage".into(),
                owner_interest_paid_ytd: Money(0),
                origination_principal: Money(60_000),
            }]
        } else {
            vec![]
        };
        world
            .close_month(false, Money(0), &interest, &snapshots)
            .unwrap();
    }
    let financial = world.finish(&[snapshot(0)]).unwrap().financial.unwrap();
    assert_eq!(financial.mortgage_payments.len(), 2);
    assert!(
        financial
            .mortgage_payments
            .iter()
            .all(|event| event.from_account_id == "reserve")
    );
    assert_eq!(financial.property_sales[0].mortgage_payoff, Money(58_000));
    assert_eq!(
        financial.property_sales[0].net_cash_to_owner,
        Money(122_000)
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
fn invalid_mortgage_effects_do_not_change_cash_or_principal() {
    let mut input = tests::mid_horizon_property_fixture(true, 0);
    input.scenario.scheduled_property_purchases[0].month = 0;
    input.scenario.property_sales[0].month = 1;
    let prepared = world::Prepared::new(input).unwrap();
    let mut world = prepared
        .world(0, vec![], CaptureMode::Summary, None, None)
        .unwrap();
    let opening = world.account_balance("alice", "checking").unwrap();
    assert!(world.prepare_month(0, &[], &[]).is_err());
    assert_eq!(world.account_balance("alice", "checking").unwrap(), opening);
    assert_eq!(world.mortgage_principal("test-mortgage").unwrap(), Money(0));
    world
        .prepare_month(
            0,
            &[mortgages::Origination {
                liability_id: "test-mortgage".into(),
                monthly_payment: Money(1_000),
            }],
            &[],
        )
        .unwrap();
    world.assemble_claims(&[]).unwrap();
    let mut book = snapshot(60_000);
    book.origination_month = 0;
    world
        .close_month(
            false,
            Money(0),
            &[mortgages::Interest {
                liability_id: "test-mortgage".into(),
                owner_interest_paid_ytd: Money(0),
                origination_principal: Money(60_000),
            }],
            &[book],
        )
        .unwrap();
    let cash = world.account_balance("alice", "checking").unwrap();
    assert!(
        world
            .prepare_month(
                1,
                &[],
                &[mortgages::Payoff {
                    liability_id: "test-mortgage".into(),
                    principal: Money(60_001)
                }]
            )
            .is_err()
    );
    assert_eq!(world.account_balance("alice", "checking").unwrap(), cash);
    assert_eq!(
        world.mortgage_principal("test-mortgage").unwrap(),
        Money(60_000)
    );
    assert!(
        world
            .assemble_claims(&[mortgages::Installment {
                liability_id: "test-mortgage".into(),
                interest: Money(0),
                principal: Money(60_001),
                rental_interest: Money(0)
            }])
            .is_err()
    );
    assert!(world.paid_mortgages().is_empty());
    assert_eq!(
        world.mortgage_principal("test-mortgage").unwrap(),
        Money(60_000)
    );
}
