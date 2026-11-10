//! Author premises, as tier 0 consumes them.
//!
//! A premise is trusted, not checked: it states a contract the analysis
//! cannot or does not establish. The callee kinds mirror the spec's
//! `chunk_export_purity` vocabulary (`pure_exports`, `pure_new_exports`,
//! `pure_members`, `callback_factory_exports`, `callback_factory_members`)
//! and its decorator-helper effect family; the site kinds mirror
//! `initializer_purity: pure` and `native_operation_inputs`.

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
    /// `f(args)` retains its arguments in a fresh result: it neither calls
    /// the functions among them nor reads the objects among them, cannot
    /// throw, and reads and writes nothing of the importer. It may read and
    /// write state private to its provider.
    CallbackFactory,
    /// `f(decorators, target, …)` applies each decorator to the object at
    /// `target_arg` and otherwise writes only that object; it invokes no
    /// method, accessor or constructor of the target, publishes nothing and
    /// may return a replacement for the target.
    DecoratorHelper,
    /// The function, or the function it returns when called, is a
    /// decorator: applied by a helper, it cannot throw, writes only the
    /// target it is given and reads only that target and its arguments.
    Decorator,
    /// A decorator that may also read and write state private to its
    /// provider.
    ContainedDecorator,
    /// `f(args)` returns a fresh result that retains its arguments, as a
    /// callback factory does, and has no state of its own.
    Allocator,
    /// `new F(args)` only stores its arguments into the new object.
    AllocatorNew,
    /// `f(function, "name")` names a function written in place and returns
    /// it.
    FunctionNamer,
    /// The binding holds a lazy module's thunk: `f()` runs the module body
    /// the first time and returns the module's exports every time.
    LazyModule,
    /// `f(lazy())` returns a fresh namespace object over a lazy module's
    /// exports and reads only that exports object.
    InteropNamespace,
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SiteKind {
    /// The statement has no observable effect and cannot throw. It reads
    /// the bindings it mentions outside function bodies, and what they
    /// reference.
    Pure,
    /// The intrinsic operations of the statement behave as specified,
    /// whatever the census found.
    NativeOperation,
}

/// A premise about one source unit.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Site {
    pub unit: usize,
    pub kind: SiteKind,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Premises {
    #[serde(default)]
    pub callees: Vec<Premise>,
    #[serde(default)]
    pub sites: Vec<Site>,
    /// Globals no code of the program modifies, whatever the census found.
    #[serde(default)]
    pub intrinsics_never_written: Vec<String>,
    /// Pairs of source units certified to commute.
    #[serde(default)]
    pub commuting: Vec<(usize, usize)>,
}
