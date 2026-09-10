//! Execute experiment-authored spending functions through the existing financial engine.
//!
//! One function instance belongs to each rollout. It observes the start of each live
//! month and requests nominal consumption; funding, realized gains, taxes and failure
//! remain engine responsibilities. This is a native Rust seam, not a Python callback API.

use super::*;
use serde::{Deserialize, Serialize};

pub mod batch;

/// Where consumption is paid to another actor, with an experiment-chosen event prefix.
/// Requests add to (never replace) the input's obligations.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Spending {
    pub from: AccountRef,
    pub to: AccountRef,
    pub cause_id: String,
}

/// Opening holdings valued at this month's prices, before monthly cashflows or sales.
/// No future paths or mutable financial state are supplied to the decision function.
pub struct Observation<'a> {
    pub month: u32,
    /// Cash across the payer agent's declared accounts, not just the funding account.
    pub cash: Money,
    /// Public securities, including bond funds; excludes individual held-to-maturity
    /// bonds, private equity and housing. This is gross value, not after-tax proceeds.
    pub public_holdings: Money,
    /// Current CPI / rollout-origin CPI. The function chooses its own reset cadence.
    pub price_level: Factor,
    /// Actor-scoped opening books, including accessible lots and already-booked
    /// contract/tax facts. This month's claims have not been assembled at this review.
    pub books: observations::ActorBooks<'a>,
}

/// Run forensic timelines using an ordinary function, constructed once per rollout.
///
/// `make_policy(rollout_id)` returns a stateful function of [`Observation`], whose result
/// is this month's requested nominal spending in input currency quanta. Zero requests
/// create no action. Negative requests and decision errors abort the simulation;
/// insufficient funds instead follow the existing per-rollout failure semantics.
///
/// Spending shares the all-or-none funding group of other obligations from its source
/// account: it does not introduce priority or partial settlement. Failed paths receive no
/// further callbacks. The factory must create independent state for reproducible rollouts.
pub fn simulate<Make, Decide>(
    input: &ExecutionInput,
    spending: &Spending,
    make_policy: Make,
) -> Result<SimulationOutput, SimulationError>
where
    Make: Fn(u32) -> Decide + Sync,
    Decide: FnMut(Observation) -> Result<Money, SimulationError>,
{
    let holdings = validate(input, spending)?;
    let rollouts: Result<Vec<_>, _> = (0..input.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            let mut decide = make_policy(rollout_id);
            let mut policy = Policy::new(spending, &holdings, &mut decide);
            simulate_rollout(
                input,
                rollout_id,
                CaptureMode::Forensic,
                None,
                Some(&mut policy),
            )
            .map(RolloutComputation::into_output)
        })
        .collect();
    Ok(SimulationOutput {
        schema_version: INPUT_SCHEMA_VERSION,
        rollouts: rollouts?,
    })
}

/// Compact amounts for the identified policy component, not total household consumption.
#[derive(Debug, Serialize)]
pub struct Summary {
    pub component: Spending,
    pub product_metrics: ProductMetricSeries,
    /// `[rollout][event month]` amounts in input currency quanta, including live zeros.
    /// Each path ends after its failure month, or after the last month of the horizon.
    /// There is no opening snapshot or post-stop padding in these consumption arrays.
    pub consumption_requested: Vec<Vec<Money>>,
    /// Actual receipts for the same demand. Other claims, trades and taxes are excluded.
    /// A different funding group's failure does not turn a paid demand into an unpaid one.
    pub consumption_paid: Vec<Vec<Money>>,
}

/// Run the same functions as [`simulate`] without retaining snapshots, journals or events.
///
/// Consumption arrays cover observed event months only; the unchanged product metrics
/// retain their snapshot-major layout and failure convention. Factory isolation,
/// validation and financial behavior are the same as [`simulate`].
pub fn simulate_summary<Make, Decide>(
    input: &ExecutionInput,
    spending: &Spending,
    make_policy: Make,
) -> Result<Summary, SimulationError>
where
    Make: Fn(u32) -> Decide + Sync,
    Decide: FnMut(Observation) -> Result<Money, SimulationError>,
{
    let holdings = validate(input, spending)?;
    let inputs = ProductInputs::resolve(input, &spending.from.agent_id)?;
    let rollouts: Result<Vec<_>, _> = (0..input.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            let mut decide = make_policy(rollout_id);
            let mut policy = Policy::new(spending, &holdings, &mut decide);
            simulate_rollout(
                input,
                rollout_id,
                CaptureMode::Summary,
                Some(&inputs),
                Some(&mut policy),
            )
            .map(|computation| {
                (
                    computation.product_metrics,
                    computation.failed_month,
                    computation.consumption_requested,
                    computation.consumption_paid,
                )
            })
        })
        .collect();
    let mut consumption_requested = Vec::new();
    let mut consumption_paid = Vec::new();
    let metrics: Vec<_> = rollouts?
        .into_iter()
        .map(|(metrics, failed_month, requested, paid)| {
            consumption_requested.push(requested);
            consumption_paid.push(paid);
            (metrics, failed_month)
        })
        .collect();
    Ok(Summary {
        component: spending.clone(),
        product_metrics: ProductMetricSeries::from_rollouts(
            input.scenario.horizon_months + 1,
            &metrics,
        )?,
        consumption_requested,
        consumption_paid,
    })
}

/// Replay one original path with fresh policy state and its original factory/series ID.
pub fn trace_rollout<Make, Decide>(
    input: &ExecutionInput,
    spending: &Spending,
    rollout_id: u32,
    make_policy: Make,
) -> Result<RolloutOutput, SimulationError>
where
    Make: FnOnce(u32) -> Decide,
    Decide: FnMut(Observation) -> Result<Money, SimulationError>,
{
    if rollout_id >= input.rollout_count {
        return Err(SimulationError::UnknownRollout {
            rollout_id,
            rollout_count: input.rollout_count,
        });
    }
    let holdings = validate(input, spending)?;
    let mut decide = make_policy(rollout_id);
    let mut policy = Policy::new(spending, &holdings, &mut decide);
    simulate_rollout(
        input,
        rollout_id,
        CaptureMode::Forensic,
        None,
        Some(&mut policy),
    )
    .map(RolloutComputation::into_output)
}

fn validate(input: &ExecutionInput, spending: &Spending) -> Result<AgentHoldings, SimulationError> {
    ValidatedInput::new(input)?;
    let accounts = input
        .scenario
        .accounts
        .iter()
        .map(|a| a.account.clone())
        .collect();
    validate_account(&accounts, &spending.from, "spending payer")?;
    validate_account(&accounts, &spending.to, "spending payee")?;
    validate_identifier("spending", &spending.cause_id)?;
    for rollout in 0..input.rollout_count {
        for month in 0..input.scenario.horizon_months {
            validate_amount_index_level(
                &spending.cause_id,
                "inflation",
                rollout,
                month,
                series_value(input, "inflation", rollout, month)?,
            )?;
        }
    }
    Ok(AgentHoldings::resolve(input, &spending.from.agent_id)?)
}

pub(super) struct Policy<'a> {
    spending: &'a Spending,
    holdings: &'a AgentHoldings,
    request: Request<'a>,
}

enum Request<'a> {
    Decide(&'a mut dyn FnMut(Observation) -> Result<Money, SimulationError>),
    Supplied(Money),
}

fn observe<'a>(
    holdings: &'a AgentHoldings,
    input: &'a ExecutionInput,
    rollout: u32,
    month: u32,
    books: observations::Books<'a>,
) -> Result<Observation<'a>, SimulationError> {
    let books = observations::ActorBooks {
        scope: holdings,
        books,
        input,
        rollout,
        month,
    };
    Ok(Observation {
        month,
        cash: books.cash()?,
        public_holdings: books.public_value()?,
        price_level: Factor::new(
            series_value(input, "inflation", rollout, month)?,
            series_value(input, "inflation", rollout, 0)?,
        ),
        books,
    })
}

impl<'a> Policy<'a> {
    pub(super) fn new(
        spending: &'a Spending,
        holdings: &'a AgentHoldings,
        decide: &'a mut dyn FnMut(Observation) -> Result<Money, SimulationError>,
    ) -> Self {
        Self {
            spending,
            holdings,
            request: Request::Decide(decide),
        }
    }

    pub(super) fn consumption(
        &mut self,
        input: &ExecutionInput,
        rollout: u32,
        month: u32,
        books: observations::Books<'_>,
    ) -> Result<Option<payments::Consume>, SimulationError> {
        let amount_due = match &mut self.request {
            Request::Decide(decide) => {
                decide(observe(self.holdings, input, rollout, month, books)?)?
            }
            Request::Supplied(amount) => *amount,
        };
        let cause_id = format!("{}_m{month}", self.spending.cause_id);
        if amount_due.0 < 0 {
            return Err(SimulationError::InvalidAmount {
                kind: "spending",
                cause_id,
                amount: amount_due.0,
            });
        }
        Ok((amount_due.0 > 0).then(|| payments::Consume {
            request_id: 0,
            cause_id,
            component_id: self.spending.cause_id.clone(),
            from: self.spending.from.clone(),
            to: self.spending.to.clone(),
            amount: amount_due,
        }))
    }
}
