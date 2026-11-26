//! Mechanical, reviewable spec selector rewrites.
//!
//! This powers `debundle spec synthesize-selectors`: a scripting-safe CLI for
//! applying proven selector rewrites across a modules tree. The core
//! source-aware rewrite is framed as: given one or more target entities, come
//! up with the simplest selector/spec fragment that uniquely selects them.
//! This module starts with an indexed subset of that minimization problem:
//! build a per-chunk declaration table and binding-name index, group requested
//! bindings by source declaration, render a structural selector with
//! declarator gaps for non-target siblings, then prove it with the selector
//! resolve every command uses: the selector must resolve on its own selector.

// The selector minimizer is split by form: the AST-holing engine (the
// `selector_render` crate), the regex-over-string-literal anchors
// (`regex_anchor`), and the per-form minimizers
// (`minimize::{function,class,object,var,group}`).
mod minimize;
mod regex_anchor;

mod chunk_index;
pub mod driver;
mod group_synthesis;
mod module_yaml;
mod name_binding_rewrite;
mod proof;
pub mod report;
mod shape_runs;
pub mod source_unit;

#[cfg(test)]
mod selector_minimizer_proptest;
