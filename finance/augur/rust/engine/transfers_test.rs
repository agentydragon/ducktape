//! Request admission and rejection atomicity across cash, income and receipts.

use super::*;

#[derive(Debug)]
struct Books {
    input: ExecutionInput,
    ledger: Ledger,
    recorder: Recorder,
    tax: TaxState,
}

impl Books {
    fn new() -> Self {
        let mut input = minimal_fixture();
        input.scenario.accounts = [
            ("alice", "checking", 100),
            ("alice", "savings", 0),
            ("bob", "checking", 0),
            ("world", "cash", 0),
        ]
        .into_iter()
        .map(|(agent, account, balance)| AccountSpec {
            account: AccountRef::new(agent, account),
            opening_balance: Money(balance),
        })
        .collect();
        input
            .scenario
            .income_sources
            .push(IncomeSource::interest(None));
        ValidatedInput::new(&input).unwrap();
        let mut ledger = Ledger::with_accounts(
            input
                .scenario
                .accounts
                .iter()
                .map(|spec| spec.account.clone()),
        );
        let equity = AccountRef::new("alice", OPENING_EQUITY);
        ledger.ensure_account(equity.clone());
        ledger
            .apply(&JournalEntry {
                month: 0,
                cause_id: "opening".into(),
                postings: vec![
                    Posting {
                        account: AccountRef::new("alice", "checking"),
                        amount: Money(100),
                    },
                    Posting {
                        account: equity,
                        amount: Money(-100),
                    },
                ],
            })
            .unwrap();
        let tax = TaxState {
            income: IncomeLedger::for_taxpayers(["alice", "bob"], &input.scenario.income_sources),
            facts: [
                (("alice".into(), "federal".into()), TaxFacts::default()),
                (("bob".into(), "federal".into()), TaxFacts::default()),
            ]
            .into(),
        };
        Self {
            input,
            ledger,
            recorder: Recorder::new(CaptureMode::Forensic),
            tax,
        }
    }

    fn transfer(
        &mut self,
        actor: Option<&str>,
        request: &TransferRequest,
        income: Option<&IncomeSource>,
        deduction: Option<&str>,
    ) -> Result<(), SimulationError> {
        execute_transfer(
            &self.input,
            &mut self.ledger,
            &mut self.recorder,
            &mut self.tax,
            0,
            request,
            TransferContext {
                actor_id: actor,
                income_category: income,
                deduction_category: deduction,
            },
        )
    }

    fn state(&self) -> String {
        format!("{:?}{:?}{:?}", self.ledger, self.tax, self.recorder)
    }
}

fn request() -> TransferRequest {
    TransferRequest {
        cause_id: "move-cash".into(),
        from: AccountRef::new("alice", "checking"),
        to: AccountRef::new("bob", "checking"),
        amount: Money(25),
    }
}

#[test]
fn admitted_actor_transfer_matches_scheduled_accounting_exactly() {
    let mut actor = Books::new();
    actor
        .transfer(Some("alice"), &request(), None, None)
        .unwrap();
    let mut scheduled = Books::new();
    scheduled
        .input
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 0,
            cause_id: "move-cash".into(),
            from: request().from,
            to: request().to,
            amount: Money(25).into(),
            income_category: None,
            deduction_category: None,
        });
    execute_cashflows(
        &scheduled.input,
        0,
        &mut scheduled.ledger,
        &mut scheduled.recorder,
        &mut scheduled.tax,
        &[],
        0,
    )
    .unwrap();
    assert_eq!(actor.state(), scheduled.state());
    assert_eq!(actor.ledger.balance(&request().from).unwrap(), Money(75));
    assert_eq!(actor.ledger.balance(&request().to).unwrap(), Money(25));
    assert_eq!(actor.recorder.transfers[0].cause_id, "move-cash");
    assert_eq!(actor.recorder.transfers[0].amount, Money(25));
    assert_eq!(actor.ledger.trial_balance(), 0);
}

#[test]
fn scheduled_income_can_arrive_from_an_exogenous_negative_balance() {
    let mut books = Books::new();
    let request = TransferRequest {
        from: AccountRef::new("world", "cash"),
        ..request()
    };
    books
        .transfer(None, &request, Some(&IncomeSource::Ordinary), None)
        .unwrap();
    assert_eq!(books.ledger.balance(&request.from).unwrap(), Money(-25));
    assert_eq!(books.tax.income.ordinary("bob"), Money(25));
    assert_eq!(
        books.recorder.transfers[0].income_category.as_deref(),
        Some("ordinary")
    );
}

#[test]
fn actors_cannot_overdraw_or_impersonate_another_source_or_classify_tax() {
    for case in 0..8 {
        let mut books = Books::new();
        let mut request = request();
        match case {
            0 => request.amount = Money(101),
            1 => request.amount = Money(0),
            2 => request.amount = Money(-1),
            3 => request.from = AccountRef::new("world", "cash"),
            4 => request.from.account_id = "missing".into(),
            5 => request.to.account_id = "missing".into(),
            6 => request.cause_id.clear(),
            _ => {}
        }
        let before = books.state();
        let income = if case == 7 {
            Some(&IncomeSource::Ordinary)
        } else {
            None
        };
        assert!(
            books
                .transfer(Some("alice"), &request, income, None)
                .is_err(),
            "case {case}"
        );
        assert_eq!(books.state(), before, "case {case}");
    }
}

#[test]
fn scheduled_tax_and_posting_failures_do_not_partially_apply() {
    for case in 0..5 {
        let mut books = Books::new();
        match case {
            0 => books
                .tax
                .income
                .accrue("bob", &IncomeSource::Ordinary, Money(i64::MAX))
                .unwrap(),
            1 => books
                .tax
                .income
                .accrue("alice", &IncomeSource::Ordinary, Money(i64::MIN))
                .unwrap(),
            2 => books.recorder.journal_entry_count = u64::MAX,
            3 => {
                let counterparty = AccountRef::new("world", "other");
                books.ledger.ensure_account(counterparty.clone());
                books
                    .ledger
                    .apply(&JournalEntry {
                        month: 0,
                        cause_id: "large balance".into(),
                        postings: vec![
                            Posting {
                                account: request().to,
                                amount: Money(i64::MAX),
                            },
                            Posting {
                                account: counterparty,
                                amount: Money(-i64::MAX),
                            },
                        ],
                    })
                    .unwrap();
            }
            _ => {}
        }
        let deduction = if case == 4 { "invalid" } else { "ordinary" };
        let before = books.state();
        assert!(
            books
                .transfer(
                    None,
                    &request(),
                    Some(&IncomeSource::Ordinary),
                    Some(deduction)
                )
                .is_err(),
            "case {case}"
        );
        assert_eq!(books.state(), before, "case {case}");
    }
}

#[test]
fn shared_income_row_is_updated_in_order_without_overwriting_a_prior_change() {
    let mut books = Books::new();
    let request = TransferRequest {
        to: AccountRef::new("alice", "savings"),
        ..request()
    };
    books
        .transfer(
            None,
            &request,
            Some(&IncomeSource::Ordinary),
            Some("ordinary"),
        )
        .unwrap();
    assert_eq!(books.tax.income.ordinary("alice"), Money(0));
    assert_eq!(books.ledger.balance(&request.from).unwrap(), Money(75));
    assert_eq!(books.ledger.balance(&request.to).unwrap(), Money(25));
    assert_eq!(books.ledger.trial_balance(), 0);
}

#[test]
fn transfer_sequence_is_not_an_implicitly_atomic_batch() {
    let mut books = Books::new();
    books
        .transfer(Some("alice"), &request(), None, None)
        .unwrap();
    let after_first = books.state();
    assert!(
        books
            .transfer(
                Some("alice"),
                &TransferRequest {
                    amount: Money(76),
                    ..request()
                },
                None,
                None
            )
            .is_err()
    );
    assert_eq!(books.state(), after_first);
    assert_eq!(books.recorder.transfers.len(), 1);
}
