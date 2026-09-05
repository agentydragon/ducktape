//! Ledger account naming. Every internal account an engine posting can name is minted
//! here, so the chart of accounts is one list rather than string literals at call sites.

use super::*;

pub(super) fn asset_basis_account(lot: &InitialLotSpec) -> AccountRef {
    AccountRef::new(
        &lot.agent_id,
        format!("asset-basis:{}:{}", lot.account_id, lot.asset_id),
    )
}

pub(super) fn realized_gain_account(agent_id: &str) -> AccountRef {
    AccountRef::new(agent_id, "income:realized-gain")
}

pub(super) fn property_basis_writeoff_account(agent_id: &str, property_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("expense:property-basis:{property_id}"))
}

pub(super) fn tax_expense_account(agent_id: &str, jurisdiction_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("expense:tax:{jurisdiction_id}"))
}

pub(super) fn tax_liability_account(agent_id: &str, jurisdiction_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("liability:tax:{jurisdiction_id}"))
}

pub(super) fn tax_prepayment_account(agent_id: &str) -> AccountRef {
    AccountRef::new(agent_id, "asset:tax-prepayments")
}

pub(super) fn tax_authority_revenue_account(agent_id: &str) -> AccountRef {
    AccountRef::new(agent_id, "income:tax-payments")
}

pub(super) fn property_asset_account(agent_id: &str, property_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("asset:property:{property_id}"))
}

pub(super) fn property_sale_clearing_account(agent_id: &str, property_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("equity:property-sale:{property_id}"))
}

pub(super) fn mortgage_liability_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("liability:mortgage:{liability_id}"))
}

pub(super) fn mortgage_interest_expense_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(
        agent_id,
        format!("expense:mortgage-interest:{liability_id}"),
    )
}

pub(super) fn mortgage_receivable_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(
        agent_id,
        format!("asset:mortgage-receivable:{liability_id}"),
    )
}

pub(super) fn mortgage_interest_income_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("income:mortgage-interest:{liability_id}"))
}

pub(super) fn mortgage_funding_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("equity:mortgage-funding:{liability_id}"))
}
