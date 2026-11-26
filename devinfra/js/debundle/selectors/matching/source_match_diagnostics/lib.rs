//! Diagnostics over the `source_match` resolver: body-debt and near-miss
//! scoring (`fact_near_miss`) and local explanation of one source range
//! (`explain`). Matching itself stays in `source_match`; nothing there depends
//! on this crate.
//!
//! Both modules reach the shared imports below through `use super::*;`.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use js_ast::item_var_decl;
use serde::Serialize;
use source_match::declared_bindings::declared_bindings;
use source_match::holes::{
    declarator_list_hole_name, module_item_list_hole_name, string_literal_regex_pattern,
};
use source_match::{
    ParsedSourceMatchSelector, SelectorField, SourceMatchBodyDebt, SourceMatchNearMiss,
    chunk_resolver, free_identifiers, parse_selector_module, selector_mode, unsupported_error,
};
#[cfg(test)]
use spec::SourceMatchIdentifierMode;
use spec::annotations::AnonymousStatementSelector;
use swc_ecma_ast::*;

mod explain;
mod fact_near_miss;

pub use explain::{
    ExplainRangeKind, RangeExplanation, RangeExplanationStatus, RangeFailure, RangeFailureCategory,
    explain_range,
};
pub use fact_near_miss::{
    ItemMatchIndices, SourceMatchDebtRuntime, fact_near_misses, fact_source_match_body_debt,
};
