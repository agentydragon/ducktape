//! Module analysis core for `materialize_logical_modules`.
//!
//! Background: see <docs/design.md>. This crate carries the shared
//! analysis substrate of the dependency-graph quotient model:
//!
//! 1. Analyze each source chunk into top-level source-unit facts: declarations,
//!    at-init reads/writes, lazy reads/writes, side effects, imports, source
//!    locations, and top-level await (`facts`, `purity`).
//! 2. Build a fine-grained dependency graph over those facts (`graph`,
//!    `atomic_groups`).
//! 3. Map source units to destination modules from the spec (`module_assignment`,
//!    `assignment_assembly`).
//! 4. Emit stable graph reports from that same graph model (`reports`).
//!
//! Realizability checking and assignment validation live in the
//! `gate` crate; the spec-independent per-chunk chunk-analysis
//! composer lives in the `chunk_analysis` crate. Both build on this core.

pub mod analysis_inputs;
pub mod assignment_assembly;
pub mod atomic_groups;
pub mod cross_module_purity;
pub mod facts;
pub mod graph;
pub mod ids;
pub mod module_assignment;
pub mod purity;
pub mod reports;

pub use analysis_inputs::{
    AnalysisAssumptions, AnalysisInputs, CallableSummary, CallbackFactoryEvidence, KnownEffect,
    LocalEffectPolicy,
};
pub use assignment_assembly::{
    AssemblyOutcome, AtomicGroupConflict, ConflictingClaim, assemble_assignment,
};
pub use atomic_groups::{
    AtomicGroup, DependencyGraphAndAtomicGroups, compute_atomic_groups,
    compute_dependency_graph_and_atomic_groups_with,
};
pub use facts::{
    CallbackFactoryBarrierFact, CallbackFactoryBarrierRejection, CallbackFactoryWriteWitness,
    ChunkFactAnalysis, EffectCell, PositionBucketed, StatementEffectSummary, StatementFacts,
    StatementKind, StructuralChunkAnalysis, StructuralStatementFacts, analyze_chunk,
    analyze_chunk_structural, analyze_chunk_with_policy, analyze_vendor_prune_chunk,
    local_namespace_iife_target,
};
pub use graph::{
    ChunkConstrainingEdgeSet, DepKind, DependencyEdge, DependencyEdgeId, DependencyGraph,
    DependencyGraphOptions, DuplicateTopLevelDeclaration, EdgeReason, EdgeRole, ModuleQuotient,
    SourceUnit, SourceUnitClass, SourceUnitId, UnresolvedDependencyEdgeEndpoint,
    build_dependency_graph_with, build_module_quotient, chunk_constraining_module_edges,
    position_lookup,
};
pub use ids::{
    BindingCatalogue, BindingId, BindingKind, BindingScopeError, ChunkId, ChunkTable,
    EntryDestination, LocalBindings, ModuleId, ModuleRole, PlannedModule, top_level_id,
};
pub use module_assignment::ModuleAssignment;
pub use purity::{
    Purity, PurityReason, PurityRule, RedundantPureMemberHint, RedundantPureMemberReason,
    RedundantPurityHint, RedundantPurityReason,
};
pub use reports::schema::{
    AtomicGraphReport, AtomicGroupConflictReport, AtomicGroupEdgeReport, AtomicGroupReport,
    BindingReport, CallbackFactoryBarrierReport, CallbackFactoryContractReport,
    CallbackFactoryRejectionReport, CallbackFactoryWriteWitnessReport, ConflictingClaimReport,
    DependencyEdgeReport, DependencyGraphReport, EdgeRoleReport, LineRange, ModuleDestinationKind,
    ModuleEntry, ModuleKey, ModuleQuotientReport, PurityCauseReport, QuotientEdgeReport,
    QuotientSccReport, SequencedSourceUnitCause, SourceLocation, SourceUnitReport,
};
