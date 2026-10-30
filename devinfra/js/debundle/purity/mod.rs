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
//! - `ts_enum_iife` — recognition of the TypeScript-emit enum IIFE
//!   plain-object shape and its named primitive fields.
//! - `purity_type` — the `Purity` / `PurityReason` / `PurityRule`
//!   verdict types.
//! - `classifier` — `classify_expr_purity` and its expression /
//!   member / call / `new` / fluent-chain / property recursion,
//!   `classify_var_decl_purity`, `classify_class_static_purity`, and the
//!   result-primitive / safe-key predicates.
//! - `native_rows` — the purity projection of builtin summary rows.

pub(crate) use crate::analysis_inputs::ProofDependencies;
pub(crate) use std::collections::{BTreeMap, BTreeSet};

pub(crate) use serde::{Deserialize, Serialize};
pub(crate) use swc_common::{Span, Spanned};
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitWith};

pub(crate) use crate::SourceLocation;
pub(crate) use crate::facts::TopLevelItemView;
pub(crate) use crate::facts::abstract_domain::builtins::{self, Invocation};
pub(crate) use crate::facts::abstract_domain::primitive::evaluate;
pub(crate) use crate::facts::abstract_domain::value::PrimKinds;

mod classifier;
mod code_graph;
mod effect_inference;
mod fluent;
mod fresh_record;
mod native_rows;
mod purity_type;
mod redundant_hints;
mod ts_enum_iife;

// Crate-internal re-exports: each submodule reaches its siblings'
// crate-internal items through `use super::*;`, which sees these globs.
pub(crate) use classifier::*;
pub(crate) use code_graph::*;
pub(crate) use effect_inference::*;
pub(crate) use fluent::*;
pub(crate) use fresh_record::*;
pub(crate) use native_rows::*;
pub(crate) use redundant_hints::*;
pub(crate) use ts_enum_iife::*;

// Public API: importable at `purity::<item>` exactly as before the split.
pub use code_graph::ChunkCodeGraph;
pub use purity_type::{Purity, PurityReason, PurityRule};
pub use redundant_hints::{
    RedundantPureMemberHint, RedundantPureMemberReason, RedundantPurityHint,
    RedundantPurityProofBasis, RedundantPurityReason,
};

#[cfg(test)]
mod classifier_tests;

#[cfg(test)]
mod graph_purity_tests;

#[cfg(test)]
mod redundant_hints_tests;
