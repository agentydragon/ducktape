//! Module analysis core for `materialize_logical_modules`.
//!
//! Background: see <docs/design.md>. This crate analyzes each source chunk
//! into top-level source-unit facts: declarations, at-init reads/writes, lazy
//! reads/writes, side effects, imports, source locations, and top-level await
//! (`facts`, `purity`), plus the fact-level evidence reports (`reports`).
//!
//! The `graph` crate builds the dependency graph, atomic groups and module
//! assignment over these facts; realizability checking and assignment
//! validation live in the `gate` crate; the spec-independent per-chunk
//! composer lives in the `chunk_analysis` crate.

pub mod analysis_inputs;
pub mod analysis_provenance;
#[cfg(test)]
mod analysis_test_support;
pub mod diagnostics;
pub mod execution_evidence;
pub mod facts;
pub mod ids;
pub mod inference;
pub mod loader_closure;
pub mod program_bindings;
pub mod purity;
pub mod reports;

pub use analysis_inputs::{
    AnalysisAssumptions, AnalysisInputs, CallableSummary, CallbackFactoryEvidence,
    DeferredRecordMember, KnownEffect, LocalEffectPolicy, ReturnedRecordSummary,
};
pub use execution_evidence::SourceLocation;
pub use facts::{
    CallbackFactoryBarrierFact, CallbackFactoryBarrierRejection, CallbackFactoryWriteWitness,
    CellOverlap, CellOverlapIndex, CellOverlapLaw, ChunkFactAnalysis, DataflowOpacityCause,
    EffectCell, PositionBucketed, StatementEffectSummary, StatementFacts, StatementKind,
    StructuralChunkAnalysis, StructuralStatementFacts, analyze_chunk, analyze_chunk_structural,
    analyze_chunk_with_policy, analyze_vendor_prune_chunk, local_namespace_iife_target,
};
pub use ids::{
    BindingCatalogue, BindingId, BindingKind, BindingScopeError, EntryDestination,
    ImportedSourceTarget, LocalBindings, ModuleId, ModuleRole, PlannedModule, top_level_id,
};
pub use purity::{
    Purity, PurityReason, PurityRule, RedundantPureMemberHint, RedundantPureMemberReason,
    RedundantPurityHint, RedundantPurityReason,
};
