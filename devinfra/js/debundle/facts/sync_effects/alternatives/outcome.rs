//! The join of the outcomes of alternative paths: what a body that ran
//! exactly one of them may have done, and what it certainly did.

use super::super::allocation_transfer::AllocationJudgment;
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::interpreter::Outcome;
use super::super::kernel::frame::CallableSelection;
use crate::analysis_inputs::ProofDependencies;
use crate::facts::statement_facts::SynchronousCalls;

impl Outcome {
    /// Join the outcomes of every leaf of a family. May-facts (effects,
    /// returns, losses, executed owners, an unknown call) are unions; definite
    /// facts (`complete`, `body_invoked`, each coverage domain, a selected
    /// return) hold only when every leaf holds them.
    pub(in crate::facts::sync_effects) fn join(leaves: Vec<Self>) -> Self {
        let effect_coverage = EffectCoverageFragment::join_alternatives(
            &leaves
                .iter()
                .map(|leaf| &leaf.effect_coverage)
                .collect::<Vec<_>>(),
        );
        let return_selection = join_selections(
            leaves
                .iter()
                .map(|leaf| leaf.return_selection.as_ref())
                .collect(),
        );
        let mut leaves = leaves.into_iter();
        let mut joined = leaves.next().unwrap_or_default();
        for leaf in leaves {
            let Outcome {
                mut effects,
                body_invoked,
                // Joined above, over every leaf at once.
                effect_coverage: _,
                root_binding_effect_identity,
                returns,
                return_selection: _,
                complete,
                executed_owners,
                unknown_call,
            } = leaf;
            // Binding and heap may-effects, with their assumed-path
            // provenance: an effect one leaf observed is observed.
            joined.absorb_call_effects(&mut effects, None);
            join_reports(&mut joined, effects);
            joined.body_invoked &= body_invoked;
            if joined.root_binding_effect_identity != root_binding_effect_identity {
                joined.root_binding_effect_identity = None;
            }
            joined.returns.extend(returns);
            joined.complete &= complete;
            joined.executed_owners.extend(executed_owners);
            joined.unknown_call |= unknown_call;
        }
        joined.effect_coverage = effect_coverage;
        joined.return_selection =
            return_selection.filter(|selection| selection.matches(&joined.returns));
        joined
    }
}

/// The fields `Outcome::absorb_call_effects` leaves. Destructured so that a
/// new field without a join rule fails to compile.
fn join_reports(joined: &mut Outcome, leaf: SynchronousCalls) {
    let SynchronousCalls {
        // Written by owner finalization (`roots/`) after the owner's body
        // has joined: every leaf still holds the fork point's value.
        initializer_binding_effects: _,
        completed_initialization: _,
        initialization_coverage_refusal: _,
        root_attempt: _,
        purity_disagreement: _,
        local_record_factory_purity: _,
        local_record_factory_heap_call: _,
        proven_private_own_field_reads,
        binding_effect_coverage: _,
        settled_array_elements: _,
        global_object_aliases: _,
        // Moved out by `absorb_call_effects`.
        reads: _,
        rebinds: _,
        uninitialized_reads: _,
        fallback_only: _,
        heap_contents: _,
        non_assumed_heap_accesses: _,
        evaluation_effects,
        local_record_factory_uses,
        local_record_factory_rejections,
        complete,
        precision_losses,
        native_integrity_checks,
        native_prototype_property_checks,
        native_prefix_observations,
        native_integrity_uses,
        native_operation_input_uses,
        imported_primitive_array_spread_input_uses,
        inference_uses,
        callback_factory_uses,
        returned_record_uses,
        author_host_contract_uses,
    } = leaf;
    let effects = &mut joined.effects;
    // A conditional read is discharged only when every leaf certifies it.
    effects
        .proven_private_own_field_reads
        .retain(|read| proven_private_own_field_reads.contains(read));
    effects.evaluation_effects.extend(&evaluation_effects);
    effects.complete &= complete;
    union(
        &mut effects.local_record_factory_uses,
        local_record_factory_uses,
    );
    union(
        &mut effects.local_record_factory_rejections,
        local_record_factory_rejections,
    );
    union(
        &mut effects.native_integrity_checks,
        native_integrity_checks,
    );
    union(
        &mut effects.native_prototype_property_checks,
        native_prototype_property_checks,
    );
    union(
        &mut effects.native_prefix_observations,
        native_prefix_observations,
    );
    union(&mut effects.native_integrity_uses, native_integrity_uses);
    union(
        &mut effects.native_operation_input_uses,
        native_operation_input_uses,
    );
    union(
        &mut effects.imported_primitive_array_spread_input_uses,
        imported_primitive_array_spread_input_uses,
    );
    union(&mut effects.inference_uses, inference_uses);
    union(&mut effects.callback_factory_uses, callback_factory_uses);
    union(&mut effects.returned_record_uses, returned_record_uses);
    union(
        &mut effects.author_host_contract_uses,
        author_host_contract_uses,
    );
    // One record per reason and site, as for a nested call's losses.
    joined.inherit_completeness(true, precision_losses);
}

fn union<T: PartialEq>(joined: &mut Vec<T>, leaf: Vec<T>) {
    for item in leaf {
        if !joined.contains(&item) {
            joined.push(item);
        }
    }
}

/// The callable every leaf selected, under all of their conditions.
fn join_selections(selections: Vec<Option<&CallableSelection>>) -> Option<CallableSelection> {
    let selections: Vec<&CallableSelection> = selections.into_iter().collect::<Option<_>>()?;
    let first = *selections.first()?;
    let mut dependencies = ProofDependencies::default();
    for selection in &selections {
        if selection.target != first.target {
            return None;
        }
        dependencies.extend(&selection.dependencies);
    }
    Some(CallableSelection {
        target: first.target.clone(),
        dependencies: Box::new(dependencies),
    })
}

/// Allocation-only when every leaf is, under all of their conditions.
pub(in crate::facts::sync_effects) fn join_allocation_judgments<'a>(
    judgments: impl IntoIterator<Item = &'a AllocationJudgment>,
) -> AllocationJudgment {
    let mut dependencies = ProofDependencies::default();
    for judgment in judgments {
        let AllocationJudgment::CoveredAllocationOnly(covered) = judgment else {
            return AllocationJudgment::Opaque;
        };
        dependencies.extend(covered);
    }
    AllocationJudgment::CoveredAllocationOnly(Box::new(dependencies))
}
