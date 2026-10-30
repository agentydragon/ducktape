//! The abstract domain the synchronous interpreter evaluates under: values,
//! primitive operator results, heap mutations, coercion steps, intrinsic-slot
//! integrity flags and the builtin summary table that describes every native
//! operation in those terms.

pub mod builtins;
pub mod effect;
pub mod foreign_call;
pub mod heap;
pub mod integrity;
pub mod primitive;
pub mod value;
