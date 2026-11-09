//! Checked plans and the single commit for modeled native heap operations.
//!
//! Admission (`exact_native_call_row` plus operand checks) proves callee
//! identity and operand eligibility; `plan` then materializes every mutation
//! the row performs. Commit has no refusal path: every check that could fail
//! happened while constructing the plan, and a plan is never evidence that
//! the operation completed normally.

use super::super::property_inference::{OwnPropertyProjection, OwnPropertyProof};
use super::super::value_model::{AbstractValue, CallSite, Values};
use super::super::world::descriptor_heap::{
    DescriptorApplication, DescriptorHeap, DynamicKeyDefinition,
};
use crate::facts::abstract_domain::builtins::{ArgSel, Row};
use crate::facts::abstract_domain::heap::Mutation;
use crate::facts::abstract_domain::value::{Kind, Value};
use analysis_model::PrimKinds;
use std::collections::BTreeSet;

/// One heap mutation of an admitted row, fully materialized by `plan`.
#[derive(Debug, Clone)]
pub(in crate::facts::sync_effects) enum NativeOperationCommit {
    /// `SetIntegrityLevel(frozen)`: the literal writability witness of every
    /// object operand is gone. Values are unchanged.
    Integrity {
        targets: BTreeSet<u32>,
    },
    CaptureDescriptors {
        site: CallSite,
        projection: Box<super::super::property_inference::OwnPropertyDescriptorProjection>,
    },
    CreateNamesArray {
        allocation: u32,
        names: Vec<String>,
    },
    DefineProperties(DescriptorApplication),
    DefineDynamicKey(DynamicKeyDefinition),
}

/// A complete native operation after its callee, operands and current-prefix
/// preconditions have been checked, but before any mutation.
#[derive(Debug, Clone)]
pub(in crate::facts::sync_effects) struct CheckedNativePlan {
    expected_revision: u64,
    commits: Vec<NativeOperationCommit>,
    /// The fresh allocation the commits create, when the row returns one.
    fresh: Option<Values>,
}

/// The prefix changed between planning and execution; nothing was committed.
pub(in crate::facts::sync_effects) struct StalePlan;

/// Adapter-owned prefix state. Implementations apply a checked commit exactly
/// once and cannot turn it into a post-entry refusal.
pub(in crate::facts::sync_effects) trait NativeOperationContext {
    fn native_operation_revision(&self) -> u64;
    fn descriptor_heap(&mut self) -> DescriptorHeap<'_>;
}

/// Materialize an admitted row's mutations against the current operands.
/// `projection` is the admitted own-property proof of the inspected operand,
/// `application` and `definition` the admitted definitions onto their
/// targets; a row that needs one without having it was not admitted.
pub(in crate::facts::sync_effects) fn plan(
    row: &Row,
    site: CallSite,
    arguments: &[Values],
    projection: Option<&OwnPropertyProof>,
    application: Option<DescriptorApplication>,
    definition: Option<DynamicKeyDefinition>,
    revision: u64,
) -> Option<CheckedNativePlan> {
    let mut commits = Vec::new();
    let mut fresh = None;
    let mut application = application;
    let mut definition = definition;
    for mutation in row.mutates {
        match *mutation {
            Mutation::Integrity(ArgSel::Arg(index)) => {
                let targets = arguments
                    .get(index)?
                    .iter()
                    .filter_map(|value| match value {
                        AbstractValue::Value(Value::Alloc(kind, object))
                            if kind.tracks_prefix_own_properties() =>
                        {
                            Some(*object)
                        }
                        _ => None,
                    })
                    .collect();
                commits.push(NativeOperationCommit::Integrity { targets });
            }
            Mutation::DefineProperties { .. } => {
                commits.push(NativeOperationCommit::DefineProperties(application.take()?));
            }
            Mutation::DefineProperty { .. } => {
                commits.push(NativeOperationCommit::DefineDynamicKey(definition.take()?));
            }
            Mutation::FreshOwnDescriptorsOf(_) => {
                let OwnPropertyProjection::Descriptors(descriptors) = &projection?.projection
                else {
                    return None;
                };
                commits.push(NativeOperationCommit::CaptureDescriptors {
                    site,
                    projection: Box::new(descriptors.clone()),
                });
                fresh = Some(Values::from([AbstractValue::OwnDescriptorMap(site)]));
            }
            Mutation::FreshOwnKeysOf(_) => {
                let OwnPropertyProjection::Names(names) = &projection?.projection else {
                    return None;
                };
                let allocation = site.allocation_id();
                commits.push(NativeOperationCommit::CreateNamesArray {
                    allocation,
                    names: names.names.clone(),
                });
                fresh = Some(Values::from([AbstractValue::Value(Value::Alloc(
                    Kind::Object,
                    allocation,
                ))]));
            }
            // Retention stores the operand without mutating the prefix heap;
            // the fresh value is the caller's retained-callback projection.
            Mutation::FreshRetains(_) => {}
            Mutation::Integrity(ArgSel::Rest(_)) => return None,
        }
    }
    Some(CheckedNativePlan {
        expected_revision: revision,
        commits,
        fresh,
    })
}

/// The one commit implementation for every adapter: apply each materialized
/// mutation to the borrowed prefix heap.
pub(in crate::facts::sync_effects) fn commit(
    heap: &mut DescriptorHeap<'_>,
    commit: NativeOperationCommit,
) {
    match commit {
        NativeOperationCommit::Integrity { targets } => {
            for target in targets {
                heap.invalidate_writable(target);
            }
        }
        NativeOperationCommit::CaptureDescriptors { site, projection } => {
            heap.capture(site, *projection);
        }
        NativeOperationCommit::CreateNamesArray { allocation, names } => {
            let slots = names
                .into_iter()
                .map(|_| Values::from([AbstractValue::Value(Value::Prim(PrimKinds::STRING))]))
                .collect();
            heap.dense_array(allocation, slots);
        }
        NativeOperationCommit::DefineProperties(application) => heap.apply(&application),
        NativeOperationCommit::DefineDynamicKey(definition) => heap.define_dynamic_key(&definition),
    }
}

/// Reject a stale plan before touching the prefix; otherwise commit every
/// mutation once and return the fresh allocation's values, if the row has one.
pub(in crate::facts::sync_effects) fn execute(
    context: &mut impl NativeOperationContext,
    plan: CheckedNativePlan,
) -> Result<Option<Values>, StalePlan> {
    if plan.expected_revision != context.native_operation_revision() {
        return Err(StalePlan);
    }
    for mutation in plan.commits {
        commit(&mut context.descriptor_heap(), mutation);
    }
    Ok(plan.fresh)
}
