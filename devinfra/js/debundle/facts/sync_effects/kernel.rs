//! The one ordered local-operation kernel.
//!
//! `continuation` interprets a `LocalBody` as an explicit step stack that
//! suspends at every call; `driver::drive` resumes it with the adapter's call
//! result. Adapters implement only the narrow traits in `context`; everything
//! they would otherwise re-implement (operand order, aggregate literals,
//! binding stores, own-data reads and writes, completion accounting) lives
//! here.

pub(super) mod context;
pub(super) mod continuation;
pub(super) mod driver;
pub(super) mod frame;
