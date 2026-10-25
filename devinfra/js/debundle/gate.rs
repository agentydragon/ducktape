//! Realizability gate and factorization validation.
//!
//! Builds on the `analysis` core (owner graph, assignment, reports):
//! quotient the owner graph into the module dependency graph used by
//! ESM import emission and linker-order reasoning, validate
//! realizability (`realizability`, `validation`), and assemble the
//! per-chunk factorization state (`chunk_analysis`,
//! `chunk_factorization`). The shared ESM import ordering consumed by
//! both the gate's simulator and the emitter lives in
//! `esm_import_order`.

mod chunk_factorization;
mod counted_digraph;
mod esm_import_order;
mod factorization_inputs;
mod realizability;
mod report_builders;
mod validation;

pub use chunk_factorization::{ChunkFactorization, ValidatedAssignment};
pub use esm_import_order::EsmImportOrder;
pub use factorization_inputs::FactorizationInputs;
pub use realizability::{
    LadderDecision, ModuleAssignmentDelta, RealizabilityIndex, RealizabilityVerdict, SccDiagnosis,
    SccRejection, check_realizability, check_realizability_touching,
    simulated_evaluation_post_order,
};
pub use validation::{
    BlockingSccEntry, CycleEdge, CycleReport, FactorizationReport,
    render_atomic_group_conflict_summary, render_cycle_summary, validate_factorization,
};

#[cfg(test)]
mod analysis_tests;
