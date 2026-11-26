//! The part of the abstract domain that depends on value flow and statement
//! facts: chunk-top heap state. The value-free parts (builtin table,
//! primitive results, intrinsic-slot integrity) live in the `analysis_domain`
//! crate.

pub mod heap;
