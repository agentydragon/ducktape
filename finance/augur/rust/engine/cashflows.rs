//! Scheduled and recurring transfers, and the balanced posting every money movement
//! goes through.

use super::*;

pub(super) fn execute_cashflows(
    fixture: &ExecutionInput,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax: &mut TaxState,
    properties: &[PropertyState],
    month: u32,
) -> Result<(), SimulationError> {
    for cashflow in fixture
        .scenario
        .scheduled_transfers
        .iter()
        .filter(|cashflow| cashflow.month == month)
    {
        execute_transfer(
            fixture,
            ledger,
            recorder,
            tax,
            month,
            &TransferRequest {
                cause_id: cashflow.cause_id.clone(),
                from: cashflow.from.clone(),
                to: cashflow.to.clone(),
                amount: amount_value(fixture, rollout_id, month, &cashflow.amount)?,
            },
            TransferContext {
                actor_id: None,
                income_category: cashflow.income_category.as_ref(),
                deduction_category: cashflow.deduction_category.as_deref(),
            },
        )?;
    }
    for cashflow in fixture
        .scenario
        .recurring_transfers
        .iter()
        .filter(|cashflow| {
            cashflow.start_month <= month && cashflow.end_month.is_none_or(|end| month <= end)
        })
    {
        execute_transfer(
            fixture,
            ledger,
            recorder,
            tax,
            month,
            &TransferRequest {
                cause_id: cashflow.cause_id.clone(),
                from: cashflow.from.clone(),
                to: cashflow.to.clone(),
                amount: amount_value(fixture, rollout_id, month, &cashflow.amount)?,
            },
            TransferContext {
                actor_id: None,
                income_category: cashflow.income_category.as_ref(),
                deduction_category: cashflow.deduction_category.as_deref(),
            },
        )?;
    }
    for cashflow in fixture
        .scenario
        .scheduled_property_cashflows
        .iter()
        .filter(|cashflow| cashflow.month == month)
    {
        if properties
            .iter()
            .any(|property| property.property_id == cashflow.property_id && property.active)
        {
            execute_transfer(
                fixture,
                ledger,
                recorder,
                tax,
                month,
                &TransferRequest {
                    cause_id: cashflow.cause_id.clone(),
                    from: cashflow.from.clone(),
                    to: cashflow.to.clone(),
                    amount: amount_value(fixture, rollout_id, month, &cashflow.amount)?,
                },
                TransferContext {
                    actor_id: None,
                    income_category: cashflow.income_category.as_ref(),
                    deduction_category: cashflow.deduction_category.as_deref(),
                },
            )?;
        }
    }
    for cashflow in fixture
        .scenario
        .recurring_property_cashflows
        .iter()
        .filter(|cashflow| {
            cashflow.start_month <= month && cashflow.end_month.is_none_or(|end| month <= end)
        })
    {
        if properties
            .iter()
            .any(|property| property.property_id == cashflow.property_id && property.active)
        {
            execute_transfer(
                fixture,
                ledger,
                recorder,
                tax,
                month,
                &TransferRequest {
                    cause_id: cashflow.cause_id.clone(),
                    from: cashflow.from.clone(),
                    to: cashflow.to.clone(),
                    amount: amount_value(fixture, rollout_id, month, &cashflow.amount)?,
                },
                TransferContext {
                    actor_id: None,
                    income_category: cashflow.income_category.as_ref(),
                    deduction_category: cashflow.deduction_category.as_deref(),
                },
            )?;
        }
    }
    Ok(())
}

pub(super) fn transfer_money(
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    month: u32,
    cause_id: &str,
    from: &AccountRef,
    to: &AccountRef,
    amount: Money,
) -> Result<(), SimulationError> {
    recorder.apply_entry(
        ledger,
        JournalEntry {
            month,
            cause_id: cause_id.into(),
            postings: vec![
                Posting {
                    account: from.clone(),
                    amount: amount.checked_neg()?,
                },
                Posting {
                    account: to.clone(),
                    amount,
                },
            ],
        },
    )
}
