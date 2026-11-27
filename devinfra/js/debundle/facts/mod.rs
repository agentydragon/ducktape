//! Per-chunk static facts: walk a chunk's top-level statements and
//! produce one [`StatementFacts`] per statement.
//!
//! The module is split by responsibility; submodules share the imports
//! and crate-internal items re-exported below via `pub(crate) use`, so
//! each submodule only needs `use super::*;`:
//!
//! - `statement_facts` — the public fact types ([`StatementFacts`],
//!   [`EffectCell`], [`ChunkFactAnalysis`]) and the internal
//!   structural-layer types.
//! - `analyze` — the `analyze_chunk{,_structural,_with_policy}` entry
//!   points, top-level-await scan, and per-statement fact assembly.
//! - `global_object` — global-object alias detection and escape taint.
//! - `purity_classification` — per-statement purity classification.
//! - `local_effect_targets` — recognized local-effect target detection
//!   (TypeScript decorate helpers) and policy dispatch.
//! - `at_init_fallback` — at-init unresolved-call fallback source
//!   pruning (safe plain-array chains, opaque-call/inline-fn finders,
//!   `no_sync_callback_members` and builtin member-row exemptions).
//! - `collector` — the single-pass [`StatementFactsCollector`] and the
//!   inline-effect collector it delegates to.
//!
//! Top-level item views, lazy/eager position tracking and the other
//! syntactic domain helpers live in the `analysis_domain` crate; the
//! prelude below globs its `item_views` and `lazy_boundary` modules.

pub(crate) use std::collections::{BTreeMap, BTreeSet};

pub(crate) use binding_targets::{
    TargetAccessRecorder, callee_base_expr, record_assign_target, record_pat_write,
    record_update_target,
};
pub(crate) use serde::{Deserialize, Serialize};
pub(crate) use swc_common::{Span, Spanned};
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitWith};

pub(crate) use analysis_domain::item_views::*;
pub(crate) use analysis_domain::lazy_boundary::*;
pub(crate) use analysis_domain::{
    impl_lazy_callable_visits, impl_lazy_class_visits, impl_lazy_optional_path_visits,
};
pub(crate) use analysis_vocabulary::execution_evidence::{SourceLocation, SourceLocator};
pub(crate) use analysis_vocabulary::inputs::{
    AnalysisInputs, KnownEffect, LocalEffectPolicy, ValidatedNativeOperationFacts,
};
pub(crate) use analysis_vocabulary::purity::Purity;
pub(crate) use purity::{
    ChunkCodeGraph, LocalClassConstructionShapes, LocalClassHeapProof, PurityScope,
    RedundantPureMemberHint, RedundantPurityHint, SetIterableProof, SetIterableShapes,
    StableArraySpreadShapes, classify_class_static_purity, classify_expr_purity,
    classify_var_decl_purity,
};

pub mod analyze;
pub mod at_init_fallback;
pub mod binding_effect_coverage;
pub mod cell_overlap;
pub mod collector;
pub mod evaluation_effects;
pub mod flow_calls;
pub mod global_object;
pub mod heap_contents;
pub mod initialization_effects;
pub mod local_effect_targets;
pub mod local_effects;
pub mod purity_classification;
pub mod saved;
pub mod source_unit_read_closure;
pub mod standard_globals;
pub mod statement_facts;

// Crate-internal re-exports: each submodule reaches its siblings'
// crate-internal items through `use super::*;`, which sees these globs.
pub(crate) use at_init_fallback::*;
pub(crate) use collector::*;
pub(crate) use global_object::*;
pub(crate) use local_effect_targets::*;
pub(crate) use statement_facts::{ConstructorFacts, ExportLinkage};
pub(crate) use value_flow::{CandidateObjectIndex, ChunkValueFlow};

// CLEANUP(added 2026-11-27): Remove these re-exports and point
//   `chunk_analysis/mod.rs` and `facts/sync_effects/execution_index/mod.rs`
//   at the defining modules once the open PRs touching those files
//   (#20495, #20532, #20539, #20545) land.
pub use analyze::{ExportReentryPolicy, collect_chunk_source_facts, complete_chunk_structural};
pub use initialization_effects::OwnerInitializationCoverageRefusalReason;
pub use statement_facts::{ChunkFactAnalysis, StructuralChunkAnalysis};
