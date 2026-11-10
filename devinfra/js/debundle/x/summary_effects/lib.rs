//! Report-only prototype: a summary-based, flow-insensitive effect analysis
//! for one chunk, and the swapped-pair check over its effect sets.

mod bits;
pub mod effects;
pub mod generate;
mod helpers;
pub mod intrinsics;
pub mod model;
pub mod orders;
pub mod pairs;
pub mod premises;
pub mod program;
pub mod report;
pub mod tier0;
