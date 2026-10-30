//! Heap state of chunk-top allocations, and the writes a builtin performs.
//!
//! A tracked allocation is a chunk-top binding's initializer: an object or
//! array literal, a TS enum IIFE (`purity/ts_enum_iife.rs`) or, for arrays, a
//! non-mutating array method result. A binding denotes its initializers and
//! every literal value flow says it may hold. `PlainData` is the projection
//! "never rebound, and every denoted allocation is `Private` and `Clean`":
//! member reads through the binding run no user code and see values no write
//! changes.
//!
//! The state is solved once per chunk from value flow and one whole-chunk
//! scan, before purity runs (plan § 5 pipeline order). It is flow-insensitive:
//! a write anywhere in the chunk taints the allocation for every read.

use std::collections::{BTreeMap, BTreeSet};

use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use super::builtins::{self, ArgSel, Invocation};
use super::primitive::{BindingPrims, evaluate};
use super::value::{Kind, Value};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::{
    ArgumentNoEscapeFacts, CandidateObjectIndex, ChunkValueFlow, TopLevelItemView,
    close_const_aliases, collect_declared_names, primitive_literal_property_keys,
    top_level_initializers,
};
use crate::purity::{
    is_ts_enum_iife_call_for_binding, is_ts_enum_iife_init_for_binding, only_inspects_operand,
    static_member, ts_enum_iife_argument_binding,
};
use crate::reports::schema::InferenceUse;

/// A write a builtin row performs. Writes to the fresh allocation are its
/// initial contents; the row's `allocates` names that allocation's kind.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mutation {
    /// `SetIntegrityLevel(O, frozen)` on the argument: it becomes
    /// non-extensible with every own property non-configurable and, for data
    /// properties, non-writable.
    Integrity(ArgSel),
    /// `[[DefineOwnProperty]]` on `target` for every own enumerable key of the
    /// `descriptors` record.
    DefineProperties { target: ArgSel, descriptors: ArgSel },
    /// The fresh allocation receives the argument's own property descriptors:
    /// data values and accessor functions by identity, never invoked.
    FreshOwnDescriptorsOf(ArgSel),
    /// The fresh allocation receives the argument's own string keys as dense
    /// elements in `[[OwnPropertyKeys]]` order.
    FreshOwnKeysOf(ArgSel),
    /// An internal slot of the fresh allocation retains the argument without
    /// invoking it.
    FreshRetains(ArgSel),
}

/// Whether code the scan does not follow can hold a reference.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
enum Escape {
    /// Only tracked aliases, returns, member-read receivers, spread sources,
    /// `typeof`/`!`/`void` operands, operand-only builtin rows and
    /// author-proven non-escaping arguments see the reference. Exports do not
    /// publish: importers are assumed not to mutate an exported binding's
    /// object (docs/purity_soundness.md § `ChunkBinding::PlainData`).
    #[default]
    Private,
    /// The reference reaches any other position: a call argument, a stored
    /// value, an alias the scan does not track.
    Published,
}

/// Whether a write can reach the allocation after its initializer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
enum Taint {
    #[default]
    Clean,
    /// A member write, update or `delete`, or for arrays a call of a method
    /// that is not non-mutating, may reach it. The scan does not separate a
    /// receiver from the allocations stored in it: a write through a receiver
    /// taints every tracked allocation the receiver reaches.
    Tainted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
struct ObjectState {
    escape: Escape,
    taint: Taint,
}

impl ObjectState {
    fn is_plain(self) -> bool {
        self.escape == Escape::Private && self.taint == Taint::Clean
    }
}

#[derive(Clone, Copy)]
enum HeapEffect {
    Publish,
    Taint,
}

/// Heap state of a chunk's tracked object and array literal initializers.
pub(crate) struct ChunkHeap {
    states: BTreeMap<Value, ObjectState>,
    /// Bindings whose every chunk-top initializer is a plain-data shape, and
    /// the allocations each denotes.
    bindings: BTreeMap<String, BTreeSet<Value>>,
    /// Tracked names assigned a value the scan cannot prove plain.
    rebound: BTreeSet<String>,
    dependencies: BTreeMap<String, ProofDependencies>,
    inference_uses: BTreeMap<String, BTreeSet<InferenceUse>>,
}

/// The `PlainData` projection: admitted bindings with the author inputs each
/// depends on.
pub(crate) struct PlainDataBindings {
    pub(crate) bindings: Vec<String>,
    pub(crate) dependencies: BTreeMap<String, ProofDependencies>,
    pub(crate) inference_uses: BTreeMap<String, BTreeSet<InferenceUse>>,
}

/// Chunk-top initializers per binding name. `var` may repeat.
type InitGroups<'a> = BTreeMap<String, Vec<(&'a Expr, VarDeclKind)>>;

fn init_groups<'a>(body: &'a [TopLevelItemView<'_>]) -> InitGroups<'a> {
    let mut inits_by_name = BTreeMap::new();
    for item in body {
        for (name, init, kind) in top_level_initializers(item.as_module_item()) {
            inits_by_name
                .entry(name)
                .or_insert_with(Vec::new)
                .push((init, kind));
        }
    }
    inits_by_name
}

/// The allocation an initializer creates, keyed by its start offset as value
/// flow keys object and array literals.
fn site(init: &Expr) -> Value {
    Value::Alloc(Kind::Object, init.unwrap_parens().span().lo.0)
}

/// Bindings whose every initializer is plain data, with those allocations.
fn tracked_bindings(inits_by_name: &InitGroups<'_>) -> BTreeMap<String, BTreeSet<Value>> {
    inits_by_name
        .iter()
        .filter(|(name, inits)| {
            inits
                .iter()
                .all(|(init, kind)| is_plain_data_init(init, name, *kind))
        })
        .map(|(name, inits)| {
            (
                name.clone(),
                inits.iter().map(|(init, _)| site(init)).collect(),
            )
        })
        .collect()
}

/// Literal allocations value flow follows. A binding with a TS enum IIFE
/// initializer contributes none: only its own name reaches it.
fn literal_roots(
    inits_by_name: &InitGroups<'_>,
    bindings: &BTreeMap<String, BTreeSet<Value>>,
) -> BTreeSet<Value> {
    bindings
        .iter()
        .filter(|(name, _)| {
            inits_by_name[*name]
                .iter()
                .all(|(init, _)| matches!(init.unwrap_parens(), Expr::Object(_) | Expr::Array(_)))
        })
        .flat_map(|(_, sites)| sites.iter().copied())
        .collect()
}

/// Allocations value flow must register as candidate roots before its single
/// solve, so the heap scan can query what reaches them.
pub(crate) fn tracked_allocation_roots(body: &[TopLevelItemView<'_>]) -> BTreeSet<Value> {
    let inits_by_name = init_groups(body);
    literal_roots(&inits_by_name, &tracked_bindings(&inits_by_name))
}

impl ChunkHeap {
    /// Solve escape and taint for every tracked object initializer.
    /// `arguments` holds author-proven non-escaping call arguments; a binding
    /// kept private through one carries its proof dependencies.
    pub(crate) fn analyze(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        value_flow: &ChunkValueFlow,
        arguments: Option<&ArgumentNoEscapeFacts>,
    ) -> Self {
        let inits_by_name = init_groups(body);
        let bindings = tracked_bindings(&inits_by_name);
        if bindings.is_empty() {
            return Self {
                states: BTreeMap::new(),
                bindings,
                rebound: BTreeSet::new(),
                dependencies: BTreeMap::new(),
                inference_uses: BTreeMap::new(),
            };
        }
        let top_level_ids: BTreeMap<String, Id> = body
            .iter()
            .flat_map(|item| collect_declared_names(item.as_module_item()))
            .map(|id| (id.0.to_string(), id))
            .collect();
        let primitive_members: BTreeMap<Id, BTreeSet<String>> = bindings
            .keys()
            .filter_map(|name| {
                let [(init, VarDeclKind::Const)] = inits_by_name[name].as_slice() else {
                    return None;
                };
                let Expr::Object(object) = init.unwrap_parens() else {
                    return None;
                };
                let keys = primitive_literal_property_keys(object)?;
                Some((top_level_ids[name].clone(), keys))
            })
            .collect();
        let roots = literal_roots(&inits_by_name, &bindings);
        let plain_names: Vec<String> = bindings.keys().cloned().collect();
        // Every chunk-top name holding a root is a tracked alias of it.
        let mut tracked = bindings;
        let mut aliases_by_root = BTreeMap::new();
        for (root, names) in value_flow.aliases_by_object(&roots) {
            let aliases: BTreeSet<String> = names
                .into_iter()
                .filter(|name| top_level_ids.contains_key(name))
                .collect();
            for alias in &aliases {
                tracked.entry(alias.clone()).or_default().insert(root);
            }
            aliases_by_root.insert(root, aliases);
        }
        let aliases: BTreeSet<String> = aliases_by_root.values().flatten().cloned().collect();
        let mut scan = HeapScan::new(&tracked, shadowed, &aliases);
        scan.value_flow = Some(value_flow);
        scan.argument_facts = arguments;
        scan.candidate_index = Some(value_flow.candidate_object_index(&roots));
        scan.primitive_members = primitive_members;
        for item in body {
            let module_item = item.as_module_item();
            match module_item {
                // Direct chunk-top var initializers were all shape-checked
                // above. Walk each expression so references to OTHER tracked
                // bindings still count, but keep these known rebinds off the
                // nested-var rebinding path.
                ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) if var.kind == VarDeclKind::Var => {
                    scan.visit_top_level_var(var);
                }
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    if let Decl::Var(var) = &export.decl
                        && var.kind == VarDeclKind::Var
                    {
                        scan.visit_top_level_var(var);
                    } else {
                        module_item.visit_with(&mut scan);
                    }
                }
                _ => module_item.visit_with(&mut scan),
            }
        }
        let HeapScan {
            states,
            rebound,
            argument_dependencies: mut dependencies,
            argument_inference_uses: mut inference_uses,
            ..
        } = scan;
        // An author proof that one alias does not escape keeps the shared
        // allocation private, so every alias of it depends on the proof.
        for aliases in aliases_by_root.values() {
            let mut shared = ProofDependencies::default();
            for alias in aliases {
                if let Some(proof) = dependencies.get(alias) {
                    shared.extend(proof);
                }
            }
            if !shared.is_empty() {
                for alias in aliases {
                    dependencies
                        .entry(alias.clone())
                        .or_default()
                        .extend(&shared);
                }
            }
            let shared_uses: BTreeSet<_> = aliases
                .iter()
                .filter_map(|alias| inference_uses.get(alias))
                .flatten()
                .cloned()
                .collect();
            if !shared_uses.is_empty() {
                for alias in aliases {
                    inference_uses
                        .entry(alias.clone())
                        .or_default()
                        .extend(shared_uses.iter().cloned());
                }
            }
        }
        // A binding denotes its own initializers and every root it may hold.
        let bindings = plain_names
            .into_iter()
            .map(|name| {
                let sites = tracked[&name].clone();
                (name, sites)
            })
            .collect();
        Self {
            states,
            bindings,
            rebound,
            dependencies,
            inference_uses,
        }
    }

    /// `PlainData`: tracked bindings never rebound whose every allocation is
    /// `Private` and `Clean`.
    pub(crate) fn plain_data_bindings(&self) -> PlainDataBindings {
        let bindings: Vec<String> = self
            .bindings
            .iter()
            .filter(|(name, sites)| !self.rebound.contains(*name) && all_plain(&self.states, sites))
            .map(|(name, _)| name.clone())
            .collect();
        let admitted: BTreeSet<&String> = bindings.iter().collect();
        let dependencies = self
            .dependencies
            .iter()
            .filter(|(name, _)| admitted.contains(name))
            .map(|(name, proof)| (name.clone(), proof.clone()))
            .collect();
        let inference_uses = self
            .inference_uses
            .iter()
            .filter(|(name, _)| admitted.contains(name))
            .map(|(name, uses)| (name.clone(), uses.clone()))
            .collect();
        PlainDataBindings {
            bindings,
            dependencies,
            inference_uses,
        }
    }
}

fn all_plain(states: &BTreeMap<Value, ObjectState>, sites: &BTreeSet<Value>) -> bool {
    sites
        .iter()
        .all(|site| states.get(site).copied().unwrap_or_default().is_plain())
}

const SAFE_PLAIN_ARRAY_METHODS: &[&str] = &["filter", "flatMap", "forEach", "map", "slice"];

/// A fresh array literal (`None`), or an array derived from `Some(binding)`
/// by a non-mutating array method.
fn plain_array_source(expr: &Expr) -> Option<Option<String>> {
    match expr.unwrap_parens() {
        Expr::Array(_) => Some(None),
        Expr::Ident(ident) => Some(Some(ident.sym.to_string())),
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return None;
            };
            let Expr::Member(member) = callee.unwrap_parens() else {
                return None;
            };
            if !matches!(
                &member.prop,
                MemberProp::Ident(prop) if ["filter", "flatMap", "map", "slice"].contains(&prop.sym.as_ref())
            ) {
                return None;
            }
            plain_array_source(member.obj.as_ref())
        }
        _ => None,
    }
}

/// Chunk-top `const` arrays, literal or derived from one by a non-mutating
/// method, whose allocation stays `Private` and `Clean` and is never the
/// receiver of another method. Without value flow each binding denotes only
/// its own initializer, and aliasing one is an escape.
pub(crate) fn plain_array_bindings(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
) -> BTreeSet<String> {
    let mut sources = Vec::new();
    let mut tracked = BTreeMap::new();
    for item in body {
        for (target, init, kind) in top_level_initializers(item.as_module_item()) {
            if kind == VarDeclKind::Const
                && let Some(source) = plain_array_source(init)
            {
                tracked.insert(target.clone(), BTreeSet::from([site(init)]));
                sources.push((target, source));
            }
        }
    }
    let candidates = close_const_aliases(&BTreeSet::new(), sources);
    if candidates.is_empty() {
        return BTreeSet::new();
    }
    tracked.retain(|name, _| candidates.contains(name));
    let aliases = BTreeSet::new();
    let mut scan = HeapScan::new(&tracked, shadowed, &aliases);
    for item in body {
        item.as_module_item().visit_with(&mut scan);
    }
    let mut methods = ArrayMethodScan { scan: &mut scan };
    for item in body {
        item.as_module_item().visit_with(&mut methods);
    }
    tracked
        .iter()
        .filter(|(name, sites)| !scan.rebound.contains(*name) && all_plain(&scan.states, sites))
        .map(|(name, _)| name.clone())
        .collect()
}

/// Plain arrays whose every element is a primitive: a literal of primitive
/// expressions, holes and spreads of earlier such arrays, or `filter`/`slice`
/// of one. Each array is also never handed to a callback that could write it:
/// an element callback of `forEach`/`map`/`filter`/`flatMap` on it must be an
/// inline function taking at most the element and index, without
/// `arguments`. `Clean` then keeps the elements primitive for the whole
/// chunk, so iterating the array through an intact `%Array.prototype%`
/// method passes only primitives and reaches no callable through it.
pub(crate) fn primitive_element_arrays(
    body: &[TopLevelItemView<'_>],
    plain_arrays: &BTreeSet<String>,
    prims: &BindingPrims,
) -> BTreeSet<String> {
    if plain_arrays.is_empty() {
        return BTreeSet::new();
    }
    let mut exposure = ArrayExposure {
        plain_arrays,
        exposed: BTreeSet::new(),
    };
    for item in body {
        item.as_module_item().visit_with(&mut exposure);
    }
    let unshadowed = BTreeSet::new();
    let scope = prims.scope(&unshadowed);
    let mut primitive = BTreeSet::new();
    for item in body {
        for (name, init, kind) in top_level_initializers(item.as_module_item()) {
            if kind != VarDeclKind::Const
                || !plain_arrays.contains(&name)
                || exposure.exposed.contains(&name)
            {
                continue;
            }
            let elements_primitive = match init.unwrap_parens() {
                Expr::Array(array) => array.elems.iter().flatten().all(|element| {
                    match (element.spread, element.expr.unwrap_parens()) {
                        (Some(_), source) => filtered_from(source, &primitive),
                        (None, expr) => {
                            evaluate(expr, &scope).is_some_and(|kinds| !kinds.is_empty())
                        }
                    }
                }),
                derived @ Expr::Call(_) => filtered_from(derived, &primitive),
                _ => false,
            };
            if elements_primitive {
                primitive.insert(name);
            }
        }
    }
    primitive
}

/// `source`, possibly through `filter`/`slice` calls, is one of `arrays`:
/// the result's elements are a subset of its elements.
fn filtered_from(expr: &Expr, arrays: &BTreeSet<String>) -> bool {
    match expr.unwrap_parens() {
        Expr::Ident(source) => arrays.contains(source.sym.as_ref()),
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return false;
            };
            let Expr::Member(member) = callee.unwrap_parens() else {
                return false;
            };
            matches!(&member.prop, MemberProp::Ident(method) if matches!(method.sym.as_ref(), "filter" | "slice"))
                && filtered_from(&member.obj, arrays)
        }
        _ => false,
    }
}

/// The identifier receiver of `receiver.method(...)` for one of `methods`.
fn array_method_receiver<'a>(call: &'a CallExpr, methods: &[&str]) -> Option<&'a str> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(member) = callee.unwrap_parens() else {
        return None;
    };
    let (Expr::Ident(receiver), MemberProp::Ident(method)) =
        (member.obj.unwrap_parens(), &member.prop)
    else {
        return None;
    };
    methods
        .contains(&method.sym.as_ref())
        .then_some(receiver.sym.as_ref())
}

/// Plain arrays whose element callback may receive the array itself: its
/// third parameter, `arguments`, or a callback the scan cannot see.
struct ArrayExposure<'a> {
    plain_arrays: &'a BTreeSet<String>,
    exposed: BTreeSet<String>,
}

impl Visit for ArrayExposure<'_> {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Some(receiver) =
            array_method_receiver(node, &["forEach", "map", "filter", "flatMap"])
            && self.plain_arrays.contains(receiver)
            && !node.args.first().is_some_and(|callback| {
                callback.spread.is_none() && sees_only_element(&callback.expr)
            })
        {
            self.exposed.insert(receiver.to_owned());
        }
        node.visit_children_with(self);
    }
}

/// An inline element callback with at most `(element, index)` identifier
/// parameters that never reads `arguments`.
fn sees_only_element(callback: &Expr) -> bool {
    let simple = |pats: &mut dyn Iterator<Item = &Pat>| {
        let pats: Vec<&Pat> = pats.collect();
        pats.len() <= 2 && pats.iter().all(|pat| matches!(pat, Pat::Ident(_)))
    };
    match callback.unwrap_parens() {
        Expr::Arrow(arrow) => simple(&mut arrow.params.iter()),
        Expr::Fn(function) => {
            struct Arguments(bool);
            impl Visit for Arguments {
                fn visit_ident(&mut self, ident: &Ident) {
                    self.0 |= ident.sym.as_ref() == "arguments";
                }
                // A nested function's `arguments` is its own.
                fn visit_function(&mut self, _: &Function) {}
            }
            let mut arguments = Arguments(false);
            function.function.body.visit_with(&mut arguments);
            simple(&mut function.function.params.iter().map(|param| &param.pat)) && !arguments.0
        }
        _ => false,
    }
}

/// Pure syntactic check for a plain-data initializer: an object literal whose
/// own properties are exclusively `KeyValue` / `Shorthand` / object spread
/// (`{...src}`) with non-computed, non-`__proto__` keys, an array literal
/// (holes read as `undefined`, no getter), or for a `var` a TS enum IIFE.
///
/// **Spreads are permitted.** `{...src}` evaluates `src`'s own enumerable
/// properties via `CopyDataProperties` and writes the resulting values to the
/// target via `CreateDataPropertyOrThrow`, which always produces a data
/// descriptor regardless of the source's descriptor shape. The spread itself
/// fires source getters at init (classified independently by the
/// `ObjectSpread`/`ArraySpread` rules), but the resulting receiver has no
/// accessor channels, which is what this check gates.
///
/// Value sub-expressions are not validated: the declaration's own purity is
/// classified separately, and post-init member reads depend only on the
/// receiver's shape and its heap state.
pub(crate) fn is_plain_data_init(expr: &Expr, binding: &str, kind: VarDeclKind) -> bool {
    match expr {
        Expr::Paren(p) => is_plain_data_init(&p.expr, binding, kind),
        Expr::Object(obj) => obj.props.iter().all(is_plain_data_prop),
        Expr::Array(_) => true,
        Expr::Call(_) if kind == VarDeclKind::Var => {
            is_ts_enum_iife_init_for_binding(expr, binding)
        }
        _ => false,
    }
}

pub(crate) fn is_plain_data_prop(prop: &PropOrSpread) -> bool {
    let PropOrSpread::Prop(prop) = prop else {
        // `{...src}`: see `is_plain_data_init`.
        return true;
    };
    match prop.as_ref() {
        Prop::Shorthand(_) => true,
        Prop::KeyValue(kv) => match &kv.key {
            // `{__proto__: X}` in an object literal SETS the prototype
            // (ES262 §13.2.5.5); if X carries accessor properties, reads on
            // the literal fire them. The computed form `{["__proto__"]: X}`
            // does not set the prototype, but computed keys are rejected.
            PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
            PropName::Str(s) => s.value.to_string_lossy() != "__proto__",
            PropName::Num(_) | PropName::BigInt(_) => true,
            PropName::Computed(_) => false,
        },
        // Methods, getters, setters, and `{key=value}` shorthand assign are
        // not data properties.
        Prop::Getter(_) | Prop::Setter(_) | Prop::Method(_) | Prop::Assign(_) => false,
    }
}

/// Whole-chunk scan that publishes or taints tracked allocations and records
/// rebinding of tracked names.
///
/// **Escape default.** Every bare tracked `Ident` the traversal reaches is a
/// captured reference and publishes the binding's allocations, unless the
/// occurrence is one of `Escape::Private`'s positions: the overrides below
/// skip those before traversal reaches the `Ident`. With value flow, a call
/// argument or stored member value also publishes every root it can reach,
/// and a member write receiver taints them, so an untracked alias
/// (`getFoo()`, `box.item`) is followed.
///
/// **Parameter scopes.** A function or arrow parameter named like a tracked
/// binding refers to the parameter in its body, so writes through it do not
/// taint the outer allocation (the TS enum IIFE names its parameter after its
/// binding). Block-scoped shadowing is not tracked; the misses are
/// conservative.
struct HeapScan<'a> {
    /// Tracked names and the allocations each denotes.
    tracked: &'a BTreeMap<String, BTreeSet<Value>>,
    /// Chunk-top shadowed globals (A8): the operand-only builtin exemption
    /// must not fire when the chunk rebinds its receiver.
    shadowed: &'a BTreeSet<&'static str>,
    states: BTreeMap<Value, ObjectState>,
    rebound: BTreeSet<String>,
    /// Tracked names shadowed by the parameters of each enclosing function.
    shadowing_scopes: Vec<BTreeSet<String>>,
    /// Every parameter name of each enclosing function.
    parameter_scopes: Vec<BTreeSet<String>>,
    value_flow: Option<&'a ChunkValueFlow>,
    argument_facts: Option<&'a ArgumentNoEscapeFacts>,
    argument_dependencies: BTreeMap<String, ProofDependencies>,
    argument_inference_uses: BTreeMap<String, BTreeSet<InferenceUse>>,
    candidate_index: Option<CandidateObjectIndex<'a>>,
    /// Chunk-top names value flow says hold a literal root. Declaring one as
    /// another's alias, or returning one, keeps the identity tracked.
    aliases: &'a BTreeSet<String>,
    /// Own primitive fields of `const` object literals: passing `X.k` passes
    /// a primitive, not the receiver.
    primitive_members: BTreeMap<Id, BTreeSet<String>>,
}

impl<'a> HeapScan<'a> {
    fn new(
        tracked: &'a BTreeMap<String, BTreeSet<Value>>,
        shadowed: &'a BTreeSet<&'static str>,
        aliases: &'a BTreeSet<String>,
    ) -> Self {
        Self {
            tracked,
            shadowed,
            states: BTreeMap::new(),
            rebound: BTreeSet::new(),
            shadowing_scopes: Vec::new(),
            parameter_scopes: Vec::new(),
            value_flow: None,
            argument_facts: None,
            argument_dependencies: BTreeMap::new(),
            argument_inference_uses: BTreeMap::new(),
            candidate_index: None,
            aliases,
            primitive_members: BTreeMap::new(),
        }
    }
}

impl HeapScan<'_> {
    fn is_shadowed(&self, name: &str) -> bool {
        self.shadowing_scopes
            .iter()
            .any(|scope| scope.contains(name))
    }

    fn with_scope<F: FnOnce(&mut Self)>(
        &mut self,
        scope: BTreeSet<String>,
        params: BTreeSet<String>,
        f: F,
    ) {
        self.shadowing_scopes.push(scope);
        self.parameter_scopes.push(params);
        f(self);
        self.parameter_scopes.pop();
        self.shadowing_scopes.pop();
    }

    /// Tracked names bound by `params`. Defaults, rest and destructuring all
    /// bind for the whole parameter scope, so in emitted helpers like
    /// `(i, m = helper, d = m.f || (m.f = [])) => ...` the `m.f = []` write
    /// targets the parameter `m`, not a chunk-top allocation bound to `m`.
    fn shadowed_by_params<'p, I>(&self, params: I) -> BTreeSet<String>
    where
        I: IntoIterator<Item = &'p Pat>,
    {
        let mut out = BTreeSet::new();
        for param in params {
            self.collect_tracked_in_pat(param, &mut out);
        }
        out
    }

    fn collect_tracked_in_pat(&self, pat: &Pat, out: &mut BTreeSet<String>) {
        match pat {
            Pat::Ident(ident) => {
                let name = ident.id.sym.as_ref();
                if self.tracked.contains_key(name) {
                    out.insert(name.to_string());
                }
            }
            Pat::Rest(rest) => self.collect_tracked_in_pat(&rest.arg, out),
            Pat::Assign(assign) => self.collect_tracked_in_pat(&assign.left, out),
            Pat::Array(array) => {
                for elem in array.elems.iter().flatten() {
                    self.collect_tracked_in_pat(elem, out);
                }
            }
            Pat::Object(object) => {
                for prop in &object.props {
                    match prop {
                        ObjectPatProp::KeyValue(kv) => self.collect_tracked_in_pat(&kv.value, out),
                        ObjectPatProp::Assign(assign) => {
                            let name = assign.key.id.sym.as_ref();
                            if self.tracked.contains_key(name) {
                                out.insert(name.to_string());
                            }
                        }
                        ObjectPatProp::Rest(rest) => self.collect_tracked_in_pat(&rest.arg, out),
                    }
                }
            }
            Pat::Invalid(_) | Pat::Expr(_) => {}
        }
    }

    fn apply(&mut self, site: Value, effect: HeapEffect) {
        let state = self.states.entry(site).or_default();
        match effect {
            HeapEffect::Publish => state.escape = Escape::Published,
            HeapEffect::Taint => state.taint = Taint::Tainted,
        }
    }

    /// Apply `effect` to the allocations the chunk-top binding `name` denotes.
    fn apply_to_name(&mut self, name: &str, effect: HeapEffect) {
        if self.is_shadowed(name) {
            return;
        }
        let Some(sites) = self.tracked.get(name) else {
            return;
        };
        for &site in sites {
            self.apply(site, effect);
        }
    }

    fn rebind(&mut self, name: &str) {
        if !self.is_shadowed(name) && self.tracked.contains_key(name) {
            self.rebound.insert(name.to_string());
        }
    }

    /// Apply `effect` to every root value flow says `expr` can reach.
    fn apply_to_reachable(&mut self, expr: &Expr, effect: HeapEffect) {
        let Some(flow) = self.value_flow else {
            return;
        };
        struct ReferencedIds<'a> {
            ids: BTreeSet<Id>,
            primitive_members: &'a BTreeMap<Id, BTreeSet<String>>,
        }
        impl Visit for ReferencedIds<'_> {
            fn visit_ident(&mut self, ident: &Ident) {
                self.ids.insert(ident.to_id());
            }

            fn visit_member_expr(&mut self, member: &MemberExpr) {
                if let Expr::Ident(receiver) = member.obj.unwrap_parens()
                    && let MemberProp::Ident(property) = &member.prop
                    && self
                        .primitive_members
                        .get(&receiver.to_id())
                        .is_some_and(|keys| keys.contains(property.sym.as_ref()))
                {
                    // Passing the literal value cannot expose its receiver.
                    return;
                }
                member.visit_children_with(self);
            }
        }
        let mut referenced = ReferencedIds {
            ids: BTreeSet::new(),
            primitive_members: &self.primitive_members,
        };
        expr.visit_with(&mut referenced);
        let index = self
            .candidate_index
            .as_ref()
            .expect("value flow comes with its candidate index");
        let mut reached = BTreeSet::new();
        if referenced.ids.is_empty() {
            index.for_values(&flow.values_of_expr(expr), |root| {
                reached.insert(root);
            });
        }
        for id in referenced.ids {
            // A parameter receives values only through call arguments, which
            // publish them unless an author proof says the callee keeps them.
            if self
                .parameter_scopes
                .iter()
                .any(|scope| scope.contains(id.0.as_ref()))
            {
                continue;
            }
            index.for_binding(&id, |root| {
                reached.insert(root);
            });
        }
        for root in reached {
            self.apply(root, effect);
        }
    }

    /// A nested `var` initializer or loop head may replace a tracked binding
    /// with any runtime value.
    fn rebind_var_pattern(&mut self, pat: &Pat) {
        let mut names = BTreeSet::new();
        self.collect_tracked_in_pat(pat, &mut names);
        for name in names {
            self.rebind(&name);
        }
    }

    /// Taint the binding a member target's receiver names, through `Paren`
    /// wrappers and optional chains.
    fn taint_member_receiver(&mut self, expr: &Expr) {
        let mut cur = expr;
        loop {
            match cur {
                Expr::Paren(p) => cur = &p.expr,
                Expr::Member(member) => {
                    if let Expr::Ident(recv) = member.obj.as_ref() {
                        self.apply_to_name(recv.sym.as_ref(), HeapEffect::Taint);
                    }
                    return;
                }
                Expr::OptChain(opt) => match &*opt.base {
                    OptChainBase::Member(member) => {
                        if let Expr::Ident(recv) = member.obj.as_ref() {
                            self.apply_to_name(recv.sym.as_ref(), HeapEffect::Taint);
                        }
                        return;
                    }
                    OptChainBase::Call(_) => return,
                },
                _ => return,
            }
        }
    }

    fn visit_top_level_var(&mut self, var: &VarDecl) {
        for decl in &var.decls {
            decl.name.visit_with(self);
            if let Some(init) = decl.init.as_deref() {
                init.visit_with(self);
            }
        }
    }
}

/// Tracked names anywhere in a compound assignment target.
struct TrackedIdents<'a> {
    tracked: &'a BTreeMap<String, BTreeSet<Value>>,
    hit: BTreeSet<String>,
}

impl Visit for TrackedIdents<'_> {
    fn visit_ident(&mut self, node: &Ident) {
        if self.tracked.contains_key(node.sym.as_ref()) {
            self.hit.insert(node.sym.to_string());
        }
    }
}

impl Visit for HeapScan<'_> {
    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        // A tracked chunk-top alias keeps the identity visible to this scan;
        // its later uses are visited under its own tracked name.
        if self.value_flow.is_some()
            && let Pat::Ident(binding) = &node.name
            && self.aliases.contains(binding.id.sym.as_ref())
            && let Some(init) = node.init.as_deref()
            && let Expr::Ident(source) = init.unwrap_parens()
            && self.aliases.contains(source.sym.as_ref())
        {
            node.name.visit_with(self);
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_var_decl(&mut self, node: &VarDecl) {
        if node.kind == VarDeclKind::Var {
            for decl in &node.decls {
                if decl.init.is_some() {
                    self.rebind_var_pattern(&decl.name);
                }
            }
        }
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        if let ForHead::VarDecl(var) = &node.left {
            for decl in &var.decls {
                // The loop assigns each enumerated key into the binding.
                self.rebind_var_pattern(&decl.name);
            }
        }
        node.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        if let ForHead::VarDecl(var) = &node.left {
            for decl in &var.decls {
                // The loop assigns each yielded value into the binding.
                self.rebind_var_pattern(&decl.name);
            }
        }
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        match &node.left {
            // A member write. Even a data-property write taints: `PlainData`
            // member reads are reorderable only while no write reaches them.
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                self.apply_to_reachable(&member.obj, HeapEffect::Taint);
                if let Expr::Ident(recv) = member.obj.as_ref() {
                    self.apply_to_name(recv.sym.as_ref(), HeapEffect::Taint);
                }
                self.apply_to_reachable(&node.right, HeapEffect::Publish);
            }
            // `X = rhs` keeps the binding tracked only when `rhs` is itself a
            // plain-data literal: anything else may produce an
            // accessor-bearing value. A compound operator produces a
            // primitive, never a literal.
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                if !is_plain_data_init(&node.right, binding.sym.as_ref(), VarDeclKind::Let) {
                    self.rebind(binding.sym.as_ref());
                }
            }
            // Destructuring and other compound targets: a tracked name
            // anywhere in one may be rebound or written through.
            _ => {
                let mut idents = TrackedIdents {
                    tracked: self.tracked,
                    hit: BTreeSet::new(),
                };
                node.left.visit_with(&mut idents);
                for name in idents.hit {
                    self.rebind(&name);
                    self.apply_to_name(&name, HeapEffect::Taint);
                }
            }
        }
        node.right.visit_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        if let Expr::Ident(ident) = node.arg.as_ref() {
            self.rebind(ident.sym.as_ref());
        }
        self.taint_member_receiver(&node.arg);
        if let Expr::Member(member) = node.arg.unwrap_parens() {
            self.apply_to_reachable(&member.obj, HeapEffect::Taint);
        }
        node.visit_children_with(self);
    }

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete {
            self.taint_member_receiver(&node.arg);
            if let Expr::Member(member) = node.arg.unwrap_parens() {
                self.apply_to_reachable(&member.obj, HeapEffect::Taint);
            }
        }
        // `typeof X` / `!X` / `void X` inspect the value transiently.
        if matches!(node.op, UnaryOp::TypeOf | UnaryOp::Bang | UnaryOp::Void)
            && matches!(node.arg.unwrap_parens(), Expr::Ident(_))
        {
            return;
        }
        node.visit_children_with(self);
    }

    // The escape default; see `HeapScan`.
    fn visit_ident(&mut self, node: &Ident) {
        self.apply_to_name(node.sym.as_ref(), HeapEffect::Publish);
    }

    // Binding positions introduce a binding rather than reading one.
    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    // `export { X }` does not publish; see `Escape::Private`.
    fn visit_export_named_specifier(&mut self, _node: &ExportNamedSpecifier) {}

    // The imported side of `import { a as b }` names the source module's
    // export, not a local binding: bundles import hundreds of single-letter
    // vendor names that would otherwise collide with local tracked names.
    fn visit_import_named_specifier(&mut self, _node: &ImportNamedSpecifier) {}

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let Some(arg) = node.arg.as_deref()
            && let Expr::Ident(ident) = arg.unwrap_parens()
            && self.aliases.contains(ident.sym.as_ref())
        {
            return;
        }
        node.visit_children_with(self);
    }

    // An identifier member receiver is read, not captured.
    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if !matches!(node.obj.unwrap_parens(), Expr::Ident(_)) {
            node.obj.visit_with(self);
        }
        node.prop.visit_with(self);
    }

    // A spread source is copied or iterated, not captured.
    fn visit_spread_element(&mut self, node: &SpreadElement) {
        if matches!(node.expr.unwrap_parens(), Expr::Ident(_)) {
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_expr_or_spread(&mut self, node: &ExprOrSpread) {
        if node.spread.is_some() && matches!(node.expr.unwrap_parens(), Expr::Ident(_)) {
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        // A one-argument intrinsic call whose row only inspects its operand
        // (`Object.{keys,values,entries,freeze,fromEntries}(X)`), with the
        // receiver unshadowed, installs no accessor and keeps no reference.
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.as_ref()
            && let Some((receiver, property)) = static_member(member)
            && !self.shadowed.contains(receiver)
            && builtins::lookup(receiver, Some(property), Invocation::Call)
                .is_some_and(only_inspects_operand)
            && node.args.len() == 1
            && node.args[0].spread.is_none()
            && matches!(node.args[0].expr.unwrap_parens(), Expr::Ident(_))
        {
            return;
        }
        // The vetted `X || (X = {})` argument of a recognized TS enum IIFE:
        // its callee writes only through its own parameter, which the
        // parameter scope keeps off the outer binding.
        if let Some(binding) = ts_enum_iife_argument_binding(node)
            && self.tracked.contains_key(binding)
            && !self.is_shadowed(binding)
            && is_ts_enum_iife_call_for_binding(node, binding)
        {
            node.callee.visit_with(self);
            return;
        }
        let protected: BTreeMap<usize, _> =
            match (&node.callee, self.value_flow, self.argument_facts) {
                (Callee::Expr(callee), Some(flow), Some(facts))
                    if matches!(callee.unwrap_parens(), Expr::Ident(_))
                        && node.args.iter().all(|argument| argument.spread.is_none()) =>
                {
                    let targets = flow.call_targets(callee);
                    if targets.complete && targets.bindings.len() == 1 {
                        targets
                            .bindings
                            .iter()
                            .next()
                            .map(|binding| {
                                node.args
                                    .iter()
                                    .enumerate()
                                    .filter_map(|(index, argument)| {
                                        if matches!(argument.expr.unwrap_parens(), Expr::Ident(_)) {
                                            facts
                                                .proof(binding, index, node.args.len())
                                                .map(|proof| (index, proof))
                                        } else {
                                            None
                                        }
                                    })
                                    .collect()
                            })
                            .unwrap_or_default()
                    } else {
                        BTreeMap::new()
                    }
                }
                _ => BTreeMap::new(),
            };
        for (index, proof) in &protected {
            if let Expr::Ident(argument) = node.args[*index].expr.unwrap_parens()
                && self.tracked.contains_key(argument.sym.as_ref())
                && !self.is_shadowed(argument.sym.as_ref())
            {
                self.argument_dependencies
                    .entry(argument.sym.to_string())
                    .or_default()
                    .extend(&proof.dependencies);
                self.argument_inference_uses
                    .entry(argument.sym.to_string())
                    .or_default()
                    .insert(proof.inference_use.clone());
            }
        }
        for (index, arg) in node.args.iter().enumerate() {
            if !protected.contains_key(&index) {
                self.apply_to_reachable(&arg.expr, HeapEffect::Publish);
            }
        }
        node.callee.visit_with(self);
        for (index, arg) in node.args.iter().enumerate() {
            if !protected.contains_key(&index) {
                arg.visit_with(self);
            }
        }
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let scope = self.shadowed_by_params(node.params.iter());
        let params = node
            .params
            .iter()
            .flat_map(binding_targets::binding_names)
            .map(|id| id.0.to_string())
            .collect();
        self.with_scope(scope, params, |s| {
            for param in &node.params {
                param.visit_with(s);
            }
            match node.body.as_ref() {
                ArrowFunctionBody::Expr(expr)
                    if matches!(expr.unwrap_parens(), Expr::Ident(ident) if s.aliases.contains(ident.sym.as_ref())) => {}
                body => body.visit_with(s),
            }
        });
    }

    fn visit_function(&mut self, node: &Function) {
        let scope = self.shadowed_by_params(node.params.iter().map(|p| &p.pat));
        let params = node
            .params
            .iter()
            .flat_map(|p| binding_targets::binding_names(&p.pat))
            .map(|id| id.0.to_string())
            .collect();
        self.with_scope(scope, params, |s| node.visit_children_with(s));
    }
}

/// Taints a tracked array named as the receiver of any method other than a
/// non-mutating one: the method may write its contents or invoke elements.
struct ArrayMethodScan<'s, 'a> {
    scan: &'s mut HeapScan<'a>,
}

impl Visit for ArrayMethodScan<'_, '_> {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && let Expr::Ident(recv) = member.obj.as_ref().unwrap_parens()
        {
            let allowed = match &member.prop {
                MemberProp::Ident(prop) => SAFE_PLAIN_ARRAY_METHODS.contains(&prop.sym.as_ref()),
                MemberProp::Computed(_) | MemberProp::PrivateName(_) => false,
            };
            if !allowed {
                self.scan
                    .apply_to_name(recv.sym.as_ref(), HeapEffect::Taint);
            }
        }
        node.visit_children_with(self);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let scope = self.scan.shadowed_by_params(node.params.iter());
        self.scan.shadowing_scopes.push(scope);
        for param in &node.params {
            param.visit_with(self);
        }
        node.body.visit_with(self);
        self.scan.shadowing_scopes.pop();
    }

    fn visit_function(&mut self, node: &Function) {
        let scope = self
            .scan
            .shadowed_by_params(node.params.iter().map(|p| &p.pat));
        self.scan.shadowing_scopes.push(scope);
        node.visit_children_with(self);
        self.scan.shadowing_scopes.pop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::{compute_shadowed_globals, top_level_item_views};

    fn plain_data(source: &str) -> Vec<String> {
        let module = raw_js_test_support::parse(source);
        let body = top_level_item_views(&module.body);
        let flow = ChunkValueFlow::analyze_with_candidates(&body, &tracked_allocation_roots(&body));
        ChunkHeap::analyze(&body, &compute_shadowed_globals(&body), &flow, None)
            .plain_data_bindings()
            .bindings
    }

    #[test]
    fn writes_and_escapes_through_any_alias_reach_the_allocation() {
        for source in [
            "const foo = { v: 1 }; const alias = foo; alias.v = 2;",
            "const foo = { v: 1 }; function get() { return foo; } get().v = 2;",
            "const foo = { v: 1 }; function get() { return foo; } const box = { item: get() }; delete box.item.v;",
            "const foo = { v: 1 }; const alias = foo; sink(alias);",
            "const foo = { v: 1 }; window.held = [foo];",
        ] {
            assert_eq!(plain_data(source), Vec::<String>::new(), "{source}");
        }
    }

    #[test]
    fn rebinding_is_a_fact_of_the_rebound_name() {
        assert_eq!(
            plain_data("const foo = { v: 1 }; let alias = foo; alias = make();"),
            ["foo"],
        );
        assert_eq!(
            plain_data("let foo = { v: 1 }; foo = make();"),
            Vec::<String>::new(),
        );
    }

    fn primitive_arrays(source: &str) -> BTreeSet<String> {
        let module = raw_js_test_support::parse(source);
        let body = top_level_item_views(&module.body);
        let plain = plain_array_bindings(&body, &compute_shadowed_globals(&body));
        let prims = crate::facts::abstract_domain::primitive::collect_binding_prims(
            &body,
            &BTreeSet::new(),
            &BTreeSet::new(),
        );
        primitive_element_arrays(&body, &plain, &prims)
    }

    #[test]
    fn primitive_element_arrays_hold_only_primitives_no_callback_can_replace() {
        let source = r#"
const name = "n";
const literal = [1, name, , `t${name}`];
const spread = [...literal, 2];
const kept = spread.filter((value, index) => index).slice();
const objects = [{ run: () => later }];
const mapped = literal.map((value) => value);
const writer = [1];
writer.forEach((value, index, array) => { array[index] = { valueOf: sideEffect }; });
const passed = [1];
passed.forEach(sideEffect);
export { literal, spread, kept, objects, mapped, writer, passed };
"#;
        assert_eq!(
            primitive_arrays(source),
            BTreeSet::from(["kept", "literal", "spread"].map(String::from)),
        );
    }
}
