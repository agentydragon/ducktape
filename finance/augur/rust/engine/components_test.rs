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
fn component_capture_keeps_explicit_stop_marks_and_independent_books() {
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
        let prepared = crate::engine::world::Prepared::new(input).unwrap();
        let mut stopped = prepared
            .world(1, opening.clone(), mode, Some("alice"), None)
            .unwrap();
        let mut live = prepared
            .world(0, opening.clone(), mode, Some("alice"), None)
            .unwrap();
        stopped.prepare_month(0, &[], &[]).unwrap();
        stopped.assemble_claims(&[]).unwrap();
        stopped.set_component_marks(opening.clone()).unwrap();
        stopped.close_month(true, Money(0), &[], &[]).unwrap();
        for month in 0..2 {
            live.prepare_month(month, &[], &[]).unwrap();
            live.assemble_claims(&[]).unwrap();
            let mut marks = opening.clone();
            marks[0].value = Money(110 + 10 * i64::from(month));
            live.set_component_marks(marks).unwrap();
            live.close_month(false, Money(0), &[], &[]).unwrap();
        }
        let stopped = stopped.finish(&[]).unwrap();
        let live = live.finish(&[]).unwrap();
        assert_eq!((stopped.rollout_id, live.rollout_id), (1, 0));
        let stopped_summary = stopped.summary.unwrap();
        let live_summary = live.summary.unwrap();
        assert_eq!(
            stopped_summary.public_holdings[0].values,
            [Money(100), Money(100)]
        );
        assert_eq!(
            live_summary.public_holdings[0].values,
            [Money(100), Money(110), Money(120)]
        );
        assert_eq!(stopped_summary.ending_book.tlh_portfolios, opening);
        assert_eq!(stopped_summary.ending_mark_month, 0);
        assert_eq!(live_summary.ending_mark_month, 2);
        if let Some(financial) = stopped.financial {
            assert_eq!(financial.months.last().unwrap().tlh_portfolios, opening);
        }
    }
}

#[test]
fn opening_component_rows_require_exact_portfolio_coverage() {
    let (input, state) = setup();
    let prepared = crate::engine::world::Prepared::new(input).unwrap();
    let duplicate = [state.tlh_portfolios.clone(), state.tlh_portfolios.clone()].concat();
    for marks in [Vec::new(), duplicate] {
        assert!(matches!(
            prepared.world(0, marks, CaptureMode::Summary, Some("alice"), None),
            Err(SimulationError::InvalidComponentEffect { .. })
        ));
    }
    assert!(matches!(
        prepared.world(
            1,
            state.tlh_portfolios,
            CaptureMode::Summary,
            Some("alice"),
            None
        ),
        Err(SimulationError::InvalidRolloutSelection)
    ));
}

#[test]
fn zero_component_marks_do_not_relax_ordinary_quote_or_negative_mark_validation() {
    let (mut input, _) = setup_at(0, 80);
    input.series[0].values = vec![0, 0];
    assert!(validate_fixture(&input).is_ok());
    // The component's declaration may also supply its public observable-price binding.
    input
        .scenario
        .holding_pools
        .push(holding_pool("alice", "custody", "fund", 1));
    assert!(validate_fixture(&input).is_ok());
    input.series[0].values[1] = -1;
    assert!(matches!(
        validate_fixture(&input),
        Err(SimulationError::InvalidSecurityPrice { value: -1, .. })
    ));
    input.series[0].values[1] = 0;
    // Another ordinary pool using the same asset still needs a positive trading quote,
    // even when it currently holds no units.
    input
        .scenario
        .holding_pools
        .push(holding_pool("alice", "ordinary", "fund", 1));
    assert!(matches!(
        validate_fixture(&input),
        Err(SimulationError::InvalidSecurityPrice { value: 0, .. })
    ));
}
