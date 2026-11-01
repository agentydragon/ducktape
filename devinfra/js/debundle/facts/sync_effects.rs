//! Synchronous call-effect analysis.

mod allocation_transfer;
pub(crate) mod execution_index;
mod namespace_wrapper;
pub(crate) mod noninvoking_initialization;
mod property_inference;
mod value_model;
