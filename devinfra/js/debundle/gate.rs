//! Realizability gate and factorization validation.
//!
//! Builds on the `analysis` core (owner graph, partition, reports):
//! quotient the owner graph into the module dependency graph used by
//! ESM import emission and linker-order reasoning, validate
//! realizability (`realizability`, `validation`), and assemble the
//! per-chunk factorization state (`chunk_analysis`,
//! `chunk_factorization`). The shared ESM import ordering consumed by
//! both the gate's simulator and the emitter lives in
//! `esm_import_order`.

mod chunk_analysis;
mod chunk_factorization;
mod esm_import_order;
mod realizability;
mod report_builders;
mod rollback_graph;
mod validation;

pub use chunk_analysis::ChunkAnalysis;
pub use chunk_factorization::ChunkFactorization;
pub use esm_import_order::EsmImportOrder;
pub use realizability::{
    CondensationOrder, CrossRebindEdge, DeltaHandle, LadderDecision, PartitionDelta,
    RealizabilityIndex, RealizabilityVerdict, SccDiagnosis, SccRejection, SccTimingReporter,
    check_realizability, check_realizability_touching, record_gate_diagnostic_translation,
    simulated_evaluation_post_order,
};
pub use rollback_graph::RollbackDiGraph;
pub use validation::{
    BlockingSccEntry, CycleEdge, CycleReport, FactorizationReport,
    render_atomic_unit_conflict_summary, render_cycle_summary, validate_factorization,
};

#[cfg(test)]
mod analysis_tests;
