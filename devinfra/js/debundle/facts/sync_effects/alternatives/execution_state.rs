//! The application executor's path state and its join.
//!
//! `ExecutionState` holds three kinds of state beside the path state:
//! identity allocators, whose ids must stay unique across sibling leaves;
//! control state, which every leaf restores when its body completes; and
//! run-wide may-accumulators, where a superset only weakens a later proof or
//! adds a may-effect. Those stay live across leaves, as do the executor's
//! `Observations` (admission reports, consumed premises, author-supplied
//! prototype premises) and its report-only `evaluation_effects`. What a later
//! operation on the same path reads back as a fact is forked here and joined.

use super::super::allocation_transfer::AllocationJudgment;
use super::super::interpreter::{CallbackFallbackSummary, ModuleControlFrame, Outcome};
use super::super::world::World;
use super::super::world::join::{JoinRefusal, Joined};
use super::outcome::join_allocation_judgments;
use super::{PathState, join_worlds};
use crate::program_execution::RuntimeSourceScope;
use imbl::OrdMap;
use std::collections::BTreeSet;
use std::sync::Arc;

/// The path-owned part of `ExecutionState`, with the running outcome of the
/// activation that forked and the executor's allocation judgment.
#[derive(Clone)]
pub(in crate::facts::sync_effects) struct ExecutionPath {
    pub(in crate::facts::sync_effects) source_controls:
        OrdMap<RuntimeSourceScope, ModuleControlFrame>,
    pub(in crate::facts::sync_effects) world: Arc<World>,
    pub(in crate::facts::sync_effects) callback_fallback: CallbackFallbackSummary,
    pub(in crate::facts::sync_effects) allocation_judgment: AllocationJudgment,
    pub(in crate::facts::sync_effects) outcome: Outcome,
}

impl PathState for ExecutionPath {
    fn world(&self) -> &World {
        &self.world
    }
}

/// The path state after a body that ran exactly one of a family's leaves.
pub(in crate::facts::sync_effects) struct JoinedPath {
    pub(in crate::facts::sync_effects) path: ExecutionPath,
    /// The joined world cannot resume the body's caller; the adapter
    /// summarizes the remainder from `path`.
    pub(in crate::facts::sync_effects) refused: Option<JoinRefusal>,
}

impl ExecutionPath {
    pub(in crate::facts::sync_effects) fn join(leaves: Vec<Self>) -> JoinedPath {
        let Joined {
            world,
            widened: _,
            mut refused,
        } = join_worlds(leaves.iter().map(|leaf| leaf.world.as_ref()));
        let source_scopes: BTreeSet<_> = leaves
            .iter()
            .flat_map(|leaf| leaf.source_controls.keys().copied())
            .collect();
        let source_controls = source_scopes
            .into_iter()
            .filter_map(|scope| {
                let frames: Vec<_> = leaves
                    .iter()
                    .filter_map(|leaf| leaf.source_controls.get(&scope))
                    .collect();
                if frames.len() != leaves.len() {
                    refused = refused.or(Some(JoinRefusal::DivergentModuleFrames));
                }
                match ModuleControlFrame::join(&frames) {
                    Some(frame) => Some((scope, frame)),
                    None => {
                        refused = refused.or(Some(JoinRefusal::DivergentModuleFrames));
                        None
                    }
                }
            })
            .collect();
        let allocation_judgment =
            join_allocation_judgments(leaves.iter().map(|leaf| &leaf.allocation_judgment));
        // The fallback's once-per-statement memos hold only where every leaf
        // expanded: an owner one leaf skipped is expanded again, which
        // promotes the same effects into the joined outcome and world.
        let expanded = intersection(leaves.iter().map(|leaf| &leaf.callback_fallback.expanded));
        let incomplete_expanded = leaves
            .iter()
            .all(|leaf| leaf.callback_fallback.incomplete_expanded);
        let mut callback_fallback = CallbackFallbackSummary {
            expanded,
            incomplete_expanded,
            ..CallbackFallbackSummary::default()
        };
        let mut outcomes = Vec::with_capacity(leaves.len());
        for leaf in leaves {
            let ExecutionPath {
                source_controls: _,
                world: _,
                callback_fallback: leaf_fallback,
                allocation_judgment: _,
                outcome,
            } = leaf;
            let CallbackFallbackSummary {
                expanded: _,
                deferred,
                incomplete_expanded: _,
                reads,
                rebinds,
            } = leaf_fallback;
            for (binding, owner) in deferred {
                callback_fallback.deferred.entry(binding).or_insert(owner);
            }
            callback_fallback.reads.extend(reads);
            callback_fallback.rebinds.extend(rebinds);
            outcomes.push(outcome);
        }
        JoinedPath {
            path: ExecutionPath {
                source_controls,
                world: Arc::new(world),
                callback_fallback,
                allocation_judgment,
                outcome: Outcome::join(outcomes),
            },
            refused,
        }
    }
}

fn intersection<'a, T: Ord + Clone + 'a>(
    mut sets: impl Iterator<Item = &'a BTreeSet<T>>,
) -> BTreeSet<T> {
    let Some(first) = sets.next() else {
        return BTreeSet::new();
    };
    let mut joined = first.clone();
    for set in sets {
        joined.retain(|item| set.contains(item));
    }
    joined
}
