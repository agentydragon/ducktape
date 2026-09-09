//! Rollout-local target decisions for one account-bound public-security allocation.
//!
//! The input owns the sleeve universe, source accounts, cash band, purchase permission
//! and rebalance tolerance. Functions choose only positive relative sleeve weights;
//! canonical execution still determines sales, funded demands, taxes and purchases.

use super::*;

/// Opening holdings at current prices, before this month's cashflows and settlement.
/// Values are gross, not after-tax liquidation proceeds. No future paths or mutable
/// books are supplied. The function owns its review cadence and decision memory.
pub struct Observation {
    pub month: u32,
    /// Cash in this allocation component's funding account, not agent-wide cash.
    pub cash: Money,
    /// Values across its source-account pools, in the input's fixed sleeve order.
    pub sleeve_values: Vec<Money>,
}

/// Run selected input rows in caller order, constructing fresh state for each row.
///
/// `make_policy(rollout_id)` returns a function producing one positive relative weight
/// per declared sleeve. Weights apply to this month's execution, not directly to books.
/// Cash-band activity suppresses quiet-band drift rebalancing: changing weights does
/// not promise immediate full reallocation. Purchases remain permission- and cash-limited.
/// Invalid decisions abort; failed funding stops that path's later callbacks.
///
/// For a selected replay, supply the same input row and a fresh factory. The factory
/// must not share decision state between paths or depend on invocation order. This
/// native-only entrypoint retains forensic receipts and holdings; it is not a Python
/// callback or compact-population interface.
pub fn simulate<Make, Decide>(
    input: &ExecutionInput,
    cash_account: &AccountRef,
    rollout_ids: &[u32],
    make_policy: Make,
) -> Result<SimulationOutput, SimulationError>
where
    Make: Fn(u32) -> Decide + Sync,
    Decide: FnMut(Observation) -> Result<Vec<i64>, SimulationError>,
{
    ValidatedInput::new(input)?;
    let policy_index = input
        .scenario
        .target_allocation_policies
        .iter()
        .position(|spec| {
            spec.agent_id == cash_account.agent_id && spec.account_id == cash_account.account_id
        })
        .ok_or_else(|| SimulationError::MissingTargetAllocationPolicy {
            agent_id: cash_account.agent_id.clone(),
            account_id: cash_account.account_id.clone(),
        })?;
    for sleeve in &input.scenario.target_allocation_policies[policy_index].sleeves {
        let series_id = format!("security:{}", sleeve.asset_id);
        if !input
            .series
            .iter()
            .any(|series| series.series_id == series_id)
        {
            return Err(SimulationError::MissingSeries { series_id });
        }
    }
    if rollout_ids.is_empty()
        || rollout_ids.iter().copied().collect::<BTreeSet<_>>().len() != rollout_ids.len()
        || rollout_ids.iter().any(|id| *id >= input.rollout_count)
    {
        return Err(SimulationError::InvalidRolloutSelection);
    }
    let rollouts: Result<Vec<_>, _> = rollout_ids
        .par_iter()
        .map(|&rollout_id| {
            let mut decide = make_policy(rollout_id);
            let mut policy = Policy {
                policy_index,
                weights: input.scenario.target_allocation_policies[policy_index]
                    .sleeves
                    .iter()
                    .map(|sleeve| sleeve.weight)
                    .collect(),
                decide: &mut decide,
            };
            simulate_rollout(
                input,
                rollout_id,
                CaptureMode::Forensic,
                None,
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

pub(super) struct Policy<'a> {
    pub(super) policy_index: usize,
    pub(super) weights: Vec<i64>,
    decide: &'a mut dyn FnMut(Observation) -> Result<Vec<i64>, SimulationError>,
}

impl Policy<'_> {
    pub(super) fn review(
        &mut self,
        input: &ExecutionInput,
        rollout: u32,
        month: u32,
        ledger: &Ledger,
        lots: &[LotState],
    ) -> Result<(), SimulationError> {
        let spec = &input.scenario.target_allocation_policies[self.policy_index];
        let weights = (self.decide)(Observation {
            month,
            cash: ledger.balance(&AccountRef::new(&spec.agent_id, &spec.account_id))?,
            sleeve_values: sleeve_holdings(input, rollout, month, lots, spec)?
                .iter()
                .map(|holding| Money(holding.value))
                .collect(),
        })?;
        crate::allocation::validate_weights(&weights, spec.sleeves.len())?;
        self.weights = weights;
        Ok(())
    }
}
