//! One monthly household decision: scheduled facts, ordered actions, then closing.
//! This native scoped example retains forensic output, not a supported Python API.

use super::*;
use serde::Serialize;

/// Exact immediate-cash requests in public pools/assets declared by initial lots.
/// Sequence order is execution order, not priority by type. An all-cash input cannot
/// introduce a new asset through this scoped control.
#[derive(Clone, Debug, Serialize)]
pub enum Action {
    Sell(trades::SaleRequest),
    Buy(trades::PurchaseRequest),
    Transfer(transfers::TransferRequest),
    PayClaim(payments::PayClaim),
    Consume(payments::Consume),
}

impl Action {
    fn cause_id(&self) -> &str {
        match self {
            Self::Sell(request) => &request.cause_id,
            Self::Buy(request) => &request.cause_id,
            Self::Transfer(request) => &request.cause_id,
            Self::PayClaim(request) => &request.cause_id,
            Self::Consume(request) => &request.cause_id,
        }
    }
}

#[derive(Debug, Serialize)]
pub enum Rejection {
    InvalidRequest(String),
    Payment(payments::Rejection),
}

#[derive(Debug, Serialize)]
pub enum Outcome {
    /// Full execution of the exact request; canonical financial records carry its effects.
    Executed,
    Rejected(Rejection),
}

/// Retain intent and result, including the failed request but never unattempted suffixes.
/// Month plus action index identify an occurrence even when cause labels are reused.
#[derive(Debug, Serialize)]
pub struct Receipt {
    pub month: u32,
    pub action_index: usize,
    pub action: Action,
    pub outcome: Outcome,
}

#[derive(Debug, Serialize)]
pub enum Stop {
    /// The matching receipt contains the failed action and reason.
    RejectedAction { month: u32, action_index: usize },
    UnpaidClaims {
        month: u32,
        claims: Vec<claims::ClaimId>,
    },
}

#[derive(Debug, Serialize)]
pub struct Rollout {
    pub financial: RolloutOutput,
    pub receipts: Vec<Receipt>,
    pub stop: Option<Stop>,
}

/// Current actor books after scheduled cashflows and claim assembly. No future paths,
/// other actors' books or mutable execution state reach the decision function.
pub struct Observation<'a> {
    pub books: observations::ActorBooks<'a>,
    pub previous_receipts: &'a [Receipt],
    claims: &'a claims::Claims,
}

impl Observation<'_> {
    pub fn claims(&self) -> impl Iterator<Item = observations::Claim<'_>> {
        observations::due_claims(self.claims, self.books.agent_id())
    }
}

/// Routing is separate from actor information; original path IDs are stable across
/// selection/reordering. A policy must not use neighboring rows as economic information.
pub struct Decision<'a> {
    pub rollout_id: u32,
    pub observation: Observation<'a>,
}

pub struct DecisionActions {
    pub rollout_id: u32,
    pub month: u32,
    pub actions: Vec<Action>,
}

struct Path {
    state: RolloutState,
    receipts: Vec<Receipt>,
    previous: usize,
    stop: Option<Stop>,
}

/// One function shape: active monthly decision batch -> keyed ordered action lists.
/// The caller owns policy memory, keyed by original path identity. A scalar policy
/// may be adapted by the caller; the engine has no scalar policy entrypoint.
///
/// Every active path appears once. A rejected action or unpaid due claim stops only
/// that path. Invalid routing/input and arithmetic/accounting defects remain simulator
/// errors, not modeled action failures. This control retains forensic output.
pub fn simulate(
    input: &ExecutionInput,
    actor: &str,
    rollout_ids: &[u32],
    mut decide: impl FnMut(Vec<Decision<'_>>) -> Result<Vec<DecisionActions>, SimulationError>,
) -> Result<Vec<Rollout>, SimulationError> {
    validate(input, actor)?;
    let holdings = AgentHoldings::resolve(input, actor)?;
    let selected: BTreeSet<_> = rollout_ids.iter().copied().collect();
    if selected.is_empty()
        || selected.len() != rollout_ids.len()
        || selected.iter().any(|id| *id >= input.rollout_count)
    {
        return Err(SimulationError::InvalidRolloutSelection);
    }
    let mut paths = rollout_ids
        .par_iter()
        .map(|&rollout_id| {
            Ok(Path {
                state: RolloutState::new(input, rollout_id, CaptureMode::Forensic, None)?,
                receipts: Vec::new(),
                previous: 0,
                stop: None,
            })
        })
        .collect::<Result<Vec<_>, SimulationError>>()?;
    while paths.iter().any(|path| !path.state.is_finished(input)) {
        let prepared = paths
            .par_iter_mut()
            .map(|path| {
                if path.state.is_finished(input) {
                    Ok(None)
                } else {
                    path.state.prepare_month(input).map(Some)
                }
            })
            .collect::<Result<Vec<_>, SimulationError>>()?;
        let batch = paths
            .iter()
            .zip(&prepared)
            .filter_map(|(path, claims)| {
                claims.as_ref().map(|claims| Decision {
                    rollout_id: path.state.rollout_id,
                    observation: Observation {
                        books: observations::ActorBooks {
                            scope: &holdings,
                            books: observations::Books {
                                ledger: &path.state.ledger,
                                lots: &path.state.lots,
                                mortgages: &path.state.mortgages,
                                tax: &path.state.tax,
                                tax_liabilities: &path.state.tax_liabilities,
                                tlh_cumulative_harvest: &path.state.tlh_cumulative_harvest,
                            },
                            input,
                            rollout: path.state.rollout_id,
                            month: path.state.month,
                        },
                        previous_receipts: &path.receipts[path.previous..],
                        claims,
                    },
                })
            })
            .collect::<Vec<_>>();
        let expected: BTreeSet<_> = batch
            .iter()
            .map(|decision| (decision.rollout_id, decision.observation.books.month()))
            .collect();
        let responses = decide(batch)?;
        let actual: BTreeSet<_> = responses
            .iter()
            .map(|response| (response.rollout_id, response.month))
            .collect();
        if actual != expected || responses.len() != expected.len() {
            return Err(SimulationError::InvalidActorResponses);
        }
        let mut by_id: BTreeMap<_, _> = responses
            .into_iter()
            .map(|response| (response.rollout_id, response.actions))
            .collect();
        let routed: Vec<_> = paths
            .iter()
            .map(|path| by_id.remove(&path.state.rollout_id))
            .collect();
        paths
            .par_iter_mut()
            .zip(prepared)
            .zip(routed)
            .try_for_each(|((path, claims), actions)| match (claims, actions) {
                (Some(claims), Some(actions)) => advance(path, input, &holdings, claims, actions),
                (None, None) => Ok(()),
                _ => unreachable!("validated active decision routing"),
            })?;
    }
    paths
        .into_iter()
        .map(|path| {
            Ok(Rollout {
                financial: path.state.finish(input)?.into_output(),
                receipts: path.receipts,
                stop: path.stop,
            })
        })
        .collect()
}

fn advance(
    path: &mut Path,
    input: &ExecutionInput,
    holdings: &AgentHoldings,
    mut claims: claims::Claims,
    actions: Vec<Action>,
) -> Result<(), SimulationError> {
    let month = path.state.month;
    path.previous = path.receipts.len();
    for (action_index, action) in actions.into_iter().enumerate() {
        let outcome = execute(&mut path.state, input, holdings, &mut claims, &action)?;
        let failed = matches!(outcome, Outcome::Rejected(_));
        path.receipts.push(Receipt {
            month,
            action_index,
            action,
            outcome,
        });
        if failed {
            path.stop = Some(Stop::RejectedAction {
                month,
                action_index,
            });
            break;
        }
    }
    let unpaid: Vec<_> = observations::due_claims(&claims, holdings.agent_id())
        .filter(|claim| claim.amount_due.0 > 0)
        .map(|claim| claim.id)
        .collect();
    if path.stop.is_none() && !unpaid.is_empty() {
        path.stop = Some(Stop::UnpaidClaims {
            month,
            claims: unpaid,
        });
    }
    if path.stop.is_some() {
        path.state.failed_month = Some(month);
    }
    record_claims(&mut path.state.recorder, &claims);
    path.state.close_month(input, None, Money(0))
}

fn validate(input: &ExecutionInput, actor: &str) -> Result<(), SimulationError> {
    ValidatedInput::new(input)?;
    let scenario = &input.scenario;
    if !scenario.target_allocation_policies.is_empty()
        || !scenario.harvest_policies.is_empty()
        || !scenario.private_equity_tender_policies.is_empty()
        || !scenario.scheduled_sales.is_empty()
    {
        return Err(SimulationError::UnsupportedActorInput {
            reason: "configured allocation, harvesting, tender policies and scheduled sales overlap actor decisions".into(),
        });
    }
    if !scenario.scheduled_property_purchases.is_empty()
        || !scenario.scheduled_property_cashflows.is_empty()
        || !scenario.recurring_property_cashflows.is_empty()
        || !scenario.initial_primary_residences.is_empty()
        || !scenario.primary_residence_events.is_empty()
        || !scenario.property_rented_fraction_events.is_empty()
        || !scenario.capital_improvement_events.is_empty()
        || !scenario.property_sales.is_empty()
        || !scenario.mortgage_interest_deduction_policies.is_empty()
        || !scenario.property_tax_policies.is_empty()
        || !scenario.federal_salt_deduction_policies.is_empty()
        || scenario
            .initial_lots
            .iter()
            .any(|lot| private_equity_issuer(&lot.asset_id).is_some())
    {
        return Err(SimulationError::UnsupportedActorInput {
            reason: "the scoped actor control supports public securities, cash and due claims, not housing or private equity".into(),
        });
    }
    if scenario
        .obligations
        .iter()
        .any(|claim| claim.from.agent_id != actor)
        || scenario
            .recurring_obligations
            .iter()
            .any(|claim| claim.from.agent_id != actor)
        || scenario
            .tax_profiles
            .iter()
            .any(|profile| profile.agent_id != actor)
    {
        return Err(SimulationError::UnsupportedActorInput {
            reason: "only the decision-making household may have payment claims; counterparties use scheduled cashflows".into(),
        });
    }
    Ok(())
}

fn execute(
    state: &mut RolloutState,
    input: &ExecutionInput,
    holdings: &AgentHoldings,
    claims: &mut claims::Claims,
    action: &Action,
) -> Result<Outcome, SimulationError> {
    if action.cause_id().is_empty() {
        return Ok(Outcome::Rejected(Rejection::InvalidRequest(
            "empty cause ID".into(),
        )));
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
        if receipt.outcome == payments::Outcome::Paid {
            let (to, kind, claim_id) = match &request {
                payments::Request::PayClaim(request) => {
                    let claim = &claims.entries[request.claim.index];
                    (
                        &claim.to,
                        claim.obligation_type.as_str(),
                        claim.cause_id.as_str(),
                    )
                }
                payments::Request::Consume(request) => {
                    (&request.to, "cash_spend", request.component_id.as_str())
                }
            };
            state.recorder.record_obligation(ObligationOutcome {
                month: state.month,
                cause_id: request.cause_id().into(),
                obligation_id: claim_id.into(),
                obligation_type: kind.into(),
                from: request.from().clone(),
                to: to.clone(),
                amount_due: receipt.amount_requested,
                amount_paid: receipt.amount_paid(),
                shortfall: Money(0),
                attempted_funding_sources: String::new(),
                failure_active: false,
            });
        }
        return Ok(match receipt.outcome {
            payments::Outcome::Paid => Outcome::Executed,
            payments::Outcome::Rejected(reason) => Outcome::Rejected(Rejection::Payment(reason)),
        });
    }
    let result = match action {
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
                SaleTlh::Pool(&mut state.tlh_cumulative_harvest),
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
        Ok(()) => Ok(Outcome::Executed),
        Err(
            error @ (SimulationError::InvalidTrade { .. }
            | SimulationError::InvalidTransfer { .. }
            | SimulationError::UnknownAccountReference { .. }),
        ) => Ok(Outcome::Rejected(Rejection::InvalidRequest(
            error.to_string(),
        ))),
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
            reason: "asset is not a declared public holding".into(),
        }),
        Err(error) => Err(error.into()),
    }
}

fn record_claims(recorder: &mut Recorder, claims: &claims::Claims) {
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
            attempted_funding_sources: String::new(),
            failure_active: claim.amount_due.0 > 0,
        });
    }
}
