//! Exact requests exercise the same accounting operations as configured callers.

use super::*;

#[derive(Debug)]
struct Books {
    input: ExecutionInput,
    ledger: Ledger,
    recorder: Recorder,
    lots: Vec<LotState>,
    tax: TaxState,
    deferred: Vec<Money>,
}

impl Books {
    fn new() -> Self {
        let mut input = minimal_fixture();
        input.scenario.accounts.push(AccountSpec {
            account: AccountRef::new("alice", "brokerage"),
            opening_balance: Money(0),
        });
        input.scenario.initial_lots = [(-12, "old", 17), (0, "new", 32)]
            .into_iter()
            .map(|(month, id, basis)| InitialLotSpec {
                lot_id: id.into(),
                agent_id: "alice".into(),
                account_id: "brokerage".into(),
                asset_id: "fund".into(),
                purchase_month: month,
                quantity_scale: 10,
                units: Quantity(10),
                basis: Money(basis),
            })
            .collect();
        input.series.push(SeriesSpec {
            series_id: "security:fund".into(),
            snapshots: 2,
            values: vec![10, 10],
        });
        input.scenario.harvest_policies.push(HarvestPolicySpec {
            owner_agent_id: "alice".into(),
            account_id: "brokerage".into(),
            asset_id: "fund".into(),
            peak_annual_yield_ppb: WIRE_RATE_SCALE / 100,
            floor_annual_yield_ppb: 0,
            maturity_decay_exponent_ppb: WIRE_RATE_SCALE,
            drawdown_sensitivity_ppb: 0,
            short_term_fraction_ppb: WIRE_RATE_SCALE,
        });
        ValidatedInput::new(&input).unwrap();
        let lots = input
            .scenario
            .initial_lots
            .iter()
            .map(|spec| LotState {
                spec: spec.clone(),
                units_remaining: spec.units,
                basis_remaining: spec.basis,
            })
            .collect();
        let cash = AccountRef::new("alice", "checking");
        let basis = asset_basis_account(&input.scenario.initial_lots[0]);
        let equity = AccountRef::new("alice", OPENING_EQUITY);
        let mut ledger = Ledger::with_accounts([
            cash.clone(),
            AccountRef::new("alice", "brokerage"),
            basis.clone(),
            equity.clone(),
            realized_gain_account("alice"),
        ]);
        ledger
            .apply(&JournalEntry {
                month: 0,
                cause_id: "opening".into(),
                postings: vec![
                    Posting {
                        account: cash,
                        amount: Money(100),
                    },
                    Posting {
                        account: basis,
                        amount: Money(49),
                    },
                    Posting {
                        account: equity,
                        amount: Money(-149),
                    },
                ],
            })
            .unwrap();
        let tax = TaxState {
            facts: [
                (("alice".into(), "a".into()), TaxFacts::default()),
                (("alice".into(), "b".into()), TaxFacts::default()),
            ]
            .into(),
            ..TaxState::default()
        };
        Self {
            input,
            ledger,
            recorder: Recorder::new(CaptureMode::Forensic),
            lots,
            tax,
            deferred: vec![Money(0)],
        }
    }

    fn sell(&mut self, request: &SaleRequest, price: i64) -> Result<(), SimulationError> {
        execute_lot_sale(
            &self.input,
            &mut self.ledger,
            &mut self.recorder,
            &mut self.lots,
            &mut self.tax,
            SaleTlh::Pool(&mut self.deferred),
            0,
            PerUnit(price),
            request,
        )
    }

    fn buy(&mut self, request: &PurchaseRequest) -> Result<(), SimulationError> {
        execute_purchase(
            &self.input,
            &mut self.ledger,
            &mut self.recorder,
            &mut self.lots,
            0,
            PerUnit(10),
            request,
        )
    }
}

fn sale(lot_id: &str, units: i64) -> SaleRequest {
    SaleRequest {
        cause_id: "sale".into(),
        agent_id: "alice".into(),
        proceeds_account_id: "checking".into(),
        asset_id: "fund".into(),
        lots: vec![LotSale {
            account_id: "brokerage".into(),
            lot_id: lot_id.into(),
            units: Quantity(units),
        }],
    }
}

fn purchase() -> PurchaseRequest {
    PurchaseRequest {
        cause_id: "purchase".into(),
        agent_id: "alice".into(),
        cash_account_id: "checking".into(),
        holding_account_id: "brokerage".into(),
        asset_id: "fund".into(),
        lot_id: "bought".into(),
        quantity_scale: 10,
        units: Quantity(15),
    }
}

#[test]
fn exact_selection_is_not_fifo_and_full_lot_basis_reconciles() {
    let mut books = Books::new();
    books.sell(&sale("new", 3), 10).unwrap();
    assert_eq!(books.lots[0].units_remaining, Quantity(10));
    assert_eq!(books.recorder.dispositions[0].basis, Money(10));
    books.sell(&sale("new", 7), 10).unwrap();
    assert_eq!(books.lots[1].units_remaining, Quantity(0));
    assert_eq!(books.lots[1].basis_remaining, Money(0));
    assert_eq!(books.recorder.dispositions[1].basis, Money(22));
    assert_eq!(
        books
            .recorder
            .dispositions
            .iter()
            .map(|item| item.proceeds.0)
            .sum::<i64>(),
        10
    );
    assert_eq!(
        books.tax.facts[&("alice".into(), "a".into())].short_term_gain,
        Money(-22)
    );
    assert_eq!(books.ledger.trial_balance(), 0);
    assert!(
        books
            .recorder
            .dispositions
            .iter()
            .all(|item| item.source_account_id == "brokerage")
    );
}

#[test]
fn fifo_scheduled_sale_matches_the_same_explicit_selection() {
    let mut explicit = Books::new();
    let selected = select_fifo(&explicit.lots, &[1, 0], Quantity(13), "sale").unwrap();
    assert_eq!(selected[0].lot_id, "old");
    let request = SaleRequest {
        lots: selected,
        ..sale("old", 13)
    };
    explicit.deferred[0] = Money(20);
    explicit.sell(&request, 10).unwrap();
    let mut scheduled = Books::new();
    scheduled.deferred[0] = Money(20);
    let mut tlh =
        scheduled_tlh_give_back_state(&scheduled.input, &scheduled.lots, &scheduled.deferred)
            .unwrap();
    execute_sale(
        &scheduled.input,
        0,
        &mut scheduled.ledger,
        &mut scheduled.recorder,
        &mut scheduled.lots,
        &mut scheduled.tax,
        &mut tlh,
        &ScheduledSaleSpec {
            month: 0,
            cause_id: "sale".into(),
            agent_id: "alice".into(),
            account_id: "brokerage".into(),
            asset_id: "fund".into(),
            units: Quantity(13),
            proceeds_account_id: "checking".into(),
        },
    )
    .unwrap();
    apply_scheduled_tlh_give_back(&tlh, &mut scheduled.deferred).unwrap();
    assert_eq!(format!("{scheduled:?}"), format!("{explicit:?}"));
}

#[test]
fn invalid_exact_lot_requests_leave_every_book_unchanged() {
    for case in 0..10 {
        let mut books = Books::new();
        books.deferred[0] = Money(20);
        let mut request = sale("old", 3);
        match case {
            0 => request.lots[0].lot_id = "absent".into(),
            1 => request.lots[0].account_id = "checking".into(),
            2 => request.agent_id = "mallory".into(),
            3 => request.asset_id = "other".into(),
            4 => request.lots[0].units = Quantity(0),
            5 => request.lots[0].units = Quantity(-1),
            6 => request.lots[0].units = Quantity(11),
            7 => request.lots.push(request.lots[0].clone()),
            8 => request.proceeds_account_id = "undeclared".into(),
            9 => request.lots.clear(),
            _ => unreachable!(),
        }
        let before = format!("{books:?}");
        assert!(books.sell(&request, 10).is_err(), "case {case}");
        assert_eq!(format!("{books:?}"), before, "case {case}");
    }
}

#[test]
fn overflow_after_first_lot_or_jurisdiction_cannot_partially_commit() {
    for case in 0..5 {
        let mut books = Books::new();
        books.deferred[0] = Money(20);
        let mut request = sale("old", 10);
        request.lots.push(sale("new", 10).lots.remove(0));
        match case {
            0 => {
                books
                    .tax
                    .facts
                    .get_mut(&("alice".into(), "b".into()))
                    .unwrap()
                    .long_term_gain = Money(i64::MAX)
            }
            1 => books.recorder.disposition_count = u64::MAX - 1,
            2 => books.recorder.journal_entry_count = u64::MAX,
            3 => {
                let entry = JournalEntry {
                    month: 0,
                    cause_id: "large cash".into(),
                    postings: vec![
                        Posting {
                            account: AccountRef::new("alice", "checking"),
                            amount: Money(i64::MAX - 100),
                        },
                        Posting {
                            account: AccountRef::new("alice", OPENING_EQUITY),
                            amount: Money(-(i64::MAX - 100)),
                        },
                    ],
                };
                // A separate counterparty avoids overflowing the original opening equity.
                let mut entry = entry;
                entry.postings[1].account = AccountRef::new("world", "cash");
                books
                    .ledger
                    .ensure_account(entry.postings[1].account.clone());
                books.ledger.apply(&entry).unwrap();
            }
            _ => {}
        }
        let before = format!("{books:?}");
        let price = if case == 4 { i64::MAX } else { 20 };
        assert!(books.sell(&request, price).is_err(), "case {case}");
        assert_eq!(format!("{books:?}"), before, "case {case}");
    }
}

#[test]
fn rejected_scheduled_sale_does_not_consume_its_tlh_allowance() {
    let mut books = Books::new();
    books.deferred[0] = Money(20);
    books.recorder.journal_entry_count = u64::MAX;
    let mut tlh =
        scheduled_tlh_give_back_state(&books.input, &books.lots, &books.deferred).unwrap();
    let before = format!("{books:?}{tlh:?}");
    let result = execute_sale(
        &books.input,
        0,
        &mut books.ledger,
        &mut books.recorder,
        &mut books.lots,
        &mut books.tax,
        &mut tlh,
        &ScheduledSaleSpec {
            month: 0,
            cause_id: "sale".into(),
            agent_id: "alice".into(),
            account_id: "brokerage".into(),
            asset_id: "fund".into(),
            units: Quantity(3),
            proceeds_account_id: "checking".into(),
        },
    );
    assert!(result.is_err());
    assert_eq!(format!("{books:?}{tlh:?}"), before);
}

#[test]
fn purchase_posts_cash_and_basis_then_joins_future_exact_sales() {
    let mut books = Books::new();
    books.buy(&purchase()).unwrap();
    let lot = &books.lots[2];
    assert_eq!(lot.units_remaining, Quantity(15));
    assert_eq!(lot.basis_remaining, Money(15));
    assert_eq!(lot.spec.purchase_month, 0);
    assert_eq!(
        books
            .ledger
            .balance(&AccountRef::new("alice", "checking"))
            .unwrap(),
        Money(85)
    );
    books.sell(&sale("bought", 15), 20).unwrap();
    assert_eq!(books.recorder.dispositions[0].realized_gain, Money(15));
    assert_eq!(books.ledger.trial_balance(), 0);
}

#[test]
fn invalid_or_unfunded_purchase_does_not_create_lot_or_debit_cash() {
    for case in 0..9 {
        let mut books = Books::new();
        let mut request = purchase();
        match case {
            0 => request.lot_id = "old".into(),
            1 => request.units = Quantity(101),
            2 => request.units = Quantity(0),
            3 => request.units = Quantity(-1),
            4 => request.quantity_scale = 100,
            5 => request.holding_account_id = "other".into(),
            6 => request.agent_id = "mallory".into(),
            7 => request.cash_account_id = "other".into(),
            8 => books.recorder.journal_entry_count = u64::MAX,
            _ => unreachable!(),
        }
        let before = format!("{books:?}");
        assert!(books.buy(&request).is_err(), "case {case}");
        assert_eq!(format!("{books:?}"), before, "case {case}");
    }
}
