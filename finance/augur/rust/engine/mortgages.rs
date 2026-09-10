//! Immutable mortgage settlement inputs. Servicing and tax-year memory live in Python.

use super::*;
use crate::execution::{MortgageFinancingSpec, ScheduledPropertyPurchaseSpec};
use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
pub struct Origination {
    pub liability_id: String,
    pub monthly_payment: Money,
}

#[derive(Deserialize)]
pub struct Payoff {
    pub liability_id: String,
    pub principal: Money,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Installment {
    pub liability_id: String,
    pub interest: Money,
    pub principal: Money,
    pub rental_interest: Money,
}

#[derive(Deserialize)]
pub struct Interest {
    pub liability_id: String,
    pub owner_interest_paid_ytd: Money,
    pub origination_principal: Money,
}

#[derive(Default, Serialize)]
pub struct OpeningEvents {
    pub originated: Vec<String>,
    pub paid_off: Vec<String>,
}

pub(super) struct Opening<'a> {
    pub originations: &'a [Origination],
    pub payoffs: &'a [Payoff],
    pub events: OpeningEvents,
}

pub(super) fn invalid(reason: impl Into<String>) -> SimulationError {
    SimulationError::InvalidMortgageEffect {
        reason: reason.into(),
    }
}

pub(super) fn terms<'a>(
    input: &'a ExecutionInput,
    liability_id: &str,
) -> Result<(&'a ScheduledPropertyPurchaseSpec, &'a MortgageFinancingSpec), SimulationError> {
    input
        .scenario
        .scheduled_property_purchases
        .iter()
        .find_map(|purchase| {
            purchase
                .mortgage
                .as_ref()
                .filter(|loan| loan.liability_id == liability_id)
                .map(|loan| (purchase, loan))
        })
        .ok_or_else(|| invalid(format!("unknown mortgage {liability_id}")))
}

pub(super) fn principal(
    input: &ExecutionInput,
    ledger: &Ledger,
    liability_id: &str,
) -> Result<Money, SimulationError> {
    let (purchase, loan) = terms(input, liability_id)?;
    let principal = ledger
        .balance(&mortgage_liability_account(
            &purchase.buyer_agent_id,
            liability_id,
        ))?
        .checked_neg()?;
    let receivable = ledger.balance(&mortgage_receivable_account(
        &loan.lender_agent_id,
        liability_id,
    ))?;
    if principal.0 < 0 || receivable != principal {
        return Err(invalid("mortgage liability and receivable disagree"));
    }
    Ok(principal)
}

impl<'a> Opening<'a> {
    pub fn new(
        input: &ExecutionInput,
        state: &RolloutState,
        originations: &'a [Origination],
        payoffs: &'a [Payoff],
    ) -> Result<Self, SimulationError> {
        let mut ids = BTreeSet::new();
        for item in originations {
            let (purchase, _) = terms(input, &item.liability_id)?;
            if !ids.insert(&item.liability_id)
                || purchase.month != state.month
                || item.monthly_payment.0 < 0
                || principal(input, &state.ledger, &item.liability_id)?.0 != 0
            {
                return Err(invalid("invalid mortgage origination"));
            }
        }
        let expected = input
            .scenario
            .scheduled_property_purchases
            .iter()
            .filter(|purchase| purchase.month == state.month)
            .filter_map(|purchase| purchase.mortgage.as_ref().map(|loan| &loan.liability_id))
            .collect::<BTreeSet<_>>();
        if ids != expected {
            return Err(invalid("missing mortgage origination"));
        }
        ids.clear();
        for item in payoffs {
            let (purchase, _) = terms(input, &item.liability_id)?;
            if !ids.insert(&item.liability_id)
                || item.principal.0 <= 0
                || item.principal != principal(input, &state.ledger, &item.liability_id)?
                || !input.scenario.property_sales.iter().any(|sale| {
                    sale.month == state.month && sale.property_id == purchase.property_id
                })
                || !state
                    .properties
                    .iter()
                    .any(|property| property.active && property.property_id == purchase.property_id)
            {
                return Err(invalid("invalid mortgage payoff"));
            }
        }
        for sale in input
            .scenario
            .property_sales
            .iter()
            .filter(|sale| sale.month == state.month)
        {
            if !state
                .properties
                .iter()
                .any(|property| property.active && property.property_id == sale.property_id)
            {
                continue;
            }
            let purchase = input
                .scenario
                .scheduled_property_purchases
                .iter()
                .find(|purchase| purchase.property_id == sale.property_id)
                .expect("validated sale");
            if let Some(loan) = &purchase.mortgage {
                if principal(input, &state.ledger, &loan.liability_id)?.0 > 0
                    && !ids.contains(&loan.liability_id)
                {
                    return Err(invalid("missing mortgage payoff"));
                }
            }
        }
        Ok(Self {
            originations,
            payoffs,
            events: OpeningEvents::default(),
        })
    }
}

pub(super) fn validate_snapshots(
    input: &ExecutionInput,
    state: &RolloutState,
    snapshots: &[MortgageSnapshot],
) -> Result<(), SimulationError> {
    let mut ids = BTreeSet::new();
    for snapshot in snapshots {
        let (purchase, loan) = terms(input, &snapshot.liability_id)?;
        if !ids.insert(&snapshot.liability_id)
            || snapshot.principal != principal(input, &state.ledger, &snapshot.liability_id)?
            || snapshot.agent_id != purchase.buyer_agent_id
            || snapshot.property_id != purchase.property_id
            || snapshot.counterparty_agent_id != loan.lender_agent_id
            || snapshot.interest_paid_ytd.0 < 0
            || snapshot.rental_interest_paid_ytd.0 < 0
            || snapshot.rental_interest_paid_ytd > snapshot.interest_paid_ytd
        {
            return Err(invalid("invalid mortgage capture snapshot"));
        }
    }
    let expected = input
        .scenario
        .scheduled_property_purchases
        .iter()
        .filter(|purchase| {
            state
                .properties
                .iter()
                .any(|property| property.property_id == purchase.property_id)
        })
        .filter_map(|purchase| purchase.mortgage.as_ref().map(|loan| &loan.liability_id))
        .collect::<BTreeSet<_>>();
    if ids != expected {
        return Err(invalid("mortgage snapshots must cover originated loans"));
    }
    Ok(())
}
