//! Module analysis engine for `materialize_logical_modules`.
//!
//! Background: see <docs/design.md>. This crate treats debundling as an
//! owner-graph quotient and scheduling problem:
//!
//! 1. Analyze each source chunk into top-level owner facts: declarations,
//!    at-init reads/writes, lazy reads/writes, side effects, imports, source
//!    locations, and top-level await.
//! 2. Build a fine-grained owner graph over those facts.
//! 3. Map owners to destination modules from the spec.
//! 4. Quotient the owner graph into the module dependency graph used by ESM
//!    import emission and linker-order reasoning.
//! 5. Validate realizability and emit stable graph reports from that same
//!    graph model. Agent-facing peel recommendation heuristics run from the
//!    serialized graph via the `debundle peel` CLI.

mod analysis_hints;
mod atomic_units;
mod chunk_analysis;
mod chunk_factorization;
mod factor_assembly;
mod facts;
mod graph;
mod ids;
mod partition;
mod purity;
mod realizability;
mod reports;
mod rollback_graph;
mod stage_one;
mod validation;

pub use analysis_hints::{AnalysisHints, KnownEffect, LocalEffectPolicy};
pub use atomic_units::{
    AtomicUnit, OwnerGraphAndUnits, compute_atomic_units, compute_owner_graph_and_units,
    compute_owner_graph_and_units_with,
};
pub use chunk_analysis::ChunkAnalysis;
pub use chunk_factorization::ChunkFactorization;
pub use factor_assembly::{
    AssemblyOutcome, AtomicUnitConflict, ConflictingClaim, assemble_partition,
};
pub use facts::{
    ChunkFactAnalysis, ChunkFactsReport, EffectCell, EffectCellReport, IdReport,
    StatementEffectSummary, StatementEffectSummaryReport, StatementFacts, StatementFactsReport,
    StatementKind, analyze_chunk, find_top_level_await, local_namespace_iife_target,
};
pub use graph::{
    ChunkConstrainingEdgeSet, DepKind, EdgeMetadata, EdgeReason, EdgeRole, ModuleQuotient,
    OwnerEdge, OwnerEdgeId, OwnerGraph, OwnerGraphOptions, OwnerId, OwnerNode, OwnerReportIndex,
    build_module_quotient, build_owner_graph, build_owner_graph_with,
    chunk_constraining_module_edges, chunk_linker_order, chunk_source_import_order,
};
pub use ids::{
    BindingKind, ChunkId, ChunkTable, LogicalModule, LogicalModuleIndex, ModuleId,
    StatementOrdinal, top_level_id,
};
pub use partition::Partition;
pub use purity::{
    Purity, PurityReason, PurityRule, RedundantPureMemberHint, RedundantPureMemberReason,
    RedundantPurityHint, RedundantPurityReason,
};
pub use realizability::{
    CrossRebindEdge, DeltaHandle, PartitionDelta, RealizabilityIndex, RealizabilityVerdict,
    SccDiagnosis, check_realizability, check_realizability_with_quotient,
};
pub use reports::schema::{
    AtomicGraphReport, AtomicUnitEdgeReport, AtomicUnitReport, BindingReport, EdgeRoleReport,
    FactorizeDiagnosticReason, LineRange, ModuleReportRef, OwnerGraphEdgeReport,
    OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport, PeelCandidateStatus,
    QuotientEdgeReport, QuotientSccReport, RESIDUAL_ENTRY_LABEL, RESIDUAL_ENTRY_MODULE_ID,
    SourceLocation,
};
pub use stage_one::{
    RebindFold, StageOneAnalysis, compute_rebind_folds, compute_stage_one_analysis,
};
pub use validation::{
    BlockingSccEntry, CycleEdge, CycleReport, FactorizationReport,
    render_atomic_unit_conflict_summary, render_cycle_summary, validate_factorization,
    validate_factorization_with_quotient,
};

#[cfg(test)]
mod analysis_tests;
