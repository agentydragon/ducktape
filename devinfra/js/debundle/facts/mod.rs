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

pub(crate) use crate::purity::{
    ChunkCodeGraph, RedundantPureMemberHint, RedundantPurityHint, classify_class_static_purity,
    classify_expr_purity, classify_var_decl_purity,
};
pub(crate) use analysis_vocabulary::execution_evidence::{SourceLocation, SourceLocator};
pub(crate) use analysis_vocabulary::inputs::{
    AnalysisInputs, KnownEffect, LocalEffectPolicy, ValidatedNativeOperationFacts,
};
pub(crate) use analysis_vocabulary::purity::Purity;

macro_rules! impl_lazy_callable_visits {
    () => {
        fn visit_function(&mut self, node: &Function) {
            lazy_visit_function(self, node);
        }
        fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
            lazy_visit_arrow_expr(self, node);
        }
        fn visit_method_prop(&mut self, node: &MethodProp) {
            lazy_visit_method_prop(self, node);
        }
        fn visit_getter_prop(&mut self, node: &GetterProp) {
            lazy_visit_getter_prop(self, node);
        }
        fn visit_setter_prop(&mut self, node: &SetterProp) {
            lazy_visit_setter_prop(self, node);
        }
    };
}

macro_rules! impl_lazy_class_visits {
    () => {
        fn visit_class(&mut self, node: &Class) {
            lazy_visit_class(self, node);
        }
        fn visit_class_member(&mut self, member: &ClassMember) {
            lazy_visit_class_member(self, member);
        }
    };
}

macro_rules! impl_lazy_optional_path_visits {
    () => {
        fn visit_if_stmt(&mut self, node: &IfStmt) {
            lazy_visit_if_stmt(self, node);
        }
        fn visit_cond_expr(&mut self, node: &CondExpr) {
            lazy_visit_cond_expr(self, node);
        }
        fn visit_bin_expr(&mut self, node: &BinExpr) {
            lazy_visit_bin_expr(self, node);
        }
        fn visit_opt_chain_expr(&mut self, node: &OptChainExpr) {
            lazy_visit_may_skip(self, node);
        }
        fn visit_while_stmt(&mut self, node: &WhileStmt) {
            lazy_visit_may_skip(self, node);
        }
        fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
            lazy_visit_may_skip(self, node);
        }
        fn visit_for_stmt(&mut self, node: &ForStmt) {
            lazy_visit_may_skip(self, node);
        }
        fn visit_try_stmt(&mut self, node: &TryStmt) {
            lazy_visit_may_skip(self, node);
        }
        fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
            lazy_visit_may_skip(self, node);
        }
    };
}

pub mod abstract_domain;
pub mod analyze;
mod array_spreads;
pub mod at_init_fallback;
pub mod binding_effect_coverage;
pub mod callable_inference;
mod cell_overlap;
pub mod collector;
mod dynamic_scope;
pub mod evaluation_effects;
pub mod global_object;
pub mod heap_contents;
pub mod initialization_effects;
pub mod item_views;
pub mod lazy_boundary;
mod local_class_construction;
pub mod local_effect_targets;
pub mod local_effects;
pub mod purity_classification;
pub mod saved;
pub mod semantic_inference;
mod set_iterables;
pub mod source_unit_read_closure;
pub mod standard_globals;
pub mod statement_facts;
pub mod value_flow;
#[cfg(test)]
mod value_flow_tests;
#[cfg(test)]
mod value_set_tests;

// Crate-internal re-exports: each submodule reaches its siblings'
// crate-internal items through `use super::*;`, which sees these globs.
pub use array_spreads::StableArraySpreadShapes;
pub(crate) use at_init_fallback::*;
pub use cell_overlap::{CellOverlap, CellOverlapIndex, CellOverlapLaw};
pub use cell_overlap::{first_overlapping, may_conflict, may_overlap};
pub(crate) use collector::*;
pub use dynamic_scope::DynamicScopeFact;
pub(crate) use global_object::*;
pub use item_views::compute_shadowed_globals;
pub(crate) use item_views::*;
pub(crate) use lazy_boundary::*;
pub use local_class_construction::LocalClassConstructionProof;
pub use local_class_construction::LocalClassConstructionShapes;
pub use local_class_construction::LocalClassHeapProof;
pub(crate) use local_effect_targets::*;
pub use purity_classification::CompletedPurity;
pub use purity_classification::NativeOwnDataReadDiagnostic;
pub use purity_classification::PreparedPurity;
pub use purity_classification::StatementPurityContext;
pub(crate) use semantic_inference::ArgumentNoEscapeFacts;
pub use set_iterables::NativeIterableOperandOrigin;
pub use set_iterables::SetIterableProof;
pub use set_iterables::SetIterableShapes;
pub use set_iterables::TopLevelNativeIterableOperandSite;
pub(crate) use set_iterables::{
    TopLevelNativeIterableOperandProjection, primitive_literal_property_keys,
};
pub use source_unit_read_closure::SourceUnitReadClosure;
pub use statement_facts::HeapContentCells;
pub use statement_facts::LocalRecordFactoryPurityCertificate;
pub use statement_facts::{
    CallbackFactoryBarrierFact, CallbackFactoryBarrierRejection,
    CallbackFactoryCallArgumentWitness, CallbackFactoryUseFact, CallbackFactoryWriteWitness,
    ReturnedRecordUseFact,
};
pub(crate) use statement_facts::{ConstructorFacts, ExportLinkage};
pub(crate) use value_flow::{CandidateObjectIndex, own_data_keys};
pub use value_flow::{ChunkValueFlow, ProgramFlowModule, ProgramValueFlows, program_value_flows};

// Public API: importable at `facts::<item>` exactly as before the split.
pub use analyze::{
    ExportReentryPolicy, analyze_chunk_structural, collect_chunk_source_facts,
    collect_chunk_source_facts_from_items, complete_chunk_structural,
    complete_chunk_structural_with_flow, complete_chunk_structural_with_inputs,
    module_has_top_level_await,
};
pub use evaluation_effects::{
    GlobalAccessEvidence, GlobalAccessKind, GlobalAccessSite, GlobalMayAccess,
};
pub use initialization_effects::{
    InitializationEffectEvidence, InitializationEffects, OwnerInitializationCoverageRefusal,
    OwnerInitializationCoverageRefusalReason, RootAttempt, RootAttemptCounts, RootAttemptRewind,
    UnknownEffectCoverage,
};
pub use item_views::{TopLevelItemView, top_level_item_views};
pub use local_effects::local_namespace_iife_target;
pub use statement_facts::{
    ChunkFactAnalysis, DataflowOpacityCause, EffectCell, PositionBucketed, StatementEffectSummary,
    StatementFacts, StatementKind, StructuralChunkAnalysis, StructuralChunkSourceFacts,
    StructuralStatementFacts,
};
