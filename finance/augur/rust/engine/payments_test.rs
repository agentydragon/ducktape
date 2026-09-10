//! Payment actions are tested separately from the configured grouped control.

use super::*;
use payments::{Consume, Outcome, PayClaim, Receipt, Rejection, Request, Target};

fn execute(
    input: &ExecutionInput,
    state: &mut RolloutState,
    claims: &mut claims::Claims,
    actor: &str,
    request: &Request,
) -> Receipt {
    payments::Context {
        fixture: input,
        ledger: &mut state.ledger,
        recorder: &mut state.recorder,
        tax: &mut state.tax,
        properties: &state.properties,
        mortgages: &mut state.mortgages,
        tax_liabilities: &mut state.tax_liabilities,
        month: state.month,
    }
    .execute(actor, claims, request)
    .unwrap()
}

#[test]
fn claim_occurrences_are_not_labels_and_consumption_is_not_a_claim() {
    let (mut input, component) = tests::spending_fixture();
    input.scenario.accounts[0].opening_balance = Money(100);
    for amount in [Money(30), Money(40)] {
        input
            .scenario
            .obligations
            .push(crate::execution::ObligationSpec {
                month: 0,
                obligation_id: "same-label".into(),
                obligation_type: "rent".into(),
                from: component.from.clone(),
                to: component.to.clone(),
                amount_due: amount.into(),
                property_id: None,
                deduction_category: None,
                deductible_fraction_ppb: WIRE_RATE_SCALE,
            });
    }
    let mut state = RolloutState::new(&input, 0, CaptureMode::Forensic, None).unwrap();
    let mut claims = claims::assemble(&input, 0, 0, &[], &[], &[]).unwrap();
    let ids: Vec<_> = observations::due_claims(&claims, "alice")
        .map(|claim| claim.id)
        .collect();
    assert_ne!(ids[0], ids[1]);
    let request = Request::PayClaim(PayClaim {
        request_id: 42,
        cause_id: "chosen-payment".into(),
        claim: ids[1],
        from: component.from.clone(),
        amount: Money(40),
    });
    let receipt = execute(&input, &mut state, &mut claims, "alice", &request);
    assert_eq!(receipt.request_id, 42);
    assert_eq!(receipt.target, Target::Claim(ids[1]));
    assert_eq!(receipt.amount_paid(), Money(40));
    assert_eq!(
        observations::due_claims(&claims, "alice")
            .map(|claim| claim.id)
            .collect::<Vec<_>>(),
        [ids[0]]
    );
    let before = state.ledger.clone();
    assert_eq!(
        execute(&input, &mut state, &mut claims, "alice", &request).outcome,
        Outcome::Rejected(Rejection::AlreadyPaid)
    );
    assert_eq!(state.ledger, before);
    let receipt = execute(
        &input,
        &mut state,
        &mut claims,
        "alice",
        &Request::Consume(Consume {
            request_id: 43,
            cause_id: "same-label_m0".into(),
            component_id: "flex-budget".into(),
            from: component.from.clone(),
            to: component.to,
            amount: Money(10),
        }),
    );
    assert_eq!(
        receipt.target,
        Target::Consumption {
            component_id: "flex-budget".into()
        }
    );
    assert_eq!(receipt.amount_paid(), Money(10));
    assert_eq!(claims.entries.len(), 2);
    assert!(!claims.entries[0].paid);
    assert_eq!(state.ledger.balance(&component.from).unwrap(), Money(50));
    assert!(state.recorder.obligations.is_empty());
    assert!(state.recorder.rollout_failures.is_empty());
    assert_eq!(state.recorder.transfers.len(), 2);
}

#[test]
fn rejected_payments_change_neither_books_nor_capture() {
    let (mut input, component) = tests::spending_fixture();
    input.scenario.accounts[0].opening_balance = Money(100);
    let mut state = RolloutState::new(&input, 0, CaptureMode::Forensic, None).unwrap();
    let mut claims = claims::Claims {
        month: 0,
        entries: vec![ActiveObligation {
            paid: false,
            cause_id: "large-claim".into(),
            obligation_type: "rent".into(),
            from: component.from.clone(),
            to: component.to.clone(),
            amount_due: Money(150),
            effect: ObligationEffect::None,
        }],
    };
    let id = observations::due_claims(&claims, "alice")
        .next()
        .unwrap()
        .id;
    let payment = PayClaim {
        request_id: 8,
        cause_id: "pay".into(),
        claim: id,
        from: component.from.clone(),
        amount: Money(150),
    };
    let mut stale = payment.clone();
    stale.claim.month = 1;
    let mut missing = payment.clone();
    missing.claim.index = 1;
    let mut partial = payment.clone();
    partial.amount = Money(50);
    let mut foreign = payment.clone();
    foreign.from = component.to.clone();
    let mut undeclared = payment.clone();
    undeclared.from.account_id = "undeclared".into();
    let mut empty = payment.clone();
    empty.cause_id.clear();
    let before = state.ledger.clone();
    let count = state.recorder.journal_entry_count;
    for (request, reason) in [
        (stale, Rejection::UnknownClaim),
        (missing, Rejection::UnknownClaim),
        (partial, Rejection::InvalidAmount),
        (foreign, Rejection::WrongActor),
        (undeclared, Rejection::UnknownAccount),
        (empty, Rejection::EmptyIdentifier),
        (
            payment,
            Rejection::InsufficientCash {
                available: Money(100),
            },
        ),
    ] {
        let receipt = execute(
            &input,
            &mut state,
            &mut claims,
            "alice",
            &Request::PayClaim(request),
        );
        assert_eq!(receipt.outcome, Outcome::Rejected(reason));
        assert_eq!(receipt.amount_paid(), Money(0));
        assert_eq!(state.ledger, before);
        assert_eq!(state.recorder.journal_entry_count, count);
        assert!(!claims.entries[0].paid);
    }
    for amount in [Money(0), Money(-1), Money(101)] {
        let receipt = execute(
            &input,
            &mut state,
            &mut claims,
            "alice",
            &Request::Consume(Consume {
                request_id: 9,
                cause_id: "consume".into(),
                component_id: "budget".into(),
                from: component.from.clone(),
                to: component.to.clone(),
                amount,
            }),
        );
        assert!(matches!(receipt.outcome, Outcome::Rejected(_)));
        assert_eq!(state.ledger, before);
    }
    claims.entries[0].from = component.to.clone();
    let foreign_claim = Request::PayClaim(PayClaim {
        request_id: 10,
        cause_id: "foreign-claim".into(),
        claim: id,
        from: component.from.clone(),
        amount: Money(150),
    });
    assert_eq!(
        execute(&input, &mut state, &mut claims, "alice", &foreign_claim).outcome,
        Outcome::Rejected(Rejection::WrongActor)
    );
    for (to, component_id, reason) in [
        (component.to.clone(), "", Rejection::EmptyIdentifier),
        (
            AccountRef::new("world", "undeclared"),
            "budget",
            Rejection::UnknownAccount,
        ),
    ] {
        let request = Request::Consume(Consume {
            request_id: 11,
            cause_id: "consume".into(),
            component_id: component_id.into(),
            from: component.from.clone(),
            to,
            amount: Money(1),
        });
        assert_eq!(
            execute(&input, &mut state, &mut claims, "alice", &request).outcome,
            Outcome::Rejected(reason)
        );
        assert_eq!(state.ledger, before);
    }
    assert!(state.recorder.transfers.is_empty());
    assert!(state.recorder.obligations.is_empty());
    assert!(state.recorder.rollout_failures.is_empty());
    assert_eq!(state.failed_month, None); // The primitive does not own the runner's stop flag.
}

#[test]
fn tax_and_mortgage_claims_use_selected_funding_account() {
    let (mut input, _) = tests::stopped_book_fixture(15, 2);
    input.scenario.accounts.push(crate::execution::AccountSpec {
        account: AccountRef::new("alice", "reserve"),
        opening_balance: Money(1_000),
    });
    let output = simulate(&input).unwrap();
    let opening = &output.rollouts[0].months[12];
    let mut state = RolloutState::new(&input, 0, CaptureMode::Forensic, None).unwrap();
    // Restore the observed opening book, including internal liability accounts.
    state
        .ledger
        .apply(&JournalEntry {
            month: 12,
            cause_id: "restore-test-opening".into(),
            postings: opening
                .balances
                .iter()
                .map(|row| Posting {
                    account: row.account.clone(),
                    amount: row
                        .balance
                        .checked_sub(state.ledger.balance(&row.account).unwrap())
                        .unwrap(),
                })
                .collect(),
        })
        .unwrap();
    state.month = 12;
    state.properties = opening.properties.clone();
    state.mortgages = opening.mortgages.clone();
    state.tax_liabilities = opening.tax_liabilities.clone();
    let mut claims = claims::assemble(
        &input,
        0,
        12,
        &state.properties,
        &state.mortgages,
        &state.tax_liabilities,
    )
    .unwrap();
    let requests: Vec<_> = observations::due_claims(&claims, "alice")
        .filter(|claim| claim.obligation_type != "cash_spend")
        .map(|claim| {
            Request::PayClaim(PayClaim {
                request_id: claim.id.index as u64,
                cause_id: format!("selected-{}", claim.cause_id),
                claim: claim.id,
                from: AccountRef::new("alice", "reserve"),
                amount: claim.amount_due,
            })
        })
        .collect();
    assert_eq!(requests.len(), 2);
    for request in &requests {
        assert_eq!(
            execute(&input, &mut state, &mut claims, "alice", request).outcome,
            Outcome::Paid
        );
    }
    assert_eq!(
        state
            .ledger
            .balance(&AccountRef::new("alice", "checking"))
            .unwrap(),
        Money(1_000)
    );
    assert_eq!(
        state
            .ledger
            .balance(&AccountRef::new("alice", "reserve"))
            .unwrap(),
        Money(850)
    );
    assert_eq!(state.mortgages[0].principal, Money(7_800));
    assert_eq!(
        state.recorder.mortgage_payments[0].from_account_id,
        "reserve"
    );
    assert_eq!(state.recorder.tax_payments[0].amount_paid, Money(50));
    assert!(
        state
            .tax_liabilities
            .iter()
            .all(|liability| liability.amount_owed == Money(0))
    );
    assert_eq!(state.recorder.tax_settlements[0].amount, Money(50));
    assert_eq!(state.ledger.trial_balance(), 0);
}

#[test]
fn moving_cash_within_the_actor_is_not_paid_consumption() {
    let (mut input, component) = tests::spending_fixture();
    let reserve = AccountRef::new("alice", "reserve");
    input.scenario.accounts.push(crate::execution::AccountSpec {
        account: reserve.clone(),
        opening_balance: Money(0),
    });
    for to in [component.from.clone(), reserve] {
        let mut state = RolloutState::new(&input, 0, CaptureMode::Forensic, None).unwrap();
        let before = state.ledger.clone();
        let count = state.recorder.journal_entry_count;
        let mut claims = claims::assemble(&input, 0, 0, &[], &[], &[]).unwrap();
        let receipt = execute(
            &input,
            &mut state,
            &mut claims,
            "alice",
            &Request::Consume(Consume {
                request_id: 1,
                cause_id: "own-account".into(),
                component_id: "budget".into(),
                from: component.from.clone(),
                to: to.clone(),
                amount: Money(10),
            }),
        );
        assert_eq!(
            receipt.outcome,
            Outcome::Rejected(Rejection::SameActorRecipient)
        );
        assert_eq!(receipt.amount_paid(), Money(0));
        assert_eq!(state.ledger, before);
        assert_eq!(state.recorder.journal_entry_count, count);
        assert!(state.recorder.transfers.is_empty());
    }
}
