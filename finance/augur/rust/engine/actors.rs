//! One monthly household decision: scheduled facts, ordered actions, then closing.
//! Retained stepping for Python-owned batch policy loops; no native policy driver.

use super::*;
use serde::Serialize;

pub mod outcomes;
mod phases;
pub use super::target_allocation::{AllocationPlan, PendingAllocationBuy};
pub use phases::PathStatus;

/// Exact immediate-cash requests in declared public pools/assets, including empty pools.
/// Sequence order is execution order, not priority by type.
#[derive(Clone, Debug, Serialize)]
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
    fn cause_id(&self) -> &str {
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

/// Retain intent and result, including the failed request but never unattempted suffixes.
/// Month plus action index identify an occurrence even when cause labels are reused.
#[derive(Clone, Debug, Serialize)]
pub struct Receipt {
    pub month: u32,
    pub action_index: usize,
    pub action: Action,
    pub outcome: Outcome,
}

#[derive(Debug, Serialize)]
#[serde(tag = "kind")]
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
    pub rollout_id: u32,
    pub summary: outcomes::Summary,
    /// Absent when detailed capture was not requested, not an empty observed history.
    pub trace: Option<Trace>,
    pub stop: Option<Stop>,
}

#[derive(Debug)]
pub struct Trace {
    pub financial: RolloutOutput,
    /// The same financial records in the shared product/event vocabulary.
    pub event_frames: crate::event_frames::EventFrames,
    pub receipts: Vec<Receipt>,
}

impl Serialize for Trace {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        #[derive(Serialize)]
        struct Events<'a> {
            rollout_ids: [u32; 1],
            frames: &'a crate::event_frames::EventFrames,
        }
        #[derive(Serialize)]
        struct TraceOutput<'a> {
            events: Events<'a>,
            books: &'a [MonthOutput],
            journal: &'a [JournalEntry],
            bond_cashflows: &'a [BondCashflowOutcome],
            distributions: &'a [DistributionOutcome],
            receipts: &'a [Receipt],
        }
        TraceOutput {
            events: Events {
                rollout_ids: [self.financial.rollout_id],
                frames: &self.event_frames,
            },
            books: &self.financial.months,
            journal: &self.financial.journal,
            bond_cashflows: &self.financial.bond_cashflows,
            distributions: &self.financial.distributions,
            receipts: &self.receipts,
        }
        .serialize(serializer)
    }
}

/// Current actor books after scheduled cashflows and claim assembly. No future paths,
/// other actors' books or mutable execution state reach the decision function.
pub struct Observation<'a> {
    pub books: observations::ActorBooks<'a>,
    pub previous_receipts: &'a [Receipt],
    claims: &'a claims::Claims,
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
    previous_receipts: Vec<Receipt>,
    trace_receipts: Option<Vec<Receipt>>,
    capture: Option<outcomes::Capture>,
    stop: Option<Stop>,
    product_shortfall: Money,
}

enum Phase {
    New(Vec<Path>),
    Pending {
        paths: Vec<Path>,
        claims: Vec<Option<claims::Claims>>,
    },
    Finished(Vec<Path>),
    Closed,
}

/// One owned input and isolated path books, advanced only by the caller's monthly batch.
/// Routing/programming errors abort the session; a financial rejection stops its path.
pub struct Session {
    input: ExecutionInput,
    holdings: Option<AgentHoldings>,
    phase: Phase,
    configured: bool,
    product: Option<ProductInputs>,
    scopes: BTreeMap<String, AgentHoldings>,
    action_phase: phases::ActionPhase,
}

impl Session {
    pub fn new(
        input: ExecutionInput,
        actor: &str,
        rollout_ids: &[u32],
        capture_mode: CaptureMode,
    ) -> Result<Self, SimulationError> {
        Self::with_options(
            input,
            actor,
            rollout_ids,
            capture_mode,
            false,
            None,
            Vec::new(),
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn with_options(
        input: ExecutionInput,
        actor: &str,
        rollout_ids: &[u32],
        capture_mode: CaptureMode,
        configured: bool,
        product_actor: Option<&str>,
        component_observations: Vec<(u32, Vec<TlhPortfolioObservation>)>,
    ) -> Result<Self, SimulationError> {
        if configured {
            validate_fixture(&input)?;
        } else {
            validate(&input, actor)?;
        }
        let holdings = if configured {
            None
        } else {
            Some(AgentHoldings::resolve(&input, actor)?)
        };
        let product = product_actor
            .map(|actor| ProductInputs::resolve(&input, actor))
            .transpose()?;
        let scopes = input
            .scenario
            .accounts
            .iter()
            .map(|account| &account.account.agent_id)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .map(|actor| Ok((actor.clone(), AgentHoldings::resolve(&input, actor)?)))
            .collect::<Result<BTreeMap<_, _>, SimulationError>>()?;
        let component_ids: BTreeSet<_> = component_observations.iter().map(|(id, _)| *id).collect();
        if component_ids.len() != component_observations.len()
            || (!component_observations.is_empty()
                && component_ids != rollout_ids.iter().copied().collect())
        {
            return Err(SimulationError::InvalidRolloutSelection);
        }
        let component_observations: BTreeMap<_, _> = component_observations.into_iter().collect();
        let selected: BTreeSet<_> = rollout_ids.iter().copied().collect();
        if selected.is_empty()
            || selected.len() != rollout_ids.len()
            || selected.iter().any(|id| *id >= input.rollout_count)
        {
            return Err(SimulationError::InvalidRolloutSelection);
        }
        let paths = rollout_ids
            .par_iter()
            .map(|&rollout_id| {
                let mut state =
                    RolloutState::new(&input, rollout_id, capture_mode, product.as_ref())?;
                components::initialize(
                    &input,
                    &mut state,
                    component_observations
                        .get(&rollout_id)
                        .cloned()
                        .unwrap_or_default(),
                    product.as_ref(),
                )?;
                state.recorder.capture_taxes = !configured;
                let mut capture = (!configured).then(|| {
                    outcomes::Capture::new(&input, holdings.as_ref().expect("actor scope"))
                });
                if let Some(capture) = &mut capture {
                    capture.snapshot(&input, holdings.as_ref().expect("actor scope"), &state)?;
                }
                Ok(Path {
                    state,
                    previous_receipts: Vec::new(),
                    trace_receipts: (!configured && capture_mode.captures_output()).then(Vec::new),
                    capture,
                    stop: None,
                    product_shortfall: Money(0),
                })
            })
            .collect::<Result<_, SimulationError>>()?;
        Ok(Self {
            input,
            holdings,
            phase: Phase::New(paths),
            configured,
            product,
            scopes,
            action_phase: phases::ActionPhase::Awaiting,
        })
    }

    /// Apply scheduled facts and assemble the first claims exactly once.
    pub fn start(&mut self) -> Result<(), SimulationError> {
        let Phase::New(paths) = std::mem::replace(&mut self.phase, Phase::Closed) else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        self.prepare(paths)
    }

    fn prepare(&mut self, mut paths: Vec<Path>) -> Result<(), SimulationError> {
        if paths.iter().all(|path| path.state.is_finished(&self.input)) {
            self.phase = Phase::Finished(paths);
            return Ok(());
        }
        let claims = paths
            .par_iter_mut()
            .map(|path| {
                if path.state.is_finished(&self.input) {
                    Ok(None)
                } else {
                    if self.configured {
                        path.state.prepare_month_events(&self.input)?;
                        claims::assemble(
                            &self.input,
                            path.state.rollout_id,
                            path.state.month,
                            &path.state.properties,
                            &path.state.mortgages,
                            &path.state.tax_liabilities,
                        )
                        .map(Some)
                    } else {
                        path.state.prepare_month(&self.input).map(Some)
                    }
                }
            })
            .collect::<Result<Vec<_>, SimulationError>>()?;
        self.phase = Phase::Pending { paths, claims };
        self.action_phase = phases::ActionPhase::Awaiting;
        Ok(())
    }

    /// Borrow only current, scoped facts. Reads never reapply scheduled cashflows.
    pub fn decisions(&self) -> Result<Vec<Decision<'_>>, SimulationError> {
        self.decisions_for(
            self.holdings
                .as_ref()
                .ok_or(SimulationError::InvalidActorSessionState)?
                .agent_id(),
        )
    }

    pub fn decisions_for(&self, actor: &str) -> Result<Vec<Decision<'_>>, SimulationError> {
        if matches!(self.phase, Phase::Finished(_)) {
            return Ok(Vec::new());
        }
        if !self.configured
            && actor
                != self
                    .holdings
                    .as_ref()
                    .ok_or(SimulationError::InvalidActorSessionState)?
                    .agent_id()
        {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let scope = self
            .scopes
            .get(actor)
            .ok_or(SimulationError::InvalidActorSessionState)?;
        let Phase::Pending { paths, claims } = &self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        Ok(paths
            .iter()
            .zip(claims)
            .filter_map(|(path, claims)| {
                claims.as_ref().map(|claims| Decision {
                    rollout_id: path.state.rollout_id,
                    observation: Observation {
                        books: observations::ActorBooks {
                            scope,
                            books: observations::Books {
                                ledger: &path.state.ledger,
                                lots: &path.state.lots,
                                mortgages: &path.state.mortgages,
                                tax: &path.state.tax,
                                tax_liabilities: &path.state.tax_liabilities,
                                tlh_portfolios: &path.state.tlh_portfolios,
                            },
                            input: &self.input,
                            rollout: path.state.rollout_id,
                            month: path.state.month,
                        },
                        previous_receipts: &path.previous_receipts,
                        claims,
                    },
                })
            })
            .collect())
    }

    /// Test harness only; production month ownership is Python.
    #[cfg(test)]
    pub fn advance(&mut self, responses: Vec<DecisionActions>) -> Result<(), SimulationError> {
        self.begin_actions(
            responses
                .iter()
                .map(|response| (response.rollout_id, response.month))
                .collect(),
        )?;
        for response in responses {
            for action in response.actions {
                let receipt = self.apply(response.rollout_id, action)?;
                if matches!(receipt.outcome, Outcome::Rejected(_)) {
                    break;
                }
            }
        }
        self.end_actions()?;
        self.close_month()
    }

    pub fn is_finished(&self) -> bool {
        matches!(self.phase, Phase::Finished(_))
    }

    /// Consume terminal results in the caller's original selection order.
    pub fn finish(&mut self) -> Result<Vec<Rollout>, SimulationError> {
        let Phase::Finished(paths) = std::mem::replace(&mut self.phase, Phase::Closed) else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        paths
            .into_iter()
            .map(|mut path| {
                let summary = path
                    .capture
                    .ok_or(SimulationError::InvalidActorSessionState)?
                    .finish(
                        &self.input,
                        self.holdings
                            .as_ref()
                            .ok_or(SimulationError::InvalidActorSessionState)?
                            .agent_id(),
                        &mut path.state,
                        path.previous_receipts,
                    )?;
                let computation = path.state.finish(&self.input)?;
                Ok(Rollout {
                    rollout_id: computation.rollout_id,
                    summary,
                    trace: path.trace_receipts.map(|receipts| {
                        let financial = computation.into_output();
                        Trace {
                            event_frames: crate::event_frames::EventFrames::from_rollouts([
                                &financial,
                            ]),
                            financial,
                            receipts,
                        }
                    }),
                    stop: path.stop,
                })
            })
            .collect()
    }
}

fn finish_actions(path: &mut Path, holdings: &AgentHoldings, claims: &claims::Claims) {
    let month = path.state.month;
    if let Some(receipts) = &mut path.trace_receipts {
        receipts.extend(path.previous_receipts.iter().cloned());
    }
    let capture = path.capture.as_mut().expect("actor capture");
    capture.unpaid_claims = observations::due_claims(&claims, holdings.agent_id())
        .filter(|claim| claim.amount_due.0 > 0)
        .map(|claim| outcomes::UnpaidClaim {
            id: claim.id,
            cause_id: claim.cause_id.into(),
            obligation_type: claim.obligation_type.into(),
            from: claim.from.clone(),
            to: claim.to.clone(),
            amount_due: claim.amount_due,
        })
        .collect();
    let unpaid: Vec<_> = capture.unpaid_claims.iter().map(|claim| claim.id).collect();
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
}

fn validate(input: &ExecutionInput, actor: &str) -> Result<(), SimulationError> {
    validate_fixture(input)?;
    let scenario = &input.scenario;
    if !scenario.target_allocation_policies.is_empty()
        || !scenario.private_equity_tender_policies.is_empty()
        || !scenario.scheduled_sales.is_empty()
    {
        return Err(SimulationError::UnsupportedActorInput {
            reason:
                "configured allocation, tender policies and scheduled sales overlap actor decisions"
                    .into(),
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
            .holding_pools
            .iter()
            .any(|pool| private_equity_issuer(&pool.asset_id).is_some())
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
                String::new(),
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
            return Err(SimulationError::InvalidActorSessionState);
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
