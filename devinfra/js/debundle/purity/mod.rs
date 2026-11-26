//! Per-chunk expression/statement purity classification.
//!
//! `ChunkCodeGraph` indexes a chunk's top-level bindings; the
//! `classify_*` functions answer "does evaluating this expression /
//! statement fire observable user code?" against that index, the
//! builtin summary table, and the spec author's declared-purity hints.
//!
//! The module is split by responsibility; submodules share the imports
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
//! - `ts_enum_iife` — the TypeScript-emit enum IIFE as a pre-purity
//!   admission shape (heap `PlainData` and the initializer's own purity).
//! - `classifier` — `classify_expr_purity` and its expression /
//!   member / call / `new` / fluent-chain / property recursion,
//!   `classify_var_decl_purity`, `classify_class_static_purity`, and the
//!   result-primitive / safe-key predicates.
//! - `native_rows` — the purity projection of builtin summary rows.
//! - `order_dependent_reads` — reads of cells whose writers the reading
//!   chunk's dependency graph cannot order.

pub(crate) use analysis_vocabulary::inputs::ProofDependencies;
pub(crate) use std::collections::{BTreeMap, BTreeSet};

pub(crate) use serde::{Deserialize, Serialize};
pub(crate) use swc_common::{Span, Spanned};
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitWith};

pub(crate) use crate::facts::TopLevelItemView;
pub(crate) use crate::facts::abstract_domain::builtins::{self, Invocation};
pub(crate) use crate::facts::abstract_domain::primitive::evaluate;
pub(crate) use analysis_vocabulary::effect::{
    CoerceSite, Effect, HavocCause, InvokeSite, binary_throws,
};
pub(crate) use analysis_vocabulary::execution_evidence::SourceLocator;
pub(crate) use analysis_vocabulary::purity::{Effects, Purity};

pub mod classifier;
pub mod code_graph;
mod effect_inference;
mod fluent;
mod fresh_record;
mod native_rows;
mod order_dependent_reads;
pub mod redundant_hints;
pub mod saved;
mod ts_enum_iife;

// Crate-internal re-exports: each submodule reaches its siblings'
// crate-internal items through `use super::*;`, which sees these globs.
pub(crate) use classifier::*;
pub use code_graph::ShapeProofRefinement;
pub(crate) use code_graph::*;
pub(crate) use effect_inference::*;
pub(crate) use fluent::*;
pub use fresh_record::FreshRecordDestructuringContext;
pub(crate) use fresh_record::*;
pub(crate) use native_rows::*;
pub(crate) use order_dependent_reads::*;
pub(crate) use ts_enum_iife::*;

// Public API: importable at `purity::<item>`.
pub use code_graph::{ChunkCodeGraph, PurityCallContext, PurityShapeInputs};
pub use effect_inference::{PurityEffectRules, PurityMode};
pub use order_dependent_reads::immutable_declarations;
pub use redundant_hints::{
    RedundantPureMemberHint, RedundantPureMemberReason, RedundantPurityHint,
    RedundantPurityProofBasis, RedundantPurityReason,
};
