//! Immediate cash transfers, with actor admission separate from canonical posting.
//!
//! Scheduled contract cashflows supply their declared tax character. A bare actor
//! transfer cannot label itself as income, a deduction or a claim payment.

use super::*;

/// Move an exact amount from the actor's available cash to a declared cash account.
/// No automatic sale, borrowing or partial fill is implied.
#[derive(Clone, Debug, serde::Deserialize, serde::Serialize)]
pub struct TransferRequest {
    pub cause_id: String,
    pub from: AccountRef,
    pub to: AccountRef,
    pub amount: Money,
}

/// Admission and tax character supplied by the engine, never by the actor's payload.
pub(super) struct TransferContext<'a> {
    pub(super) actor_id: Option<&'a str>,
    pub(super) income_category: Option<&'a IncomeSource>,
    pub(super) deduction_category: Option<&'a str>,
}

pub(super) fn execute_transfer(
    input: &ExecutionInput,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax: &mut TaxState,
    month: u32,
    request: &TransferRequest,
    context: TransferContext<'_>,
) -> Result<(), SimulationError> {
    let TransferContext {
        actor_id,
        income_category,
        deduction_category,
    } = context;
    // None is reserved for engine-scheduled cashflows, not caller-requested credit.
    if let Some(actor_id) = actor_id {
        if request.cause_id.is_empty() {
            return Err(SimulationError::EmptyIdentifier {
                kind: "transfer request",
            });
        }
        if request.from.agent_id != actor_id {
            return Err(SimulationError::InvalidTransfer {
                cause_id: request.cause_id.clone(),
                reason: "source account does not belong to the actor".into(),
            });
        }
        if income_category.is_some() || deduction_category.is_some() {
            return Err(SimulationError::InvalidTransfer {
                cause_id: request.cause_id.clone(),
                reason: "bare actor transfers cannot declare tax character".into(),
            });
        }
    }
    for account in [&request.from, &request.to] {
        if !input
            .scenario
            .accounts
            .iter()
            .any(|spec| &spec.account == account)
        {
            return Err(SimulationError::UnknownAccountReference {
                context: request.cause_id.clone(),
                agent_id: account.agent_id.clone(),
                account_id: account.account_id.clone(),
            });
        }
    }
    if actor_id.is_some()
        && (request.amount.0 <= 0 || request.amount > ledger.balance(&request.from)?)
    {
        return Err(SimulationError::InvalidTransfer {
            cause_id: request.cause_id.clone(),
            reason: "amount must be positive and covered by available cash".into(),
        });
    }
    post_cashflow(
        ledger,
        recorder,
        tax,
        month,
        request,
        income_category,
        deduction_category,
    )
}

/// The accounting operation for both admitted actor transfers and scheduled cashflows.
/// Scheduled external flows may debit an exogenous counterparty below zero; that
/// does not grant an actor permission to overdraw. Every fallible update is staged.
fn post_cashflow(
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax: &mut TaxState,
    month: u32,
    request: &TransferRequest,
    income_category: Option<&IncomeSource>,
    deduction_category: Option<&str>,
) -> Result<(), SimulationError> {
    let TransferRequest {
        cause_id,
        from,
        to,
        amount,
    } = request;
    let amount = *amount;
    if let Some(category) = deduction_category
        && category != "ordinary"
    {
        return Err(SimulationError::UnsupportedDeductionCategory {
            category: category.into(),
        });
    }
    let recorded_income_category = income_category
        .filter(|_| {
            tax.facts
                .keys()
                .any(|(agent_id, _)| agent_id == &to.agent_id)
        })
        .map(|source| String::from(source.clone()));
    let mut income = tax.income.updates();
    if let Some(source) = income_category {
        income.accrue(&to.agent_id, source, amount)?;
    }
    if deduction_category == Some("ordinary") {
        income.deduct_from_ordinary(&from.agent_id, amount)?;
    }
    transfer_money(ledger, recorder, month, cause_id, from, to, amount)?;
    income.commit();
    recorder.record_transfer(TransferOutcome {
        month,
        cause_id: cause_id.clone(),
        from: from.clone(),
        to: to.clone(),
        amount,
        income_category: recorded_income_category,
    });
    Ok(())
}
