//! Per-chunk expression/statement purity classification.
//!
//! `ChunkCodeGraph` indexes a chunk's top-level bindings; the
//! `classify_*` functions answer "does evaluating this expression /
//! statement fire observable user code?" against that index, the
//! builtin summary table, and the spec author's declared-purity hints.
//!
//! The crate is split by responsibility; submodules share the imports
//! and crate-internal items re-exported below via `pub(crate) use`, so
//! each submodule only needs `use super::*;`:
//!
//! - `code_graph` — `ChunkCodeGraph` / `ChunkBinding`, stable function
//!   identities, and the canonical function-body purity walk.
//! - `effect_inference` — typed body-completion and growing-effect facts for
//!   local and cross-module recursive call obligations.
//! - `redundant_hints` — `RedundantPurity*` / `RedundantPureMember*`
//!   types and the load-bearing-hint detectors.
//! - `fluent` — the closure of author-trusted fluent roots over chunk-top
//!   `const` chains.
//! - `classifier` — `classify_expr_purity` and its expression /
//!   member / call / `new` / fluent-chain / property recursion,
//!   `classify_var_decl_purity`, `classify_class_static_purity`, and the
//!   result-primitive / safe-key predicates.
//! - `native_rows` — the purity projection of builtin summary rows.
//! - `order_dependent_reads` — reads of cells whose writers the reading
//!   chunk's dependency graph cannot order.
//! - `heap` — chunk-top allocation heap state and its `PlainData`
//!   projection.
//! - `semantic_inference`, `callable_inference` — positive certificates
//!   over closed source/native facts.
//! - `set_iterables`, `local_class_construction`, `array_spreads`,
//!   `fresh_record` — the shape proofs `ChunkCodeGraph::build` queries.
//! - `saved` — protobuf purity verdicts.

pub(crate) use analysis_vocabulary::inputs::ProofDependencies;
pub(crate) use std::collections::{BTreeMap, BTreeSet};

pub(crate) use serde::{Deserialize, Serialize};
pub(crate) use swc_common::{Span, Spanned};
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitWith};

pub(crate) use analysis_domain::builtins::{self, Invocation};
pub(crate) use analysis_domain::item_views::TopLevelItemView;
pub(crate) use analysis_domain::primitive::evaluate;
pub(crate) use analysis_vocabulary::effect::{
    CoerceSite, Effect, HavocCause, InvokeSite, binary_throws,
};
pub(crate) use analysis_vocabulary::execution_evidence::SourceLocator;
pub(crate) use analysis_vocabulary::purity::{Effects, Purity};

mod array_spreads;
pub mod callable_inference;
pub mod classifier;
pub mod code_graph;
mod effect_inference;
mod fluent;
mod fresh_record;
pub mod heap;
mod local_class_construction;
mod native_rows;
mod order_dependent_reads;
pub mod redundant_hints;
pub mod saved;
pub mod semantic_inference;
mod set_iterables;

// Crate-internal re-exports: each submodule reaches its siblings'
// crate-internal items through `use super::*;`, which sees these globs.
pub use array_spreads::StableArraySpreadShapes;
pub use classifier::*;
pub use code_graph::ShapeProofRefinement;
pub(crate) use code_graph::*;
pub(crate) use effect_inference::*;
pub(crate) use fluent::*;
pub use fresh_record::FreshRecordDestructuringContext;
pub(crate) use fresh_record::*;
pub use local_class_construction::{
    LocalClassConstructionProof, LocalClassConstructionShapes, LocalClassHeapProof,
};
pub(crate) use native_rows::*;
pub(crate) use order_dependent_reads::*;
pub(crate) use semantic_inference::ArgumentNoEscapeFacts;
pub use set_iterables::{
    NativeIterableOperandOrigin, SetIterableProof, SetIterableShapes,
    TopLevelNativeIterableOperandSite,
};
pub(crate) use set_iterables::{
    TopLevelNativeIterableOperandProjection, primitive_literal_property_keys,
};

// Public API: importable at `purity::<item>`.
pub use code_graph::{ChunkCodeGraph, PurityCallContext, PurityShapeInputs};
pub use effect_inference::{PurityEffectRules, PurityMode};
pub use order_dependent_reads::immutable_declarations;
pub use redundant_hints::{
    RedundantPureMemberHint, RedundantPureMemberReason, RedundantPurityHint,
    RedundantPurityProofBasis, RedundantPurityReason,
};
