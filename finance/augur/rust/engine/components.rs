//! Settle facts produced by Python-owned components. No component model or cohort
//! state lives here; the financial kernel checks cash, journals and tax consequences.

use super::*;
use crate::execution::TlhOperation;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CashRequest {
    pub cause_id: String,
    pub agent_id: String,
    pub portfolio_id: String,
    pub cash_account_id: String,
    pub amount: Money,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct LiquidateRequest {
    pub cause_id: String,
    pub agent_id: String,
    pub portfolio_id: String,
    pub cash_account_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ComponentEffects {
    pub observation: TlhPortfolioObservation,
    pub cash_account_id: Option<String>,
    /// Signed cash INTO the household account; contributions are negative.
    pub cash_amount: Money,
    pub short_term_gain: Money,
    pub long_term_gain: Money,
    pub interest: Vec<InterestIncome>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InterestIncome {
    pub issuer_jurisdiction_id: Option<String>,
    pub amount: Money,
}

pub(super) fn validate_request(
    action: &actors::Action,
    effects: &ComponentEffects,
    observations: &[TlhPortfolioObservation],
) -> Result<(), SimulationError> {
    let invalid = || SimulationError::InvalidComponentEffect {
        reason: "component effects do not match the requested operation".into(),
    };
    let (agent, portfolio, cash, expected) = match action {
        actors::Action::Contribute(request) if request.amount.0 >= 0 => (
            &request.agent_id,
            &request.portfolio_id,
            &request.cash_account_id,
            request.amount.checked_neg()?,
        ),
        actors::Action::Withdraw(request) if request.amount.0 >= 0 => (
            &request.agent_id,
            &request.portfolio_id,
            &request.cash_account_id,
            request.amount,
        ),
        actors::Action::Liquidate(request) => {
            let old = observations
                .iter()
                .find(|item| item.portfolio_id == request.portfolio_id)
                .ok_or_else(invalid)?;
            if effects.observation.value != Money(0)
                || effects.observation.reported_tax_basis != Money(0)
            {
                return Err(invalid());
            }
            (
                &request.agent_id,
                &request.portfolio_id,
                &request.cash_account_id,
                old.value,
            )
        }
        _ => return Err(invalid()),
    };
    if &effects.observation.owner_agent_id != agent
        || &effects.observation.portfolio_id != portfolio
        || effects.cash_account_id.as_ref() != Some(cash)
        || effects.cash_amount != expected
        || !effects.interest.is_empty()
    {
        return Err(invalid());
    }
    Ok(())
}

pub(super) fn basis_account(observation: &TlhPortfolioObservation) -> AccountRef {
    AccountRef::new(
        &observation.owner_agent_id,
        format!("asset:managed-portfolio:{}", observation.portfolio_id),
    )
}

pub(super) fn validate_observation(
    input: &ExecutionInput,
    observation: &TlhPortfolioObservation,
) -> Result<(), SimulationError> {
    let valid = input.scenario.tlh_portfolios.iter().any(|spec| {
        spec.portfolio_id == observation.portfolio_id
            && spec.owner_agent_id == observation.owner_agent_id
            && spec.account_id == observation.account_id
            && spec.asset_id == observation.asset_id
    });
    if !valid || observation.value.0 < 0 || observation.reported_tax_basis.0 < 0 {
        return Err(SimulationError::InvalidComponentEffect {
            reason: "component observation has unknown ownership or negative value/basis".into(),
        });
    }
    Ok(())
}

pub(super) fn initialize(
    input: &ExecutionInput,
    state: &mut RolloutState,
    observations: Vec<TlhPortfolioObservation>,
    product: Option<&ProductInputs>,
) -> Result<(), SimulationError> {
    let ids: BTreeSet<_> = observations.iter().map(|item| &item.portfolio_id).collect();
    if ids.len() != observations.len() || ids.len() != input.scenario.tlh_portfolios.len() {
        return Err(SimulationError::InvalidComponentEffect {
            reason: "opening component observations must cover exactly the declared portfolios"
                .into(),
        });
    }
    for observation in &observations {
        validate_observation(input, observation)?;
        state.recorder.apply_entry(
            &mut state.ledger,
            JournalEntry {
                month: 0,
                cause_id: format!("opening-component:{}", observation.portfolio_id),
                postings: vec![
                    Posting {
                        account: basis_account(observation),
                        amount: observation.reported_tax_basis,
                    },
                    Posting {
                        account: AccountRef::new(&observation.owner_agent_id, OPENING_EQUITY),
                        amount: observation.reported_tax_basis.checked_neg()?,
                    },
                ],
            },
        )?;
    }
    state.tlh_portfolios = observations;
    if let Some(opening) = state.recorder.months.first_mut() {
        opening.balances = account_balances(&state.ledger);
        opening.tlh_portfolios.clone_from(&state.tlh_portfolios);
    }
    if let Some(inputs) = product {
        state.product_metrics[0] = product_snapshot(
            input,
            inputs,
            state.rollout_id,
            0,
            &state.ledger,
            &state.lots,
            &state.tlh_portfolios,
            &state.properties,
            &[],
            Money(0),
            false,
        )?;
    }
    Ok(())
}

/// A changed market quote can update gross value, never basis or cash/tax state.
pub(super) fn mark(
    input: &ExecutionInput,
    state: &mut RolloutState,
    observations: Vec<TlhPortfolioObservation>,
) -> Result<(), SimulationError> {
    let ids: BTreeSet<_> = observations.iter().map(|item| &item.portfolio_id).collect();
    if ids.len() != observations.len() || observations.len() != state.tlh_portfolios.len() {
        return Err(SimulationError::InvalidComponentEffect {
            reason: "component marks must cover exactly the existing portfolios".into(),
        });
    }
    for observation in &observations {
        validate_observation(input, observation)?;
        let matches_basis = state.tlh_portfolios.iter().any(|previous| {
            previous.portfolio_id == observation.portfolio_id
                && previous.reported_tax_basis == observation.reported_tax_basis
        });
        if !matches_basis {
            return Err(SimulationError::InvalidComponentEffect {
                reason: "a mark-only update cannot change component basis".into(),
            });
        }
    }
    state.tlh_portfolios = observations;
    Ok(())
}

/// Stage all fallible arithmetic and tax updates before the atomic journal posting.
/// Python commits its candidate component only after this operation succeeds.
pub(super) fn settle(
    input: &ExecutionInput,
    state: &mut RolloutState,
    actor: &str,
    cause_id: &str,
    effects: &ComponentEffects,
    operation: TlhOperation,
) -> Result<(), SimulationError> {
    let observation = &effects.observation;
    validate_observation(input, observation)?;
    for slice in &effects.interest {
        let source = IncomeSource::interest(slice.issuer_jurisdiction_id.as_deref());
        if slice.amount.0 < 0
            || !input.scenario.income_sources.contains(&source)
            || slice.issuer_jurisdiction_id.as_ref().is_some_and(|issuer| {
                !input
                    .scenario
                    .jurisdictions
                    .iter()
                    .any(|item| &item.jurisdiction_id == issuer)
            })
        {
            return Err(SimulationError::InvalidComponentEffect {
                reason: "component interest needs a declared income source and nonnegative amount"
                    .into(),
            });
        }
    }
    if cause_id.is_empty() || observation.owner_agent_id != actor {
        return Err(SimulationError::InvalidComponentEffect {
            reason: "component effects need a cause and the component's owner".into(),
        });
    }
    let index = state
        .tlh_portfolios
        .iter()
        .position(|current| current.portfolio_id == observation.portfolio_id)
        .ok_or_else(|| SimulationError::InvalidComponentEffect {
            reason: "component has no opening observation".into(),
        })?;
    let previous = &state.tlh_portfolios[index];
    let basis_change = observation
        .reported_tax_basis
        .checked_sub(previous.reported_tax_basis)?;
    let gain = effects.interest.iter().try_fold(
        effects
            .short_term_gain
            .checked_add(effects.long_term_gain)?,
        |sum, income| sum.checked_add(income.amount),
    )?;
    if effects.cash_amount.checked_add(basis_change)? != gain {
        return Err(SimulationError::InvalidComponentEffect {
            reason: "component cash, basis change and realized gains do not reconcile".into(),
        });
    }
    let mut postings = Vec::new();
    if let Some(account_id) = &effects.cash_account_id {
        let account = AccountRef::new(actor, account_id);
        if !input
            .scenario
            .accounts
            .iter()
            .any(|spec| spec.account == account)
        {
            return Err(SimulationError::UnknownAccountReference {
                context: cause_id.into(),
                agent_id: actor.into(),
                account_id: account_id.clone(),
            });
        }
        if state
            .ledger
            .balance(&account)?
            .checked_add(effects.cash_amount)?
            .0
            < 0
        {
            return Err(SimulationError::InvalidComponentEffect {
                reason: "component contribution exceeds available cash".into(),
            });
        }
        postings.push(Posting {
            account,
            amount: effects.cash_amount,
        });
    } else if effects.cash_amount != Money(0) {
        return Err(SimulationError::InvalidComponentEffect {
            reason: "component cash movement needs a household account".into(),
        });
    }
    postings.push(Posting {
        account: basis_account(observation),
        amount: basis_change,
    });
    postings.push(Posting {
        account: realized_gain_account(actor),
        amount: effects
            .short_term_gain
            .checked_add(effects.long_term_gain)?
            .checked_neg()?,
    });
    let interest_total = effects
        .interest
        .iter()
        .try_fold(Money(0), |sum, item| sum.checked_add(item.amount))?;
    if interest_total != Money(0) {
        postings.push(Posting {
            account: AccountRef::new(EXTERNAL_AGENT, "boundary"),
            amount: interest_total.checked_neg()?,
        });
    }
    let mut tax = CapitalGainUpdates::for_facts(&mut state.tax.facts, actor);
    tax.accrue(effects.short_term_gain, false)?;
    tax.accrue(effects.long_term_gain, true)?;
    let mut income = state.tax.income.updates();
    for slice in &effects.interest {
        income.accrue(
            actor,
            &IncomeSource::interest(slice.issuer_jurisdiction_id.as_deref()),
            slice.amount,
        )?;
    }
    state.recorder.apply_tlh_effect(
        &mut state.ledger,
        JournalEntry {
            month: state.month,
            cause_id: cause_id.into(),
            postings,
        },
        TlhFinancialEffect {
            month: state.month,
            cause_id: cause_id.into(),
            portfolio_id: observation.portfolio_id.clone(),
            agent_id: actor.into(),
            account_id: observation.account_id.clone(),
            cash_account_id: effects.cash_account_id.clone(),
            operation,
            cash_amount: effects.cash_amount,
            short_term_gain: effects.short_term_gain,
            long_term_gain: effects.long_term_gain,
            basis_change,
            interest_income: interest_total,
        },
    )?;
    tax.commit();
    income.commit();
    state.tlh_portfolios[index] = observation.clone();
    Ok(())
}
