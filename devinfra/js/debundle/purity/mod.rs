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
pub(crate) use crate::facts::abstract_domain::builtins::{self, Invocation, Row};
pub(crate) use crate::facts::abstract_domain::primitive::{PrimScope, evaluate};
pub(crate) use crate::facts::abstract_domain::value::PrimKinds;
pub(crate) use crate::inference::{FactContext, IterativeInference, IterativeRules};

mod classifier;
mod code_graph;
mod effect_inference;
mod fresh_record;
mod native_rows;
mod plain_data;
mod purity_type;
mod redundant_hints;
mod ts_enum_iife;

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

/// Primitive types of chunk-top bindings (`collect_binding_prims`) and the
/// global names whose intrinsic the chunk may replace.
#[derive(Debug, Default, Clone)]
pub(crate) struct BindingPrims {
    kinds: BTreeMap<String, PrimKinds>,
    /// Shadowed globals plus every `G` and `G.m` a visible write may replace.
    replaced_globals: BTreeSet<String>,
}

impl BindingPrims {
    /// The bindings visible where `local_shadowed` names are rebound.
    pub(crate) fn scope<'a>(&'a self, local_shadowed: &'a BTreeSet<String>) -> ScopedPrims<'a> {
        ScopedPrims {
            prims: self,
            local_shadowed,
        }
    }

    /// Bindings whose primitive excludes Symbol: `ToString` and `ToNumber`
    /// on them run no user code.
    fn non_symbol_bindings(&self) -> BTreeSet<String> {
        self.bindings_within(PrimKinds::ANY.without(PrimKinds::SYMBOL))
    }

    /// Symbol bindings: safe for `ToPropertyKey`, while `ToString` throws.
    fn symbol_bindings(&self) -> BTreeSet<String> {
        self.bindings_within(PrimKinds::SYMBOL)
    }

    fn bindings_within(&self, kinds: PrimKinds) -> BTreeSet<String> {
        self.kinds
            .iter()
            .filter(|(_, binding)| binding.is_only(kinds))
            .map(|(name, _)| name.clone())
            .collect()
    }
}

pub(crate) struct ScopedPrims<'a> {
    prims: &'a BindingPrims,
    local_shadowed: &'a BTreeSet<String>,
}

impl PrimScope for ScopedPrims<'_> {
    fn binding(&self, ident: &Ident) -> Option<PrimKinds> {
        let name = ident.sym.as_ref();
        if self.local_shadowed.contains(name) {
            return None;
        }
        self.prims.kinds.get(name).copied()
    }

    /// Purity trusts a callee only where its row allows identity from A11 and
    /// an unshadowed, unwritten spelling.
    fn intact_intrinsic(&self, row: &Row, global: &Ident, member: Option<&str>) -> bool {
        let name = global.sym.as_ref();
        row.assumed_intact
            && !self.local_shadowed.contains(name)
            && !self.prims.replaced_globals.contains(name)
            && member.is_none_or(|member| {
                !self
                    .prims
                    .replaced_globals
                    .contains(&format!("{name}.{member}"))
            })
    }
}

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
