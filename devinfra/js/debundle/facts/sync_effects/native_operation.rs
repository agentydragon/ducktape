//! Checked and executed products for modeled native operations.

use super::allocation_transfer::AllocationJudgment;
use super::descriptor_transfer::{
    DescriptorApplication, NativeObjectPostcondition, PropertyAssignment,
};
use super::effect_coverage::EffectCoverageFragment;
use super::property_inference::OwnPropertyDescriptorProjection;
use super::value_model::{CallSite, Values};
use super::{Completion, TransferLedger};
use crate::analysis_inputs::{ParameterHeapRead, ProofDependencies};
use crate::facts::HeapContentCells;
use crate::facts::abstract_domain::value::{HeapAddress, Kind, SourceHeapResolver};

/// Values and any same-operation object-shape fact. The latter is refreshed
/// only after the operation has committed to the current prefix.
pub(super) struct NativeOperationResult {
    pub(super) values: Values,
    pub(super) postcondition: Option<Box<NativeObjectPostcondition>>,
}

/// References held in native internal slots, separate from ordinary own data.
/// The pooled allocation identity may represent several concrete instances, so
/// references are unioned and uncertainty is sticky.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct NativeRetainedReferences {
    pub(super) values: Values,
    pub(super) may_contain_unknown: bool,
}

impl NativeRetainedReferences {
    pub(super) fn weak_union(&mut self, other: &Self) {
        self.values.extend(other.values.iter().cloned());
        self.may_contain_unknown |= other.may_contain_unknown;
    }
}

/// A complete native operation after its receiver, operands, protocol and
/// current-prefix preconditions have been checked, but before any mutation.
/// This plan is not evidence that the operation completed normally.
pub(super) struct CheckedNativePlan {
    pub(super) expected_revision: u64,
    pub(super) commit: NativeOperationCommit,
    pub(super) result: NativeOperationResult,
    pub(super) effects: EffectCoverageFragment,
    pub(super) dependencies: Box<ProofDependencies>,
    pub(super) heap: HeapContentCells,
    pub(super) parameter_reads: std::collections::BTreeSet<ParameterHeapRead>,
    pub(super) allocation: AllocationJudgment,
}

/// Birth evidence comes from the checked commit, not from an address appearing
/// in a result or footprint. Consumers still prove invocation ownership and
/// escape/substitution before discharging accesses to these allocations.
#[derive(Clone, Copy)]
pub(super) enum NativePrivateBirth {
    DescriptorMap(HeapAddress),
    DenseArray(HeapAddress),
}

impl CheckedNativePlan {
    pub(super) fn private_birth(
        &self,
        resolver: SourceHeapResolver,
    ) -> Result<Option<NativePrivateBirth>, ()> {
        let birth = match &self.commit {
            NativeOperationCommit::None
            | NativeOperationCommit::DefineProperties(_)
            | NativeOperationCommit::AssignProperties(_) => None,
            NativeOperationCommit::CaptureDescriptors { site, .. } => {
                Some(NativePrivateBirth::DescriptorMap(
                    resolver.allocation(Kind::Object, site.allocation_id()),
                ))
            }
            NativeOperationCommit::CreateNamesArray { allocation, .. } => {
                Some(NativePrivateBirth::DenseArray(*allocation))
            }
            // This commit retains capabilities but supplies no allocation birth
            // or export correspondence for the internal-slot domain.
            NativeOperationCommit::RetainNativeReferences { .. } => return Err(()),
        };
        let expected: std::collections::BTreeSet<_> = birth
            .map(|birth| match birth {
                NativePrivateBirth::DescriptorMap(address) => {
                    crate::facts::EffectCell::DescriptorMapContents(address)
                }
                NativePrivateBirth::DenseArray(address) => {
                    crate::facts::EffectCell::ArrayContents(address)
                }
            })
            .into_iter()
            .collect();
        if self.heap.allocations != expected {
            return Err(());
        }
        Ok(birth)
    }
}

/// Mutations are fully materialized by preflight. Commit has no refusal path:
/// every check that could fail must have happened while constructing the plan.
pub(super) enum NativeOperationCommit {
    None,
    RetainNativeReferences {
        allocation: HeapAddress,
        values: Values,
    },
    CaptureDescriptors {
        site: CallSite,
        projection: OwnPropertyDescriptorProjection,
    },
    CreateNamesArray {
        allocation: HeapAddress,
        names: Vec<String>,
    },
    DefineProperties(DescriptorApplication),
    AssignProperties(PropertyAssignment),
}

pub(super) struct NativeOperationTransfer {
    pub(super) ledger: TransferLedger<NativeOperationResult, ()>,
    pub(super) heap: HeapContentCells,
    pub(super) parameter_reads: std::collections::BTreeSet<ParameterHeapRead>,
}

/// Adapter-owned prefix state. Implementations must apply a checked commit
/// exactly once and cannot turn it into a post-entry refusal.
pub(super) trait NativeOperationContext {
    fn native_operation_revision(&self) -> u64;
    fn commit_native_operation(&mut self, commit: NativeOperationCommit);
}

/// Reject a stale plan before touching the current prefix; otherwise commit
/// once, then publish the operation's completion, effects, dependencies and
/// postcondition as one transfer product.
pub(super) fn execute_native_plan(
    context: &mut impl NativeOperationContext,
    plan: CheckedNativePlan,
) -> Result<NativeOperationTransfer, ()> {
    if plan.expected_revision != context.native_operation_revision() {
        return Err(());
    }
    context.commit_native_operation(plan.commit);
    let mut result = plan.result;
    if let Some(mut postcondition) = result.postcondition.take() {
        postcondition.extend_dependencies(&plan.dependencies);
        result.postcondition =
            Some(postcondition.after_mutation(context.native_operation_revision()));
    }
    let mut effects = plan.effects;
    effects.depend_on(&plan.dependencies);
    Ok(NativeOperationTransfer {
        ledger: TransferLedger::new(
            Completion::Normal(result),
            effects,
            plan.dependencies,
            plan.allocation,
        ),
        heap: plan.heap,
        parameter_reads: plan.parameter_reads,
    })
}
