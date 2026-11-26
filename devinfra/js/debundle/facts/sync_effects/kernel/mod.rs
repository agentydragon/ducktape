//! The one ordered local-operation kernel.
//!
//! `continuation` interprets a `LocalBody` as an explicit step stack that
//! suspends at every call; `driver::drive` resumes it with the adapter's call
//! result. Adapters implement only the narrow traits in `context`; everything
//! they would otherwise re-implement (operand order, aggregate literals,
//! lexical cells, closure creation and binding stores, own-data reads and writes,
//! base-class construction, path selection at conditional and logical
//! operators, completion accounting) lives here.

pub mod alternatives;
mod bindings;
pub(crate) mod branch;
pub mod cells;
pub mod closure;
pub mod construct;
pub mod context;
pub mod continuation;
pub mod driver;
mod field_access;
pub mod frame;
mod value_context;
