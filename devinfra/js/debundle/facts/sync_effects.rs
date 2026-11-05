//! Synchronous call-effect analysis.

mod allocation_transfer;
mod array_transfer;
mod callable_body_facts;
mod descriptor_transfer;
mod effect_coverage;
pub(crate) use effect_coverage::{
    BindingEffectAuthority, BindingEffectCoverage, EffectCoverageFragment,
    SavedBindingEffectCoverage, SavedEffectCoverageFragment,
};
pub(crate) mod execution_index;
mod local_operations;
mod local_transfer;
mod namespace_wrapper;
pub(crate) mod noninvoking_initialization;
mod own_data_transfer;
mod property_inference;
mod value_model;
