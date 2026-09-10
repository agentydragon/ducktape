//! Component model output is factual input to one atomic cash/tax settlement.
use super::*;
use crate::engine::components::{ComponentEffects, InterestIncome, initialize, settle};
use crate::execution::{TlhOperation, TlhPortfolioSpec};

fn setup() -> (ExecutionInput, RolloutState) {
    setup_at(100, 80)
}

fn setup_at(value: i64, basis: i64) -> (ExecutionInput, RolloutState) {
    let mut input = minimal_fixture();
    input.scenario.accounts[0].opening_balance = Money(100);
    input
        .scenario
        .income_sources
        .push(IncomeSource::interest(None));
    input.scenario.tlh_portfolios.push(TlhPortfolioSpec {
        portfolio_id: "managed".into(),
        owner_agent_id: "alice".into(),
        account_id: "custody".into(),
        asset_id: "fund".into(),
    });
    input.series.push(SeriesSpec {
        series_id: "security:fund".into(),
        snapshots: 2,
        values: vec![100, 110],
    });
    validate_fixture(&input).unwrap();
    let mut state = RolloutState::new(&input, 0, CaptureMode::Forensic, None).unwrap();
    initialize(
        &input,
        &mut state,
        vec![TlhPortfolioObservation {
            portfolio_id: "managed".into(),
            owner_agent_id: "alice".into(),
            account_id: "custody".into(),
            asset_id: "fund".into(),
            value: Money(value),
            reported_tax_basis: Money(basis),
        }],
        None,
    )
    .unwrap();
    state
        .tax
        .facts
        .insert(("alice".into(), "a".into()), TaxFacts::default());
    state
        .tax
        .facts
        .insert(("alice".into(), "b".into()), TaxFacts::default());
    state.tax.income = IncomeLedger::for_taxpayers(["alice"], &input.scenario.income_sources);
    (input, state)
}

fn harvest(state: &RolloutState) -> ComponentEffects {
    let mut observation = state.tlh_portfolios[0].clone();
    observation.reported_tax_basis = Money(70);
    ComponentEffects {
        observation,
        cash_account_id: None,
        cash_amount: Money(0),
        short_term_gain: Money(-10),
        long_term_gain: Money(0),
        interest: vec![],
    }
}

fn fingerprint(state: &RolloutState) -> String {
    format!(
        "{:?}{:?}{:?}{:?}",
        state.ledger, state.tax, state.recorder, state.tlh_portfolios
    )
}

#[test]
fn basis_statement_cash_and_tax_reconcile_without_ordinary_lots() {
    let (input, mut state) = setup();
    let effects = harvest(&state);
    settle(
        &input,
        &mut state,
        "alice",
        "manager",
        &effects,
        TlhOperation::ModeledRealization,
    )
    .unwrap();
    let mut contribution = effects.clone();
    contribution.observation.value = Money(120);
    contribution.observation.reported_tax_basis = Money(90);
    contribution.cash_account_id = Some("checking".into());
    contribution.cash_amount = Money(-20);
    contribution.short_term_gain = Money(0);
    settle(
        &input,
        &mut state,
        "alice",
        "contribution",
        &contribution,
        TlhOperation::Contribution,
    )
    .unwrap();
    assert_eq!(
        state
            .ledger
            .balance(&AccountRef::new("alice", "checking"))
            .unwrap(),
        Money(80)
    );
    assert_eq!(
        state
            .ledger
            .balance(&crate::engine::components::basis_account(
                &contribution.observation
            ))
            .unwrap(),
        Money(90)
    );
    assert!(state.lots.is_empty());
    assert!(
        state
            .tax
            .facts
            .values()
            .all(|facts| facts.short_term_gain == Money(-10) && facts.long_term_gain == Money(0))
    );
    assert_eq!(state.ledger.trial_balance(), 0);
}

#[test]
fn invalid_effects_and_overflow_leave_every_financial_book_unchanged() {
    for case in 0..9 {
        let (input, mut state) = setup();
        let mut effects = harvest(&state);
        match case {
            0 => effects.short_term_gain = Money(-9), // Inconsistent financial effect, not simulated ruin.
            1 => effects.observation.owner_agent_id = "other".into(),
            2 => effects.observation.reported_tax_basis = Money(-1),
            3 => state.recorder.journal_entry_count = u64::MAX,
            4 => {
                state
                    .tax
                    .facts
                    .get_mut(&("alice".into(), "b".into()))
                    .unwrap()
                    .short_term_gain = Money(i64::MIN)
            }
            5 => {
                effects.observation.reported_tax_basis = Money(181);
                effects.cash_account_id = Some("checking".into());
                effects.cash_amount = Money(-101);
                effects.short_term_gain = Money(0);
            }
            6 | 7 => {
                effects = ComponentEffects {
                    observation: state.tlh_portfolios[0].clone(),
                    cash_account_id: Some("checking".into()),
                    cash_amount: Money(if case == 6 { 10 } else { -10 }),
                    short_term_gain: Money(0),
                    long_term_gain: Money(0),
                    interest: vec![InterestIncome {
                        issuer_jurisdiction_id: if case == 6 {
                            Some("undeclared".into())
                        } else {
                            None
                        },
                        amount: Money(if case == 6 { 10 } else { -10 }),
                    }],
                };
            }
            8 => state.recorder.tlh_financial_effect_count = u64::MAX,
            _ => unreachable!(),
        }
        let before = fingerprint(&state);
        assert!(
            settle(
                &input,
                &mut state,
                "alice",
                "manager",
                &effects,
                TlhOperation::ModeledRealization
            )
            .is_err(),
            "case {case}"
        );
        assert_eq!(fingerprint(&state), before, "case {case}");
    }
}

#[test]
fn distribution_cash_uses_interest_source_not_capital_gain_journal_account() {
    let (input, mut state) = setup();
    let effects = ComponentEffects {
        observation: state.tlh_portfolios[0].clone(),
        cash_account_id: Some("checking".into()),
        cash_amount: Money(5),
        short_term_gain: Money(0),
        long_term_gain: Money(0),
        interest: vec![InterestIncome {
            issuer_jurisdiction_id: None,
            amount: Money(5),
        }],
    };
    settle(
        &input,
        &mut state,
        "alice",
        "distribution",
        &effects,
        TlhOperation::Distribution,
    )
    .unwrap();
    assert_eq!(
        state
            .ledger
            .balance(&AccountRef::new("alice", "checking"))
            .unwrap(),
        Money(105)
    );
    assert_eq!(
        state
            .ledger
            .balance(&realized_gain_account("alice"))
            .unwrap(),
        Money(0)
    );
    assert_eq!(
        state
            .ledger
            .balance(&AccountRef::new(EXTERNAL_AGENT, "boundary"))
            .unwrap(),
        Money(-5)
    );
    assert!(
        state
            .tax
            .facts
            .values()
            .all(|facts| facts.short_term_gain == Money(0) && facts.long_term_gain == Money(0))
    );
    assert_eq!(state.tlh_portfolios[0].reported_tax_basis, Money(80));
}

#[test]
fn withdrawal_receipt_does_not_recalculate_component_rounded_value() {
    let (input, mut state) = setup_at(2, 2);
    let mut effects = harvest(&state);
    // Private fill rounding is not recoverable from the previous rounded gross mark.
    effects.observation.value = Money(0);
    effects.observation.reported_tax_basis = Money(0);
    effects.cash_account_id = Some("checking".into());
    effects.cash_amount = Money(1);
    effects.short_term_gain = Money(0);
    effects.long_term_gain = Money(-1);
    let action = crate::engine::actors::Action::Withdraw(crate::engine::components::CashRequest {
        cause_id: "redemption".into(),
        agent_id: "alice".into(),
        portfolio_id: "managed".into(),
        cash_account_id: "checking".into(),
        amount: Money(1),
    });
    crate::engine::components::validate_request(&action, &effects, &state.tlh_portfolios).unwrap();
    settle(
        &input,
        &mut state,
        "alice",
        "redemption",
        &effects,
        TlhOperation::Redemption,
    )
    .unwrap();
    assert_eq!(state.tlh_portfolios[0].value, Money(0));
    assert_eq!(state.ledger.trial_balance(), 0);
}

#[test]
fn selected_component_capture_keeps_observed_stop_marks_and_live_path_identity() {
    for mode in [
        CaptureMode::Summary,
        CaptureMode::Dense,
        CaptureMode::Forensic,
    ] {
        let (mut input, state) = setup();
        input.rollout_count = 2;
        input.scenario.horizon_months = 2;
        input.series[0].snapshots = 3;
        input.series[0].values = vec![100, 110, 120, 100, 110, 120];
        let opening = state.tlh_portfolios.clone();
        let mut session = crate::engine::actors::Session::with_options(
            input,
            "alice",
            &[1, 0],
            mode,
            false,
            None,
            vec![(1, opening.clone()), (0, opening.clone())],
        )
        .unwrap();
        session.start().unwrap();
        session.begin_actions(vec![(0, 0), (1, 0)]).unwrap();
        let action =
            crate::engine::actors::Action::Withdraw(crate::engine::components::CashRequest {
                cause_id: "too-much".into(),
                agent_id: "alice".into(),
                portfolio_id: "managed".into(),
                cash_account_id: "checking".into(),
                amount: Money(101),
            });
        session
            .reject(1, action, "exceeds component value".into())
            .unwrap();
        let statuses = session.end_actions().unwrap();
        assert_eq!(
            statuses
                .iter()
                .map(|row| (row.rollout_id, row.month, row.stopped))
                .collect::<Vec<_>>(),
            [(1, 0, true), (0, 0, false)]
        );
        let mut live = opening.clone();
        live[0].value = Money(110);
        session
            .set_component_marks(vec![(1, opening.clone()), (0, live.clone())])
            .unwrap();
        session.close_month().unwrap();
        assert_eq!(
            session
                .decisions()
                .unwrap()
                .iter()
                .map(|row| row.rollout_id)
                .collect::<Vec<_>>(),
            [0]
        );
        session.begin_actions(vec![(0, 1)]).unwrap();
        session.end_actions().unwrap();
        live[0].value = Money(120);
        session.set_component_marks(vec![(0, live)]).unwrap();
        session.close_month().unwrap();
        let results = session.finish().unwrap();
        assert_eq!(
            results.iter().map(|row| row.rollout_id).collect::<Vec<_>>(),
            [1, 0]
        );
        assert_eq!(
            results[0].summary.public_holdings[0].values,
            [Money(100), Money(100)]
        );
        assert_eq!(
            results[1].summary.public_holdings[0].values,
            [Money(100), Money(110), Money(120)]
        );
        assert_eq!(results[0].summary.ending_book.tlh_portfolios, opening);
        assert_eq!(results[0].summary.ending_mark_month, 0);
        assert_eq!(results[1].summary.ending_mark_month, 2);
        if let Some(trace) = &results[0].trace {
            assert_eq!(
                trace.financial.months.last().unwrap().tlh_portfolios,
                opening
            );
        }
    }
}

#[test]
fn opening_component_rows_cannot_alias_or_escape_selected_paths() {
    let (input, state) = setup();
    for rows in [
        vec![
            (0, state.tlh_portfolios.clone()),
            (0, state.tlh_portfolios.clone()),
        ],
        vec![(1, state.tlh_portfolios.clone())],
    ] {
        assert!(matches!(
            crate::engine::actors::Session::with_options(
                input.clone(),
                "alice",
                &[0],
                CaptureMode::Summary,
                false,
                None,
                rows
            ),
            Err(SimulationError::InvalidRolloutSelection)
        ));
    }
}
