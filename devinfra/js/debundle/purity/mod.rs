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
//! - `plain_data` — plain-data candidate collection and whole-chunk
//!   escape/write scanners.
//! - `value_inference` — finite positive closure for fluent, plain-array,
//!   primitive, and Symbol const value classes.
//! - `ts_enum_iife` — recognition of the TypeScript-emit enum IIFE
//!   plain-object shape.
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

mod classifier;
mod code_graph;
mod effect_inference;
mod fresh_record;
mod native_rows;
mod plain_data;
mod purity_type;
mod redundant_hints;
mod ts_enum_iife;
mod value_inference;

// Crate-internal re-exports: each submodule reaches its siblings'
// crate-internal items through `use super::*;`, which sees these globs.
pub(crate) use classifier::*;
pub(crate) use code_graph::*;
pub(crate) use effect_inference::*;
pub(crate) use fresh_record::*;
pub(crate) use native_rows::*;
pub(crate) use plain_data::*;
pub(crate) use redundant_hints::*;
pub(crate) use ts_enum_iife::*;

#[derive(Default)]
pub(crate) struct BindingValueClasses {
    pub(crate) primitive_bindings: BTreeSet<String>,
    pub(crate) symbol_const_bindings: BTreeSet<String>,
    pub(crate) number_bindings: BTreeSet<String>,
    pub(crate) bigint_bindings: BTreeSet<String>,
    /// Stable `var` bindings are Number after initialization and `undefined`
    /// before it, so callers must not treat these as exact Number facts.
    pub(crate) number_or_undefined_bindings: BTreeSet<String>,
}

use value_inference::{
    infer_const_value_classes, infer_fluent_const_bindings, infer_plain_array_candidates,
    is_bigint_expression, is_definitely_non_bigint_expression, is_definitely_string_expression,
    is_number_expression,
};

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
