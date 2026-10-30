//! The abstract domain the synchronous interpreter evaluates under: value
//! kinds, heap mutations, coercion steps and the builtin summary table that
//! describes every native operation in those terms.

pub mod builtins;
pub mod effect;
pub mod heap;
pub mod value;
