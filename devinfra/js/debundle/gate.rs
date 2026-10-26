//! Realizability gate and assignment validation.
//!
//! Builds on the `analysis` core (dependency graph, assignment, reports):
//! quotient the dependency graph into the module dependency graph used by
//! ESM import emission and linker-order reasoning, validate
//! realizability (`realizability`, `validation`), and assemble the
//! per-chunk assignment analysis (`chunk_analysis`,
//! `assignment_analysis`). The shared ESM import ordering consumed by
//! both the gate's simulator and the emitter lives in
//! `esm_import_order`.

mod assignment_analysis;
mod assignment_inputs;
mod counted_digraph;
mod esm_import_order;
mod realizability;
mod report_builders;
mod validation;

pub use assignment_analysis::{
    AssignmentAnalysis, AssignmentAnalysisBuildError, ValidatedAssignment,
};
pub use assignment_inputs::AssignmentInputs;
pub use esm_import_order::EsmImportOrder;
pub use realizability::{
    LadderDecision, ModuleAssignmentDelta, RealizabilityIndex, RealizabilityVerdict, SccDiagnosis,
    SccRejection, check_realizability, check_realizability_touching,
    simulated_evaluation_post_order,
};
pub use validation::{
    AssignmentReport, BlockingSccEntry, CycleEdge, CycleReport,
    render_atomic_group_conflict_summary, render_cycle_summary, validate_assignment,
};

#[cfg(test)]
mod analysis_tests;
