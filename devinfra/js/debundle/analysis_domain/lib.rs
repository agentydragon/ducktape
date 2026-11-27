//! The syntactic and abstract domain the rest of analysis builds on: top-level
//! item views, lazy/eager position tracking, whole-chunk dynamic scope, the
//! builtin summary table, primitive operator results, intrinsic-slot
//! integrity, chunk-top allocation roots and the TypeScript enum IIFE shape.
//! Nothing here depends on statement facts, value flow or purity. Matching a
//! `ModuleItem` down to the declaration it carries belongs to `js_ast`, whose
//! item views this crate calls instead of restating the item shapes.

pub(crate) use std::collections::{BTreeMap, BTreeSet};

pub(crate) use serde::{Deserialize, Serialize};
pub(crate) use swc_common::Spanned;
pub(crate) use swc_ecma_ast::*;
pub(crate) use swc_ecma_visit::{Visit, VisitWith};

pub(crate) use item_views::*;

pub mod allocation_roots;
pub mod builtins;
pub mod dynamic_scope;
pub mod integrity;
pub mod item_views;
pub mod lazy_boundary;
pub mod primitive;
pub mod ts_enum_iife;
