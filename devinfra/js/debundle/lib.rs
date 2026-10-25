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
//! 3. Map owners to destination modules from the spec (`assignment`,
//!    `assignment_assembly`).
//! 4. Emit stable graph reports from that same graph model (`reports`).
//!
//! Realizability checking and factorization validation live in the
//! `gate` crate; the spec-independent per-chunk chunk-analysis
//! composer lives in the `chunk_analysis` crate. Both build on this core.

pub mod analysis_inputs;
pub mod assignment_assembly;
pub mod atomic_units;
pub mod cross_module_purity;
pub mod facts;
pub mod graph;
pub mod ids;
pub mod module_assignment;
pub mod purity;
pub mod reports;

pub use analysis_inputs::{AnalysisAssumptions, AnalysisInputs, KnownEffect, LocalEffectPolicy};
pub use assignment_assembly::{
    AssemblyOutcome, AtomicUnitConflict, ConflictingClaim, assemble_assignment,
};
pub use atomic_units::{
    AtomicUnit, OwnerGraphAndUnits, compute_atomic_units, compute_owner_graph_and_units_with,
};
pub use facts::{
    ChunkFactAnalysis, EffectCell, PositionBucketed, StatementEffectSummary, StatementFacts,
    StatementKind, StructuralChunkAnalysis, StructuralStatementFacts, analyze_chunk,
    analyze_chunk_structural, analyze_chunk_with_policy, analyze_vendor_prune_chunk,
    local_namespace_iife_target,
};
pub use graph::{
    ChunkConstrainingEdgeSet, DepKind, DuplicateTopLevelDeclaration, EdgeReason, EdgeRole,
    ModuleQuotient, OwnerEdge, OwnerEdgeId, OwnerGraph, OwnerGraphOptions, OwnerId, OwnerNode,
    UnresolvedOwnerEdgeEndpoint, build_module_quotient, build_owner_graph_with,
    chunk_constraining_module_edges, position_lookup,
};
pub use ids::{
    BindingKind, ChunkId, ChunkTable, ModuleId, PlannedModule, StatementOrdinal, top_level_id,
};
pub use module_assignment::ModuleAssignment;
pub use purity::{
    Purity, PurityReason, PurityRule, RedundantPureMemberHint, RedundantPureMemberReason,
    RedundantPurityHint, RedundantPurityReason,
};
pub use reports::schema::{
    AtomicGraphReport, AtomicUnitConflictReport, AtomicUnitEdgeReport, AtomicUnitReport,
    BindingReport, ConflictingClaimReport, EdgeRoleReport, LineRange, ModuleEntry, ModuleKey,
    OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport,
    PurityCauseReport, QuotientEdgeReport, QuotientSccReport, SequencedOwnerCause, SourceLocation,
};
