//! Types the per-chunk analysis consumes and reports: author inputs and
//! imported summaries, inference provenance, the report schema, the `Purity`
//! verdict, execution evidence, and the abstract domain's values and effects.
//!
//! These change far less often than the analysis passes in the `analysis`
//! crate, which build on them.

pub mod effect;
pub mod execution_evidence;
pub mod inputs;
pub mod provenance;
pub mod purity;
pub mod report_schema;
pub mod value;
