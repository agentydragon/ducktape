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
//!   `no_sync_callback_members` and builtin member-row exemptions).
//! - `collector` — the single-pass [`StatementFactsCollector`] and the
//!   inline-effect collector it delegates to.
//! - `lazy_boundary` — the [`LazyBoundary`] trait and the shared
//!   lazy/eager AST descent helpers.

pub(crate) use std::collections::{BTreeMap, BTreeSet};

pub(crate) use binding_targets::{
    TargetAccessRecorder, callee_base_expr, declaration_ids, hoisted_var_ids, record_assign_target,
    record_pat_write, record_update_target,
};
pub(crate) use serde::{Deserialize, Serialize};
pub(crate) use swc_common::{Span, Spanned};
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitWith};

pub(crate) use crate::analysis_inputs::{
    AnalysisInputs, KnownEffect, LocalEffectPolicy, ValidatedNativeOperationFacts,
};
pub(crate) use crate::execution_evidence::SourceLocation;
pub(crate) use crate::purity::{
    ChunkCodeGraph, Purity, RedundantPureMemberHint, RedundantPurityHint,
    classify_class_static_purity, classify_expr_purity, classify_var_decl_purity,
    detect_redundant_pure_member_hints, detect_redundant_purity_hints,
};

pub mod abstract_domain;
mod analyze;
mod array_spreads;
mod at_init_fallback;
#[cfg(test)]
mod at_init_fallback_tests;
mod callable_inference;
mod cell_overlap;
mod collector;
mod dynamic_scope;
mod evaluation_effects;
mod global_object;
pub(crate) mod heap_contents;
mod initialization_effects;
mod item_views;
mod lazy_boundary;
mod local_class_construction;
mod local_effect_targets;
mod local_effects;
mod purity_classification;
pub mod saved;
mod semantic_inference;
mod set_iterables;
mod source_unit_read_closure;
pub mod standard_globals;
mod statement_facts;
pub(crate) mod sync_effects;
mod value_flow;
#[cfg(test)]
mod value_flow_tests;
#[cfg(test)]
mod value_set_tests;

// Crate-internal re-exports: each submodule reaches its siblings'
// crate-internal items through `use super::*;`, which sees these globs.
pub(crate) use array_spreads::StableArraySpreadShapes;
pub(crate) use at_init_fallback::*;
pub(crate) use cell_overlap::may_conflict;
pub use cell_overlap::{CellOverlap, CellOverlapIndex, CellOverlapLaw};
pub(crate) use collector::*;
pub(crate) use dynamic_scope::DynamicScopeFact;
pub(crate) use global_object::*;
pub(crate) use item_views::*;
pub(crate) use lazy_boundary::*;
pub(crate) use local_class_construction::{
    LocalClassConstructionProof, LocalClassConstructionShapes, LocalClassHeapProof,
};
pub(crate) use local_effect_targets::*;
pub(crate) use purity_classification::*;
pub(crate) use semantic_inference::ArgumentNoEscapeFacts;
pub use set_iterables::certify_stable_set_spreads;
pub(crate) use set_iterables::{
    NativeIterableOperandOrigin, SetIterableProof, SetIterableShapes,
    TopLevelNativeIterableOperandProjection, TopLevelNativeIterableOperandSite,
    primitive_literal_property_keys,
};
pub(crate) use source_unit_read_closure::SourceUnitReadClosure;
pub use statement_facts::ConditionalProviderTargetRequirement;
pub use statement_facts::{
    CallbackFactoryBarrierFact, CallbackFactoryBarrierRejection,
    CallbackFactoryCallArgumentWitness, CallbackFactoryUseFact, CallbackFactoryWriteWitness,
    ReturnedRecordUseFact,
};
pub(crate) use statement_facts::{
    ConstructorFacts, ExportLinkage, HeapAccessOrigin, HeapAccessProvenance, HeapContentAccesses,
    HeapContentCells, LocalRecordFactoryPurityCertificate, SettledElements,
};
pub use sync_effects::{OperationSupportOperation, OperationSupportReceipt, SourceOperationSite};
pub(crate) use value_flow::{CandidateObjectIndex, ChunkValueFlow, own_data_keys};

/// Source locations from one canonical lowering of an already-prepared chunk.
/// Callable bounds include bodies with no operation sites; class roots remain
/// distinct from explicit constructor bodies. These locations establish source
/// correspondence, not that any operation executed.
pub struct CanonicalSourceInventory {
    pub source_sites: BTreeSet<SourceOperationSite>,
    pub callable_spans: BTreeMap<u32, Span>,
    pub class_spans: BTreeMap<u32, Span>,
}

pub fn canonical_source_inventory(
    source: &StructuralChunkAnalysis<'_>,
) -> CanonicalSourceInventory {
    sync_effects::execution_index::interpreter::canonical_source_inventory(source)
}

// Public API: importable at `facts::<item>` exactly as before the split.
pub use analyze::{
    ExportReentryPolicy, analyze_chunk, analyze_chunk_structural,
    analyze_chunk_structural_in_chunk, analyze_chunk_with_policy, analyze_vendor_prune_chunk,
    collect_chunk_source_facts, collect_chunk_source_facts_from_items,
    collect_chunk_source_facts_in_chunk, complete_chunk_structural,
    complete_chunk_structural_with_argument_inputs, complete_chunk_structural_with_inputs,
    complete_chunk_structural_with_native_allocations, module_has_top_level_await,
};
pub(crate) use analyze::{
    collect_chunk_source_facts_from_items_in_scope, complete_chunk_structural_with_flow,
    structural_value_flow,
};
pub use evaluation_effects::{
    GlobalAccessEvidence, GlobalAccessKind, GlobalAccessSite, GlobalMayAccess,
};
pub use initialization_effects::{
    InitializationEffectEvidence, InitializationEffects, OwnerInitializationCoverageRefusal,
    OwnerInitializationCoverageRefusalReason, UnknownEffectCoverage,
};
pub use item_views::{TopLevelItemView, top_level_item_views};
pub use local_effects::local_namespace_iife_target;
pub use statement_facts::{
    ChunkFactAnalysis, DataflowOpacityCause, EffectCell, LexicalCellId, PositionBucketed,
    RequiredImportedPrimitiveRead, StatementEffectSummary, StatementFacts, StatementKind,
    StructuralChunkAnalysis, StructuralChunkSourceFacts, StructuralStatementFacts,
};
