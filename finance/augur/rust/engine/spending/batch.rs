//! Experimental owning handoff for Python-authored opening-month spending policies.
//!
//! This transports the existing spending control, not a general actor-action API. A
//! caller observes live paths, decides once per path/month, and supplies nominal requests.
//! Configured funding, taxes, capture and stop behavior use the same monthly evaluator.

use super::*;

#[derive(Debug, Error)]
pub enum BatchError {
    #[error(transparent)]
    Simulation(#[from] SimulationError),
    #[error("rollout {rollout_id}: {source}")]
    Advance {
        rollout_id: u32,
        source: SimulationError,
    },
    #[error("batch session is finished or aborted")]
    Closed,
    #[error("requests must identify distinct live selected paths at their current month")]
    InvalidRequests,
    #[error("cannot finish while selected paths remain live")]
    Unfinished,
}

/// Columnar opening observations; integer money uses the input currency quantum.
/// IDs are routing metadata, not policy features. Price level is numerator/denominator.
#[derive(Default)]
pub struct ObservationBatch {
    pub rollout_ids: Vec<u32>,
    pub months: Vec<u32>,
    pub cash: Vec<i64>,
    pub public_holdings: Vec<i64>,
    pub price_numerators: Vec<i64>,
    pub price_denominators: Vec<i64>,
}

/// Either compact component amounts/product metrics or the canonical forensic output.
pub enum Output {
    Summary(Summary),
    Forensic(SimulationOutput),
}

/// One immutable input beside retained books; never an input or book clone per month.
/// Errors close the whole prototype session, matching native spending-control errors.
/// Insufficient funding instead stops only that path. There is no retry operation.
pub struct Session {
    input: ExecutionInput,
    spending: Spending,
    holdings: AgentHoldings,
    product: Option<ProductInputs>,
    rollout_ids: Vec<u32>,
    states: Option<BTreeMap<u32, RolloutState>>,
}

impl Session {
    /// Select original input path IDs in output order. Forensic capture is for replay;
    /// compact capture retains the same requested/paid amounts as `simulate_summary`.
    pub fn new(
        input: ExecutionInput,
        spending: Spending,
        rollout_ids: Vec<u32>,
        forensic: bool,
    ) -> Result<Self, BatchError> {
        let holdings = validate(&input, &spending)?;
        if rollout_ids.is_empty()
            || rollout_ids.iter().any(|&id| id >= input.rollout_count)
            || rollout_ids.iter().collect::<BTreeSet<_>>().len() != rollout_ids.len()
        {
            return Err(SimulationError::InvalidRolloutSelection.into());
        }
        let (mode, product) = if forensic {
            (CaptureMode::Forensic, None)
        } else {
            (
                CaptureMode::Summary,
                Some(
                    ProductInputs::resolve(&input, &spending.from.agent_id)
                        .map_err(SimulationError::from)?,
                ),
            )
        };
        let states = rollout_ids
            .par_iter()
            .map(|&id| {
                RolloutState::new(&input, id, mode, product.as_ref()).map(|state| (id, state))
            })
            .collect::<Result<_, _>>()?;
        Ok(Self {
            input,
            spending,
            holdings,
            product,
            rollout_ids,
            states: Some(states),
        })
    }

    /// Observe only live paths in selection order. Reads do not advance the clock.
    pub fn observe(&mut self) -> Result<ObservationBatch, BatchError> {
        let states = self.states.take().ok_or(BatchError::Closed)?;
        let mut batch = ObservationBatch::default();
        for &id in &self.rollout_ids {
            let state = &states[&id];
            if state.is_finished(&self.input) {
                continue;
            }
            let observation = observe(
                &self.holdings,
                &self.input,
                id,
                state.month,
                observations::Books {
                    ledger: &state.ledger,
                    lots: &state.lots,
                    mortgages: &state.mortgages,
                    tax: &state.tax,
                    tax_liabilities: &state.tax_liabilities,
                    tlh_cumulative_harvest: &state.tlh_cumulative_harvest,
                },
            )?;
            batch.rollout_ids.push(id);
            batch.months.push(state.month);
            batch.cash.push(observation.cash.0);
            batch.public_holdings.push(observation.public_holdings.0);
            batch
                .price_numerators
                .push(observation.price_level.numerator());
            batch
                .price_denominators
                .push(observation.price_level.denominator());
        }
        self.states = Some(states);
        Ok(batch)
    }

    /// Advance each `(original path ID, observed month, requested quanta)` once.
    /// Rows may be reordered or chunked. Stale/duplicate/stopped IDs abort the session.
    pub fn advance(&mut self, requests: Vec<(u32, u32, i64)>) -> Result<(), BatchError> {
        let mut states = self.states.take().ok_or(BatchError::Closed)?;
        let mut seen = BTreeSet::new();
        for &(id, month, _) in &requests {
            if !seen.insert(id)
                || !states
                    .get(&id)
                    .is_some_and(|state| !state.is_finished(&self.input) && state.month == month)
            {
                return Err(BatchError::InvalidRequests);
            }
        }
        let selected: Vec<_> = requests
            .into_iter()
            .map(|(id, _, amount)| {
                (
                    id,
                    states.remove(&id).expect("validated request ID"),
                    amount,
                )
            })
            .collect();
        let advanced: Result<Vec<_>, BatchError> = selected
            .into_par_iter()
            .map(|(id, state, amount)| {
                let mut policy = Policy {
                    spending: &self.spending,
                    holdings: &self.holdings,
                    request: Request::Supplied(Money(amount)),
                };
                state
                    .advance_month(&self.input, self.product.as_ref(), Some(&mut policy))
                    .map(|state| (id, state))
                    .map_err(|source| BatchError::Advance {
                        rollout_id: id,
                        source,
                    })
            })
            .collect();
        states.extend(advanced?);
        self.states = Some(states);
        Ok(())
    }

    /// Consume all terminal states. Product columns follow the supplied selection order.
    pub fn finish(&mut self) -> Result<Output, BatchError> {
        let mut states = self.states.take().ok_or(BatchError::Closed)?;
        if states.values().any(|state| !state.is_finished(&self.input)) {
            return Err(BatchError::Unfinished);
        }
        let rollouts: Vec<_> = self
            .rollout_ids
            .iter()
            .map(|id| {
                states
                    .remove(id)
                    .expect("selected path retained")
                    .finish(&self.input)
            })
            .collect::<Result<_, _>>()?;
        if self.product.is_none() {
            return Ok(Output::Forensic(SimulationOutput {
                schema_version: INPUT_SCHEMA_VERSION,
                rollouts: rollouts
                    .into_iter()
                    .map(RolloutComputation::into_output)
                    .collect(),
            }));
        }
        let mut consumption_requested = Vec::new();
        let mut consumption_paid = Vec::new();
        let metrics: Vec<_> = rollouts
            .into_iter()
            .map(|rollout| {
                consumption_requested.push(rollout.consumption_requested);
                consumption_paid.push(rollout.consumption_paid);
                (rollout.product_metrics, rollout.failed_month)
            })
            .collect();
        Ok(Output::Summary(Summary {
            component: self.spending.clone(),
            product_metrics: ProductMetricSeries::from_rollouts(
                self.input.scenario.horizon_months + 1,
                &metrics,
            )
            .map_err(SimulationError::from)?,
            consumption_requested,
            consumption_paid,
        }))
    }
}
