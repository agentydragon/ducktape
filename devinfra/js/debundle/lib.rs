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

mod facts;
mod graph;
mod ids;
mod peelability;
mod purity;
mod report_schema;
mod reports;
mod schedule;
mod validation;

pub use facts::{
    ChunkFactAnalysis, StatementFacts, StatementKind, analyze_chunk_facts,
    analyze_chunk_facts_with_source_locations, analyze_chunk_with_source_locations,
    find_top_level_await,
};
pub use graph::{
    EdgeKind, EdgeMetadata, EdgeReason, ModuleDepGraph, OwnerGraph, OwnerId, OwnerNode,
    build_owner_graph, quotient_owner_graph,
};
pub use ids::{
    BindingId, BindingKind, BindingName, BindingTable, ChunkId, ChunkTable, LogicalModule,
    LogicalModuleIndex, ModuleId, StatementOrdinal,
};
pub use purity::{Purity, PurityReason, PurityRule};
pub use report_schema::{
    BindingReport, EvaluatedPeelCandidateReport, ModuleReportRef, OwnerGraphEdgeReport,
    OwnerGraphNodeReport, OwnerGraphPeelSetReport, OwnerGraphPeelabilityReport,
    OwnerGraphQuotientReport, OwnerGraphReport, PeelCandidateStatus, QuotientEdgeReport,
    QuotientSccReport, ResidualOwnerCompanionOptionReport, ResidualOwnerPeelHorizonReport,
    ResidualOwnerPeelStatus, SourceLocation,
};
pub use schedule::Schedule;
pub use validation::{
    CrossDestinationAssignmentReport, CycleEdge, CycleReport, ScheduleReport,
    render_cross_destination_assignment_summary, render_cycle_summary, validate_schedule,
};

#[cfg(test)]
mod analysis_tests;
