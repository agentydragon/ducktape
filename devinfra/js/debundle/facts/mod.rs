//! Per-chunk static facts: walk a chunk's top-level statements and
//! produce one [`StatementFacts`] per statement.
//!
//! The module is split by responsibility; submodules share the imports
//! and crate-internal items re-exported below via `pub(crate) use`, so
//! each submodule only needs `use super::*;`:
//!
//! - `statement_facts` — the public fact types ([`StatementFacts`],
//!   [`PositionBucketed`], [`EffectCell`], [`ChunkFactAnalysis`],
//!   [`StatementKind`]) and the internal structural-layer types.
//! - `analyze` — the `analyze_chunk{,_structural,_with_policy}` entry
//!   points, top-level-await scan, and per-statement fact assembly.
//! - `item_views` — top-level item views, multi-declarator split,
//!   declaration/classification helpers, shadowed-global computation.
//! - `global_object` — global-object alias detection and escape taint.
//! - `purity_classification` — per-statement purity classification.
//! - `local_effect_targets` — recognized local-effect target detection
//!   (TypeScript decorate helpers) and policy dispatch.
//! - `at_init_fallback` — at-init unresolved-call fallback source
//!   pruning (safe plain-array chains, opaque-call/inline-fn finders,
//!   no-sync-callback member recognition).
//! - `collector` — the single-pass [`StatementFactsCollector`] and the
//!   inline-effect collector it delegates to.
//! - `lazy_boundary` — the [`LazyBoundary`] trait and the shared
//!   lazy/eager AST descent helpers.

pub(crate) use std::collections::{BTreeMap, BTreeSet};

pub(crate) use binding_targets::{
    TargetAccessRecorder, callee_base_expr, declaration_ids, hoisted_var_ids, record_assign_target,
    record_pat_write, record_update_target, strip_parens,
};
pub(crate) use serde::{Deserialize, Serialize};
pub(crate) use swc_common::{Span, Spanned};
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitWith};

pub(crate) use crate::analysis_hints::{AnalysisHints, KnownEffect, LocalEffectPolicy};
pub(crate) use crate::purity::{
    ChunkCodeGraph, Purity, PurityReason, PurityRule, RedundantPureMemberHint, RedundantPurityHint,
    SHADOW_TRACKED_GLOBALS, class_has_static_observable, classify_expr_purity,
    classify_var_decl_purity, detect_redundant_pure_member_hints, detect_redundant_purity_hints,
};
pub(crate) use crate::{SourceLocation, StatementOrdinal};

mod analyze;
mod at_init_fallback;
mod collector;
mod global_object;
mod item_views;
mod lazy_boundary;
mod local_effect_targets;
mod local_effects;
mod purity_classification;
mod statement_facts;
pub mod wire;

// Crate-internal re-exports: each submodule reaches its siblings'
// crate-internal items through `use super::*;`, which sees these globs.
pub(crate) use at_init_fallback::*;
pub(crate) use collector::*;
pub(crate) use global_object::*;
pub(crate) use item_views::*;
pub(crate) use lazy_boundary::*;
pub(crate) use local_effect_targets::*;
pub(crate) use purity_classification::*;
pub(crate) use statement_facts::*;

// Public API: importable at `facts::<item>` exactly as before the split.
pub use analyze::{analyze_chunk, find_top_level_await};
pub use item_views::{TopLevelItemView, top_level_item_views};
pub use local_effects::local_namespace_iife_target;
pub use statement_facts::{
    ChunkFactAnalysis, EffectCell, PositionBucketed, StatementEffectSummary, StatementFacts,
    StatementKind,
};
pub use wire::{ChunkFactsReport, IdReport, StatementFactsReport};
