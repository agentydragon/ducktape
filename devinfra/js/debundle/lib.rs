//! Module analysis core for `materialize_logical_modules`.
//!
//! Background: see <docs/design.md>. This crate carries the shared
//! analysis substrate of the owner-graph quotient model:
//!
//! 1. Analyze each source chunk into top-level owner facts: declarations,
//!    at-init reads/writes, lazy reads/writes, side effects, imports, source
//!    locations, and top-level await (`facts`, `purity`).
//! 2. Build a fine-grained owner graph over those facts (`graph`,
//!    `atomic_units`).
//! 3. Map owners to destination modules from the spec (`partition`,
//!    `factor_assembly`).
//! 4. Emit stable graph reports from that same graph model (`reports`).
//!
//! Realizability checking and factorization validation live in the
//! `gate` crate; the spec-independent per-chunk Stage A composer lives
//! in the `stage_one` crate. Both build on this core.

pub mod analysis_hints;
pub mod atomic_units;
pub mod factor_assembly;
pub mod facts;
pub mod graph;
pub mod ids;
pub mod partition;
pub mod purity;
pub mod reports;

pub use analysis_hints::{AnalysisHints, KnownEffect, LocalEffectPolicy};
pub use atomic_units::{
    AtomicUnit, OwnerGraphAndUnits, compute_atomic_units, compute_owner_graph_and_units,
    compute_owner_graph_and_units_with,
};
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
    position_lookup,
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
pub use reports::schema::{
    AtomicGraphReport, AtomicUnitConflictReport, AtomicUnitEdgeReport, AtomicUnitReport,
    BindingReport, ConflictingClaimReport, EdgeRoleReport, FactorizeDiagnosticReason, LineRange,
    ModuleEntry, ModuleKey, OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphQuotientReport,
    OwnerGraphReport, PeelCandidateStatus, QuotientEdgeReport, QuotientSccReport, SourceLocation,
};
