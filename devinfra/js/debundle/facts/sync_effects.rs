//! Synchronous call-effect analysis.

mod allocation_transfer;
mod array_transfer;
mod callable_body_facts;
mod completion;
mod descriptor_transfer;
mod effect_coverage;
pub(crate) use completion::{
    AbruptAlternative, Completion, NonNormalPossibilities, TransferLedger, UnknownThrowValues,
};
#[cfg(test)]
pub(crate) use effect_coverage::OperationStep;
pub(crate) use effect_coverage::{
    BindingEffectAuthority, BindingEffectCoverage, EffectCoverageFragment, LocalBodyIdentity,
    OperationSupportLocation, SavedBindingEffectCoverage, SavedEffectCoverageFragment,
    SavedOperationSupportReceipt,
};
pub use effect_coverage::{
    OperationSupportOperation, OperationSupportReceipt, SourceOperationSite,
};
pub(crate) mod execution_index;
mod local_operations;
mod local_transfer;
mod namespace_wrapper;
mod native_operation;
pub(crate) mod noninvoking_initialization;
mod object_surface;
mod own_data_transfer;
mod property_inference;
mod value_model;

/// Conservative footprint for a generic member store on a known heap origin.
/// Exact writable-data replacements use the typed same-prefix proof below;
/// this shared surface path keeps key and descriptor changes visible to every
/// producer that knows only the origin and static-key projection.
pub(crate) fn member_store_write_cells(
    origin: super::abstract_domain::value::HeapAddress,
    property: Option<String>,
) -> std::collections::BTreeSet<crate::facts::EffectCell> {
    own_data_transfer::OwnPropertyStorePostcondition::MayCreateOrRedefine { origin, property }
        .write_cells()
}
