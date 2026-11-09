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
use super::super::interpreter::{CallbackFallbackSummary, Outcome};
use super::super::world::World;
use super::super::world::join::{JoinRefusal, Joined};
use super::outcome::join_allocation_judgments;
use super::{PathState, join_worlds};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_ecma_ast::Id;

/// The path-owned part of `ExecutionState`, with the running outcome of the
/// activation that forked and the executor's allocation judgment.
#[derive(Clone)]
pub(in crate::facts::sync_effects) struct ExecutionPath {
    pub(in crate::facts::sync_effects) world: Arc<World>,
    pub(in crate::facts::sync_effects) callback_fallback: CallbackFallbackSummary,
    pub(in crate::facts::sync_effects) fallback_callback_sizes: Option<(usize, usize)>,
    pub(in crate::facts::sync_effects) fallback_outcome: Outcome,
    pub(in crate::facts::sync_effects) fallback_local_owners: BTreeSet<SourceUnitOrdinal>,
    pub(in crate::facts::sync_effects) fallback_local_deferred: BTreeMap<Id, SourceUnitOrdinal>,
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
            refused,
        } = join_worlds(leaves.iter().map(|leaf| leaf.world.as_ref()));
        let allocation_judgment =
            join_allocation_judgments(leaves.iter().map(|leaf| &leaf.allocation_judgment));
        // The fallback's once-per-statement memos hold only where every leaf
        // expanded: an owner one leaf skipped is expanded again, which
        // promotes the same effects into the joined outcome and world.
        let expanded = intersection(leaves.iter().map(|leaf| &leaf.callback_fallback.expanded));
        let incomplete_expanded = leaves
            .iter()
            .all(|leaf| leaf.callback_fallback.incomplete_expanded);
        let fallback_local_owners =
            intersection(leaves.iter().map(|leaf| &leaf.fallback_local_owners));
        // The sizes index the append-only addition lists. Leaves that agree
        // promoted the same number of additions; every addition of the joined
        // lists was promoted by some leaf, and promotions are unions in the
        // joined outcome and world. Leaves that disagree promote the whole
        // closure again.
        let fallback_callback_sizes = leaves
            .first()
            .and_then(|first| first.fallback_callback_sizes)
            .filter(|sizes| {
                leaves
                    .iter()
                    .all(|leaf| leaf.fallback_callback_sizes == Some(*sizes))
            });
        let mut callback_fallback = CallbackFallbackSummary {
            expanded,
            incomplete_expanded,
            ..CallbackFallbackSummary::default()
        };
        let mut fallback_local_deferred = BTreeMap::new();
        let mut fallback_outcomes = Vec::with_capacity(leaves.len());
        let mut outcomes = Vec::with_capacity(leaves.len());
        for leaf in leaves {
            let ExecutionPath {
                world: _,
                callback_fallback: leaf_fallback,
                fallback_callback_sizes: _,
                fallback_outcome,
                fallback_local_owners: _,
                fallback_local_deferred: leaf_deferred,
                allocation_judgment: _,
                outcome,
            } = leaf;
            let CallbackFallbackSummary {
                expanded: _,
                deferred,
                incomplete_expanded: _,
                reads: _,
                rebinds: _,
                read_additions,
                rebind_additions,
            } = leaf_fallback;
            // May-sets. Each addition list stays the insertion order of its
            // set, as the fallback's size bookkeeping requires.
            for (binding, owner) in deferred {
                callback_fallback.deferred.entry(binding).or_insert(owner);
            }
            for binding in read_additions {
                if callback_fallback.reads.insert(binding.clone()) {
                    callback_fallback.read_additions.push(binding);
                }
            }
            for binding in rebind_additions {
                if callback_fallback.rebinds.insert(binding.clone()) {
                    callback_fallback.rebind_additions.push(binding);
                }
            }
            for (binding, owner) in leaf_deferred {
                fallback_local_deferred.entry(binding).or_insert(owner);
            }
            fallback_outcomes.push(fallback_outcome);
            outcomes.push(outcome);
        }
        JoinedPath {
            path: ExecutionPath {
                world: Arc::new(world),
                callback_fallback,
                fallback_callback_sizes,
                fallback_outcome: Outcome::join(fallback_outcomes),
                fallback_local_owners,
                fallback_local_deferred,
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
