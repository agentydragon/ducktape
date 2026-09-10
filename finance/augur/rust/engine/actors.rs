//! Exact financial request execution and current actor-scoped facts.

use super::*;
use serde::{Deserialize, Serialize};

pub mod outcomes;

/// Exact immediate-cash requests in declared public pools/assets, including empty pools.
/// Sequence order is execution order, not priority by type.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind")]
pub enum Action {
    Sell(trades::SaleRequest),
    Buy(trades::PurchaseRequest),
    Transfer(transfers::TransferRequest),
    PayClaim(payments::PayClaim),
    Consume(payments::Consume),
    Contribute(components::CashRequest),
    Withdraw(components::CashRequest),
    Liquidate(components::LiquidateRequest),
}

impl Action {
    pub(super) fn cause_id(&self) -> &str {
        match self {
            Self::Sell(request) => &request.cause_id,
            Self::Buy(request) => &request.cause_id,
            Self::Transfer(request) => &request.cause_id,
            Self::PayClaim(request) => &request.cause_id,
            Self::Consume(request) => &request.cause_id,
            Self::Contribute(request) | Self::Withdraw(request) => &request.cause_id,
            Self::Liquidate(request) => &request.cause_id,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", content = "detail")]
pub enum Rejection {
    InvalidRequest(String),
    Payment(payments::Rejection),
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", content = "reason")]
pub enum Outcome {
    /// Full execution of the exact request; canonical financial records carry its effects.
    Executed,
    Rejected(Rejection),
}

/// Current actor books after scheduled cashflows and claim assembly. No future paths,
/// other actors' books or mutable execution state reach the decision function.
pub struct Observation<'a> {
    pub books: observations::ActorBooks<'a>,
    pub(super) claims: &'a claims::Claims,
}

/// Already-held, unredeemed contract terms and current par/indexed principal.
/// This is not a public-security quote or an offer to liquidate the bond.
pub struct HeldBond<'a> {
    pub terms: &'a BondSpec,
    pub principal: Money,
}

impl Observation<'_> {
    pub fn claims(&self) -> impl Iterator<Item = observations::Claim<'_>> {
        observations::due_claims(self.claims, self.books.agent_id())
    }

    pub fn held_bonds(&self) -> impl Iterator<Item = Result<HeldBond<'_>, SimulationError>> {
        self.books
            .input
            .scenario
            .initial_bonds
            .iter()
            .filter(|bond| bond.agent_id == self.books.agent_id())
            .filter_map(|bond| {
                // Scheduled facts for this event already ran, including redemption.
                match bond_held_principal(
                    self.books.input,
                    self.books.rollout,
                    bond,
                    self.books.month + 1,
                    self.books.month,
                ) {
                    Ok(Some(principal)) => Some(Ok(HeldBond {
                        terms: bond,
                        principal,
                    })),
                    Ok(None) => None,
                    Err(error) => Some(Err(error)),
                }
            })
    }
}

pub(super) fn execute(
    state: &mut RolloutState,
    input: &ExecutionInput,
    holdings: &AgentHoldings,
    claims: &mut claims::Claims,
    action: &Action,
) -> Result<(Outcome, Option<(payments::Request, payments::Receipt)>), SimulationError> {
    if action.cause_id().is_empty() {
        return Ok((
            Outcome::Rejected(Rejection::InvalidRequest("empty cause ID".into())),
            None,
        ));
    }
    let actor = holdings.agent_id();
    let payment = match action {
        Action::PayClaim(request) => Some(payments::Request::PayClaim(request.clone())),
        Action::Consume(request) => Some(payments::Request::Consume(request.clone())),
        _ => None,
    };
    if let Some(request) = payment {
        let receipt = payments::Context {
            fixture: input,
            ledger: &mut state.ledger,
            recorder: &mut state.recorder,
            tax: &mut state.tax,
            properties: &state.properties,
            mortgages: &mut state.mortgages,
            tax_liabilities: &mut state.tax_liabilities,
            month: state.month,
        }
        .execute(actor, claims, &request)?;
        // An otherwise valid consumption request can fail for lack of cash. Its
        // requested/paid gap is reportable, not a newly incurred debt. Unpaid claims
        // are recorded once below, including those whose payment was never attempted.
        let unfunded_consumption = matches!(request, payments::Request::Consume(_))
            && matches!(
                receipt.outcome,
                payments::Outcome::Rejected(payments::Rejection::InsufficientCash { .. })
            );
        if receipt.outcome == payments::Outcome::Paid || unfunded_consumption {
            let target = request.describe(claims).expect("resolved payment target");
            state.recorder.record_obligation(receipt.obligation(
                &request,
                &target,
                state.month,
                target.label,
            )?);
        }
        let outcome = match &receipt.outcome {
            payments::Outcome::Paid => Outcome::Executed,
            payments::Outcome::Rejected(reason) => {
                Outcome::Rejected(Rejection::Payment(reason.clone()))
            }
        };
        return Ok((outcome, Some((request, receipt))));
    }
    let result = match action {
        Action::Contribute(_) | Action::Withdraw(_) | Action::Liquidate(_) => {
            return Err(SimulationError::InvalidComponentEffect {
                reason: "component request needs its modeled financial effects".into(),
            });
        }
        Action::Sell(request) => price(
            input,
            holdings,
            state,
            &request.agent_id,
            &request.asset_id,
            &request.cause_id,
        )
        .and_then(|price| {
            trades::execute_lot_sale(
                input,
                &mut state.ledger,
                &mut state.recorder,
                &mut state.lots,
                &mut state.tax,
                state.month,
                trades::SaleProceeds::Quoted(price),
                request,
            )
        }),
        Action::Buy(request) => price(
            input,
            holdings,
            state,
            &request.agent_id,
            &request.asset_id,
            &request.cause_id,
        )
        .and_then(|price| {
            trades::execute_purchase(
                input,
                &mut state.ledger,
                &mut state.recorder,
                &mut state.lots,
                state.month,
                price,
                request,
            )
        }),
        Action::Transfer(request) => transfers::execute_transfer(
            input,
            &mut state.ledger,
            &mut state.recorder,
            &mut state.tax,
            state.month,
            request,
            transfers::TransferContext {
                actor_id: Some(actor),
                income_category: None,
                deduction_category: None,
            },
        ),
        Action::PayClaim(_) | Action::Consume(_) => unreachable!("payments handled above"),
    };
    match result {
        Ok(()) => Ok((Outcome::Executed, None)),
        Err(
            error @ (SimulationError::InvalidTrade { .. }
            | SimulationError::InvalidTransfer { .. }
            | SimulationError::UnknownAccountReference { .. }),
        ) => Ok((
            Outcome::Rejected(Rejection::InvalidRequest(error.to_string())),
            None,
        )),
        Err(error) => Err(error),
    }
}

fn price(
    input: &ExecutionInput,
    holdings: &AgentHoldings,
    state: &RolloutState,
    requested_actor: &str,
    asset: &str,
    cause_id: &str,
) -> Result<PerUnit, SimulationError> {
    if requested_actor != holdings.agent_id() {
        return Err(SimulationError::InvalidTrade {
            cause_id: cause_id.into(),
            reason: "wrong actor".into(),
        });
    }
    match holdings.public_price(input, asset, state.rollout_id, state.month) {
        Ok(price) => Ok(price),
        Err(HoldingsError::MissingSeries { .. }) => Err(SimulationError::InvalidTrade {
            cause_id: cause_id.into(),
            reason: "asset has no declared public holding pool".into(),
        }),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn record_claims(recorder: &mut Recorder, claims: &claims::Claims) {
    for claim in claims.entries.iter().filter(|claim| !claim.paid) {
        recorder.record_obligation(ObligationOutcome {
            month: claims.month,
            cause_id: claim.cause_id.clone(),
            obligation_id: claim.cause_id.clone(),
            obligation_type: claim.obligation_type.clone(),
            from: claim.from.clone(),
            to: claim.to.clone(),
            amount_due: claim.amount_due,
            amount_paid: Money(0),
            shortfall: claim.amount_due,
            failure_active: claim.amount_due.0 > 0,
        });
    }
}
