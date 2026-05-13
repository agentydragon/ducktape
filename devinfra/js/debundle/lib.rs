//! Module analysis engine for `materialize_logical_modules`.
//!
//! Background: see <DESIGN.md>. This crate treats debundling as an
//! owner-graph quotient and scheduling problem:
//!
//! 1. Analyze each source chunk into top-level owner facts: declarations,
//!    at-init reads/writes, lazy reads/writes, side effects, imports, source
//!    locations, and top-level await.
//! 2. Build a fine-grained owner graph over those facts.
//! 3. Map owners to destination modules from the spec.
//! 4. Quotient the owner graph into the module dependency graph used by ESM
//!    import emission and linker-order reasoning.
//! 5. Validate realizability, derive peelability, and emit reports from that
//!    same graph model.

mod atomic_units;
mod factor_assembly;
mod factorize;
mod facts;
mod graph;
mod ids;
mod partition;
mod peelability;
mod purity;
mod report_schema;
mod reports;
mod schedule;
mod validation;

pub use atomic_units::{AtomicUnit, compute_atomic_units};
pub use factor_assembly::{
    AssemblyOutcome, AtomicUnitConflict, ConflictingClaim, assemble_partition,
};
pub use factorize::build_factorize_report;
pub use facts::{
    ChunkFactAnalysis, StatementFacts, StatementKind, analyze_chunk_facts,
    analyze_chunk_facts_with_source_locations, analyze_chunk_with_source_locations,
    find_top_level_await,
};
pub use graph::{
    DepKind, EdgeMetadata, EdgeReason, ModuleQuotient, OwnerGraph, OwnerId, OwnerNode,
    build_module_quotient, build_owner_graph,
};
pub use ids::{
    BindingId, BindingKind, BindingName, BindingTable, ChunkId, ChunkTable, LogicalModule,
    LogicalModuleIndex, ModuleId, StatementOrdinal,
};
pub use partition::Partition;
pub use purity::{Purity, PurityReason, PurityRule, RedundantPurityHint, RedundantPurityReason};
pub use report_schema::{
    BindingReport, EvaluatedPeelCandidateReport, FactorizeCell, FactorizeOptions, FactorizeReport,
    ModuleReportRef, OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphPeelSetReport,
    OwnerGraphPeelabilityReport, OwnerGraphQuotientReport, OwnerGraphReport, PeelCandidateStatus,
    QuotientEdgeReport, QuotientSccReport, RESIDUAL_ENTRY_LABEL, RESIDUAL_ENTRY_MODULE_ID,
    ResidualOwnerCompanionOptionReport, ResidualOwnerPeelHorizonReport, ResidualOwnerPeelStatus,
    SourceLocation,
};
pub use schedule::Schedule;
pub use validation::{
    AtomicUnitClaimReport, AtomicUnitConflictReport, CycleEdge, CycleReport, ScheduleReport,
    render_atomic_unit_conflict_summary, render_cycle_summary, validate_schedule,
};

#[cfg(test)]
mod analysis_tests;
