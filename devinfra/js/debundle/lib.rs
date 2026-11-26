//! Module analysis core for `materialize_logical_modules`.
//!
//! Background: see <docs/design.md>. This crate holds the structural layer
//! of per-chunk analysis: top-level source-unit facts (declarations, at-init
//! reads/writes, lazy reads/writes, side effects, imports, source locations,
//! top-level await), value flow, purity rules and the fact-level evidence
//! reports (`facts`, `purity`, `reports`). The types these passes consume
//! and report live below it in the `analysis_vocabulary` crate.
//!
//! The `sync_effects` crate (`facts/sync_effects/`) runs the synchronous
//! interpreter over this layer; the `analyze` crate (`analyze/`) combines both
//! into `analyze_chunk`. The `graph` crate builds the dependency graph, atomic groups and module
//! assignment over these facts; realizability checking and assignment
//! validation live in the `gate` crate; the spec-independent per-chunk
//! composer lives in the `chunk_analysis` crate.

pub mod diagnostics;
pub mod facts;
pub mod ids;
pub mod loader_closure;
pub mod program_bindings;
pub mod purity;
pub mod reports;

pub use facts::{
    CallbackFactoryBarrierFact, CallbackFactoryBarrierRejection, CallbackFactoryWriteWitness,
    CellOverlap, CellOverlapIndex, CellOverlapLaw, ChunkFactAnalysis, DataflowOpacityCause,
    EffectCell, PositionBucketed, StatementEffectSummary, StatementFacts, StatementKind,
    StructuralChunkAnalysis, StructuralStatementFacts, analyze_chunk_structural,
    local_namespace_iife_target,
};
pub use ids::{
    BindingCatalogue, BindingId, BindingKind, BindingScopeError, EntryDestination,
    ImportedSourceTarget, LocalBindings, ModuleId, ModuleRole, PlannedModule, top_level_id,
};
pub use purity::{
    RedundantPureMemberHint, RedundantPureMemberReason, RedundantPurityHint, RedundantPurityReason,
};
