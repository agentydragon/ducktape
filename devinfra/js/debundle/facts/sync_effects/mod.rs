//! Synchronous call-effect analysis.

mod adapters;
mod alternatives;
pub mod execution_index;
mod foreign_call;
pub mod interpreter;
mod kernel;
mod legacy;
mod namespace_wrapper;
mod native;
pub(crate) mod noninvoking_initialization;
mod roots;
mod runtime_callbacks;
mod runtime_heap;
mod runtime_inference;
mod runtime_models;
mod strict_provider;
