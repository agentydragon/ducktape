//! Author premises about callees, as tier 0 consumes them.
//!
//! A premise is trusted, not checked: it states a contract of a function
//! the analysis cannot or does not look into. The kinds mirror the spec's
//! `chunk_export_purity` vocabulary (`pure_exports`, `pure_new_exports`,
//! `pure_members`, `callback_factory_exports`, `callback_factory_members`);
//! `decorator_helper` has no counterpart there.

use serde::Deserialize;

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Deserialize, serde::Serialize,
)]
#[serde(rename_all = "snake_case")]
pub enum PremiseKind {
    /// `f(args)` only allocates. Its result and behavior depend only on its
    /// arguments and the objects they reference; it cannot throw, runs no
    /// code but the functions among its arguments, and keeps no reference
    /// outside its result.
    PureCall,
    /// The same for `new F(args)`.
    PureNew,
    /// A pure call that also never invokes the functions among its
    /// arguments: it retains them in its result.
    CallbackFactory,
    /// `f(decorators, target, …)` changes only the object passed at
    /// `target_arg`, including whatever the decorator functions do when
    /// applied; it may return a replacement for the target.
    DecoratorHelper,
}

/// How the chunk names the callee.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "by", rename_all = "snake_case", deny_unknown_fields)]
pub enum Callee {
    /// A binding imported from `specifier`; `export` is the exported name,
    /// `default`, or `*` for a namespace import.
    Import { specifier: String, export: String },
    /// A top-level binding declared by the source unit `unit`.
    Local { unit: usize },
    /// `ns.member` where `ns` is such an import.
    ImportMember {
        specifier: String,
        export: String,
        member: String,
    },
    /// `ns.member` where the source unit `unit` declares `ns`.
    LocalMember { unit: usize, member: String },
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Premise {
    pub callee: Callee,
    pub kind: PremiseKind,
    /// For a decorator helper: the argument position of the target.
    #[serde(default = "default_target_arg")]
    pub target_arg: usize,
}

fn default_target_arg() -> usize {
    1
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Premises {
    #[serde(default)]
    pub callees: Vec<Premise>,
}
