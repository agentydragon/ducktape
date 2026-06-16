//! Per-chunk expression/statement purity classification.
//!
//! `ChunkCodeGraph` indexes a chunk's top-level bindings; the
//! `classify_*` functions answer "does evaluating this expression /
//! statement fire observable user code?" against that index, the
//! whitelist tables, and the spec author's declared-purity hints.
//!
//! The module is split by responsibility; submodules share the imports
//! and crate-internal items re-exported below via `pub(crate) use`, so
//! each submodule only needs `use super::*;`:
//!
//! - `code_graph` — `ChunkCodeGraph` / `ChunkBinding`, the chunk-top
//!   function call-graph + SCC purity fixpoint, and the function-body
//!   purity walk (`classify_function_body` / `BodyPurityCollector`).
//! - `redundant_hints` — `RedundantPurity*` / `RedundantPureMember*`
//!   types and the load-bearing-hint detectors.
//! - `plain_data` — plain-data / plain-array / primitive-const /
//!   fluent-const binding collection and the escape/write scanners.
//! - `ts_enum_iife` — recognition of the TypeScript-emit enum IIFE
//!   plain-object shape.
//! - `purity_type` — the `Purity` / `PurityReason` / `PurityRule`
//!   verdict types.
//! - `classifier` — `classify_expr_purity` and its expression /
//!   member / call / fluent-chain / property recursion, plus
//!   `classify_var_decl_purity` and `class_has_static_observable`.
//! - `builtin_calls` — `new`-expression, iterable-arg, and
//!   `Object.*` builtin admission helpers and the result-primitive /
//!   safe-key predicates.

pub(crate) use std::collections::{BTreeMap, BTreeSet};

pub(crate) use binding_targets::strip_parens;
pub(crate) use petgraph::algo::tarjan_scc;
pub(crate) use petgraph::graphmap::DiGraphMap;
pub(crate) use serde::{Deserialize, Serialize};
pub(crate) use swc_common::{Span, Spanned};
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitWith};

pub(crate) use crate::SourceLocation;
pub(crate) use crate::facts::TopLevelItemView;

mod whitelists;

pub(crate) use whitelists::{
    PLAIN_DATA_HOSTILE_BUILTINS, PURE_BUILTIN_NEW_ARRAY_ITERABLE, PURE_BUILTIN_NEW_NO_ARGS,
    PURE_BUILTIN_NEW_STRING_LITERAL_ARG, PURE_GLOBAL_CALLS, PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS,
    PURE_OBJECT_CALLS_ON_PLAIN_DATA, PURE_STATIC_CALLS, PURE_STATIC_FUNCTION_REFS,
    PURE_STATIC_PROPS,
};
pub(crate) use whitelists::{SHADOW_TRACKED_GLOBALS, WHITELIST_RECEIVERS};

mod builtin_calls;
mod classifier;
mod code_graph;
mod plain_data;
mod purity_type;
mod redundant_hints;
mod ts_enum_iife;

// Crate-internal re-exports: each submodule reaches its siblings'
// crate-internal items through `use super::*;`, which sees these globs.
pub(crate) use builtin_calls::*;
pub(crate) use classifier::*;
pub(crate) use code_graph::*;
pub(crate) use plain_data::*;
pub(crate) use redundant_hints::*;
pub(crate) use ts_enum_iife::*;

// Public API: importable at `purity::<item>` exactly as before the split.
pub use code_graph::ChunkCodeGraph;
pub use purity_type::{Purity, PurityReason, PurityRule};
pub use redundant_hints::{
    RedundantPureMemberHint, RedundantPureMemberReason, RedundantPurityHint, RedundantPurityReason,
};

#[cfg(test)]
mod classifier_tests;

#[cfg(test)]
mod graph_purity_tests;

#[cfg(test)]
mod redundant_hints_tests;
