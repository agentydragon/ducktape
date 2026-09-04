//! Scheduled and recurring transfers, and the balanced posting every money movement
//! goes through.

use super::*;

pub(super) fn execute_cashflows(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &[PropertyState],
    month: u32,
) -> Result<(), SimulationError> {
    for cashflow in fixture
        .scenario
        .scheduled_transfers
        .iter()
        .filter(|cashflow| cashflow.month == month)
    {
        apply_cashflow(
            ledger,
            recorder,
            tax_facts,
            month,
            &cashflow.cause_id,
            &cashflow.from,
            &cashflow.to,
            amount_value(fixture, rollout_id, month, &cashflow.amount)?,
            cashflow.income_category.as_deref(),
            cashflow.deduction_category.as_deref(),
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
        apply_cashflow(
            ledger,
            recorder,
            tax_facts,
            month,
            &cashflow.cause_id,
            &cashflow.from,
            &cashflow.to,
            amount_value(fixture, rollout_id, month, &cashflow.amount)?,
            cashflow.income_category.as_deref(),
            cashflow.deduction_category.as_deref(),
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
            apply_cashflow(
                ledger,
                recorder,
                tax_facts,
                month,
                &cashflow.cause_id,
                &cashflow.from,
                &cashflow.to,
                amount_value(fixture, rollout_id, month, &cashflow.amount)?,
                cashflow.income_category.as_deref(),
                cashflow.deduction_category.as_deref(),
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
            apply_cashflow(
                ledger,
                recorder,
                tax_facts,
                month,
                &cashflow.cause_id,
                &cashflow.from,
                &cashflow.to,
                amount_value(fixture, rollout_id, month, &cashflow.amount)?,
                cashflow.income_category.as_deref(),
                cashflow.deduction_category.as_deref(),
            )?;
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn apply_cashflow(
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    month: u32,
    cause_id: &str,
    from: &AccountRef,
    to: &AccountRef,
    amount: Money,
    income_category: Option<&str>,
    deduction_category: Option<&str>,
) -> Result<(), SimulationError> {
    transfer_money(ledger, recorder, month, cause_id, from, to, amount)?;
    let recorded_income_category = (income_category == Some("ordinary")
        && tax_facts
            .keys()
            .any(|(agent_id, _)| agent_id == &to.agent_id))
    .then(|| "ordinary".to_owned());
    recorder.record_transfer(TransferOutcome {
        month,
        cause_id: cause_id.into(),
        from: from.clone(),
        to: to.clone(),
        amount,
        income_category: recorded_income_category,
    });
    record_transfer_income(tax_facts, &to.agent_id, income_category, amount)?;
    record_transfer_deduction(tax_facts, &from.agent_id, deduction_category, amount)
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
