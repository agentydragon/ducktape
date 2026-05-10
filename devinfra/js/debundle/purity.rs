use std::collections::{BTreeMap, BTreeSet};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use crate::SourceLocation;
use crate::facts::TopLevelItemView;

/// Chunk-wide code graph: indexes top-level bindings and answers
/// queries the classifier needs that go beyond per-expression
/// inspection. Currently exposes function-body purity for
/// chunk-local Ident callees (used by `classify_call_purity` to
/// short-circuit `Pure` callees). Designed to grow into a fuller
/// binding-shape model — import provenance, var-init purity,
/// class shape, etc. — as further analyses land. New binding
/// kinds add new `ChunkBinding` variants and matching query
/// methods; the iteration in `ChunkCodeGraph::build` extends to
/// them naturally.
#[derive(Debug, Default, Clone)]
pub struct ChunkCodeGraph {
    bindings: BTreeMap<String, ChunkBinding>,
}

#[derive(Debug, Clone)]
enum ChunkBinding {
    /// Chunk-top function declaration or `const f = function/arrow`.
    /// `purity` is the worst purity reachable from the body, computed
    /// by fixed-point iteration over all chunk-top functions.
    Function { purity: Purity },
}

impl ChunkCodeGraph {
    /// Build the graph for `body`. Two phases:
    ///
    /// 1. **Call-graph construction.** For each chunk-top function,
    ///    walk its body and collect the set of other chunk-top
    ///    functions it calls (Ident-callee form only). Edges:
    ///    caller → callee.
    /// 2. **SCC-bottom-up classification.** Decompose the call
    ///    graph into strongly-connected components via
    ///    `petgraph::algo::tarjan_scc` (returns SCCs in reverse
    ///    topological order — sinks first). Process each SCC in
    ///    order, so by the time we classify a caller, every
    ///    callee outside the caller's own SCC is already
    ///    finalized. Within an SCC (the only place mutual
    ///    recursion shows up), iterate via a worklist: re-classify
    ///    a function only when one of its same-SCC callees has
    ///    changed. Each function in an SCC is reclassified at
    ///    most twice (`Pure → Unknown` or `Pure → Impure`, both
    ///    terminal), so per-SCC work is `O(scc_size · body_size)`,
    ///    and total work is `O(N · body_size)` for the whole
    ///    chunk regardless of recursion depth.
    pub(crate) fn build(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
    ) -> Self {
        let functions = collect_chunk_functions(body);
        let name_to_idx: BTreeMap<&str, usize> = functions
            .iter()
            .enumerate()
            .map(|(i, f)| (f.name.as_str(), i))
            .collect();

        // Phase 1: call edges.
        let mut call_graph: DiGraphMap<usize, ()> = DiGraphMap::new();
        let mut callees_of: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); functions.len()];
        for (i, function) in functions.iter().enumerate() {
            call_graph.add_node(i);
            let mut collector = CallCollector {
                callees: BTreeSet::new(),
                name_to_idx: &name_to_idx,
            };
            function.visit_body_with(&mut collector);
            for &callee in &collector.callees {
                call_graph.add_edge(i, callee, ());
            }
            callees_of[i] = collector.callees;
        }

        // Phase 2: optimistic init + SCC-bottom-up classification.
        let mut graph = ChunkCodeGraph {
            bindings: functions
                .iter()
                .map(|f| {
                    (
                        f.name.clone(),
                        ChunkBinding::Function {
                            purity: Purity::Pure,
                        },
                    )
                })
                .collect(),
        };
        // tarjan_scc emits SCCs in reverse topological order: leaves
        // (sinks — functions that don't call any chunk-top
        // function) come first, callers come later.
        for scc in tarjan_scc(&call_graph) {
            graph.classify_scc(&scc, &functions, &callees_of, shadowed, declared_pure);
        }
        graph
    }

    /// Re-classify every function in `scc` until no purity changes.
    /// Worklist-driven: only re-process a function when one of its
    /// same-SCC callees has changed (cross-SCC callees are already
    /// finalized by bottom-up SCC ordering).
    fn classify_scc(
        &mut self,
        scc: &[usize],
        functions: &[ChunkFunction<'_>],
        callees_of: &[BTreeSet<usize>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
    ) {
        let scc_set: BTreeSet<usize> = scc.iter().copied().collect();
        // Reverse adjacency restricted to this SCC: callee → callers.
        let mut callers_in_scc: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for &i in scc {
            for &callee in &callees_of[i] {
                if scc_set.contains(&callee) {
                    callers_in_scc.entry(callee).or_default().push(i);
                }
            }
        }
        let mut pending: BTreeSet<usize> = scc_set;
        while let Some(&i) = pending.iter().next() {
            pending.remove(&i);
            let new_purity = classify_function_body(&functions[i], shadowed, declared_pure, self);
            let name = &functions[i].name;
            let was_pure = self
                .function_purity(name)
                .expect("seeded by build")
                .is_pure();
            // Monotone fixed-point: Pure → NotPure is the only
            // useful transition. Once a function is classified
            // NotPure, re-classifying with concatenated reasons
            // would diverge (the reason list grows every
            // iteration), so we settle on the first NotPure
            // verdict and stop.
            if was_pure && !new_purity.is_pure() {
                self.bindings
                    .insert(name.clone(), ChunkBinding::Function { purity: new_purity });
                if let Some(callers) = callers_in_scc.get(&i) {
                    pending.extend(callers.iter().copied());
                }
            }
        }
    }

    /// Purity of the chunk-local function bound to `name`, if any.
    /// Returns `None` for non-function bindings (imports, vars,
    /// classes) and for names not bound at chunk top.
    pub(crate) fn function_purity(&self, name: &str) -> Option<Purity> {
        match self.bindings.get(name)? {
            ChunkBinding::Function { purity } => Some(purity.clone()),
        }
    }
}

/// Visitor that collects the indices of other chunk-top functions
/// called by a function body (Ident-callee form only). Skips
/// nested function/arrow/method bodies (those are separate lazy
/// scopes — their callees go to their own graph entries).
struct CallCollector<'a> {
    callees: BTreeSet<usize>,
    name_to_idx: &'a BTreeMap<&'a str, usize>,
}

impl Visit for CallCollector<'_> {
    fn visit_function(&mut self, _: &Function) {}
    fn visit_arrow_expr(&mut self, _: &ArrowExpr) {}
    fn visit_method_prop(&mut self, _: &MethodProp) {}
    fn visit_getter_prop(&mut self, _: &GetterProp) {}
    fn visit_setter_prop(&mut self, _: &SetterProp) {}

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Ident(id) = callee.as_ref()
            && let Some(&idx) = self.name_to_idx.get(id.sym.as_ref())
        {
            self.callees.insert(idx);
        }
        // Recurse to find nested calls in args / receiver.
        call.visit_children_with(self);
    }
}

#[derive(Debug, Clone)]
struct ChunkFunction<'a> {
    name: String,
    /// Block-bodied function/arrow.
    block_body: Option<&'a BlockStmt>,
    /// Concise-arrow expression body (`(x) => expr`).
    expr_body: Option<&'a Expr>,
}

impl ChunkFunction<'_> {
    /// Drive a `Visit` visitor over this function's body. Block
    /// bodies recurse via `visit_with`; concise-arrow expression
    /// bodies fire `visit_expr` directly so the visitor's
    /// `visit_call_expr` / `visit_expr` overrides catch the body.
    fn visit_body_with<V: Visit + ?Sized>(&self, visitor: &mut V) {
        if let Some(block) = self.block_body {
            block.visit_with(visitor);
        }
        if let Some(expr) = self.expr_body {
            expr.visit_with(visitor);
        }
    }
}

fn collect_chunk_functions<'a, 'item>(
    body: &'a [TopLevelItemView<'item>],
) -> Vec<ChunkFunction<'a>> {
    let mut out = Vec::new();
    for item in body {
        match item.as_module_item() {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(fn_decl))) => push_fn_decl(fn_decl, &mut out),
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => push_var_functions(var, &mut out),
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
                Decl::Fn(fn_decl) => push_fn_decl(fn_decl, &mut out),
                Decl::Var(var) => push_var_functions(var, &mut out),
                _ => {}
            },
            _ => {}
        }
    }
    out
}

fn push_fn_decl<'a>(fn_decl: &'a FnDecl, out: &mut Vec<ChunkFunction<'a>>) {
    out.push(ChunkFunction {
        name: fn_decl.ident.sym.to_string(),
        block_body: fn_decl.function.body.as_ref(),
        expr_body: None,
    });
}

fn push_var_functions<'a>(var: &'a VarDecl, out: &mut Vec<ChunkFunction<'a>>) {
    // `let` / `var` bindings are reassignable: caching their
    // body's purity and short-circuiting `f(...)` to that purity
    // is unsound if a later `f = …` reassigns them to something
    // impure. Only `const`-bound function/arrow initializers are
    // tracked; reassignment of a `const` is a syntax error.
    if var.kind != VarDeclKind::Const {
        return;
    }
    for decl in &var.decls {
        let Pat::Ident(binding) = &decl.name else {
            continue;
        };
        let Some(init) = decl.init.as_deref() else {
            continue;
        };
        let name = binding.id.sym.to_string();
        match init {
            Expr::Fn(fn_expr) => {
                out.push(ChunkFunction {
                    name,
                    block_body: fn_expr.function.body.as_ref(),
                    expr_body: None,
                });
            }
            Expr::Arrow(arrow) => match arrow.body.as_ref() {
                BlockStmtOrExpr::BlockStmt(block) => {
                    out.push(ChunkFunction {
                        name,
                        block_body: Some(block),
                        expr_body: None,
                    });
                }
                BlockStmtOrExpr::Expr(expr) => {
                    out.push(ChunkFunction {
                        name,
                        block_body: None,
                        expr_body: Some(expr.as_ref()),
                    });
                }
            },
            _ => {}
        }
    }
}

fn classify_function_body(
    function: &ChunkFunction<'_>,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let mut collector = BodyPurityCollector {
        purity: Purity::Pure,
        shadowed,
        declared_pure,
        graph,
    };
    function.visit_body_with(&mut collector);
    collector.purity
}

/// Visitor that walks a function body and accumulates the worst
/// purity of every top-level expression encountered. Skips nested
/// function/arrow/method/getter/setter bodies (those are separate
/// lazy scopes — their purity, if needed, comes from their own
/// graph entry or from the caller's `Unknown` fallback).
struct BodyPurityCollector<'a> {
    purity: Purity,
    shadowed: &'a BTreeSet<&'static str>,
    declared_pure: &'a BTreeSet<String>,
    graph: &'a ChunkCodeGraph,
}

impl Visit for BodyPurityCollector<'_> {
    fn visit_function(&mut self, _: &Function) {}
    fn visit_arrow_expr(&mut self, _: &ArrowExpr) {}
    fn visit_method_prop(&mut self, _: &MethodProp) {}
    fn visit_getter_prop(&mut self, _: &GetterProp) {}
    fn visit_setter_prop(&mut self, _: &SetterProp) {}

    fn visit_expr(&mut self, expr: &Expr) {
        // Classify the entire expression in one shot —
        // `classify_expr_purity` already recurses through
        // nested subexpressions and returns the worst.
        let p = classify_expr_purity(expr, self.shadowed, self.declared_pure, self.graph);
        self.purity = std::mem::replace(&mut self.purity, Purity::Pure).worst(p);
    }

    // Statement-level effects that don't surface as an Impure /
    // Unknown sub-expression. `throw e` alters control flow
    // observably even when `e` is a Pure literal; `debugger`
    // pauses execution observably to a host attached to the
    // process. Both make the enclosing function not Pure.
    fn visit_throw_stmt(&mut self, node: &ThrowStmt) {
        let throw_reason = Purity::from_reason(PurityRule::ThrowStmt, node.span);
        self.purity = std::mem::replace(&mut self.purity, Purity::Pure).worst(throw_reason);
        // Still recurse so the thrown expression contributes its
        // own purity (e.g. `throw io()` should also see the call).
        node.arg.visit_with(self);
    }

    fn visit_debugger_stmt(&mut self, node: &DebuggerStmt) {
        let dbg_reason = Purity::from_reason(PurityRule::DebuggerStmt, node.span);
        self.purity = std::mem::replace(&mut self.purity, Purity::Pure).worst(dbg_reason);
    }
}

/// Two-state expression-level purity with structured reasons.
///
/// `Pure` means the expression is statically provably free of
/// observable side effects; `NotPure { reasons }` carries the
/// list of every classifier rule that fired against the expression
/// or one of its sub-expressions (in source order). The classifier
/// previously distinguished `Impure` from `Unknown` for an internal
/// soundness argument, but downstream consumers (owner-graph
/// `has_side_effect`) collapsed both to "not pure"; this type
/// matches that contract and replaces the bool with the full
/// rationale.
///
/// Reasons collected by `Purity::worst` are concatenated, so a
/// composite like `f() + g()` records both `UnknownCall` reasons
/// (with their respective spans), rather than only the first.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Purity {
    Pure,
    NotPure { reasons: Vec<PurityReason> },
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct PurityReason {
    pub rule: PurityRule,
    /// Resolved by `resolve_reason_locations` once the per-chunk
    /// `line_range_for_span` is in scope (inside
    /// `analyze_item_facts`). The classifier itself only fills
    /// `span` — the wire-emitted reason has `source_location`
    /// populated and `span` skipped.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(skip)]
    pub span: Span,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PurityRule {
    AssignOrUpdate,
    AwaitOrYield,
    DeleteOperator,
    ThrowStmt,
    DebuggerStmt,
    UnknownCall,
    UnknownNew,
    UnknownMember,
    SuperProp,
    TaggedTpl,
    ArraySpread,
    ObjectSpread,
    ObjectAssignProp,
    ClassStaticObservable,
    BareControlFlow,
    Other,
}

impl Purity {
    pub fn is_pure(&self) -> bool {
        matches!(self, Purity::Pure)
    }

    /// Combine two purity verdicts. `Pure` is the identity;
    /// concatenating `NotPure` reasons preserves every offending
    /// sub-expression in source order.
    pub fn worst(self, other: Self) -> Self {
        match (self, other) {
            (Purity::Pure, x) | (x, Purity::Pure) => x,
            (Purity::NotPure { reasons: mut a }, Purity::NotPure { reasons: b }) => {
                a.extend(b);
                Purity::NotPure { reasons: a }
            }
        }
    }

    fn from_reason(rule: PurityRule, span: Span) -> Self {
        Purity::NotPure {
            reasons: vec![PurityReason {
                rule,
                span,
                source_location: None,
                detail: None,
            }],
        }
    }

    fn from_reason_with_detail(rule: PurityRule, span: Span, detail: String) -> Self {
        Purity::NotPure {
            reasons: vec![PurityReason {
                rule,
                span,
                source_location: None,
                detail: Some(detail),
            }],
        }
    }
}

/// Static-property reads on these globals are Pure (no
/// observable side effect, no getter to fire). Indexed as
/// `(receiver_ident, property_name)`.
const PURE_STATIC_PROPS: &[(&str, &str)] = &[
    ("Math", "PI"),
    ("Math", "E"),
    ("Math", "LN2"),
    ("Math", "LN10"),
    ("Math", "LOG2E"),
    ("Math", "LOG10E"),
    ("Math", "SQRT2"),
    ("Math", "SQRT1_2"),
    ("Number", "EPSILON"),
    ("Number", "MAX_SAFE_INTEGER"),
    ("Number", "MIN_SAFE_INTEGER"),
    ("Number", "MAX_VALUE"),
    ("Number", "MIN_VALUE"),
    ("Number", "POSITIVE_INFINITY"),
    ("Number", "NEGATIVE_INFINITY"),
    ("Number", "NaN"),
    ("Symbol", "iterator"),
    ("Symbol", "asyncIterator"),
    ("Symbol", "toStringTag"),
    ("Symbol", "toPrimitive"),
    ("Symbol", "hasInstance"),
    ("Symbol", "species"),
    ("Symbol", "isConcatSpreadable"),
    ("Symbol", "match"),
    ("Symbol", "replace"),
    ("Symbol", "search"),
    ("Symbol", "split"),
];

/// Static methods that are Pure regardless of argument values.
/// Everything in this table must satisfy: per ECMA-262, the call
/// fires no user-defined code on any argument type — no `ToNumber`
/// / `ToString` / `ToPrimitive` / `ToPropertyKey` coercion, no
/// iterator protocol, no proxy trap, no own-property `[[Get]]`,
/// no mutation of any reachable object. See DESIGN.md A8 for the
/// admission contract; AGENTS.md "Pure-call whitelist soundness"
/// for the agent-facing rule. New entries land only with a spec
/// citation showing no user-callback path; "common in practice"
/// is not sufficient.
const PURE_STATIC_CALLS: &[(&str, &str)] = &[
    // Type predicate — checks the IsArray internal slot. Spec
    // explicitly says: "does not perform a call to ToObject on its
    // argument".
    ("Array", "isArray"),
    // Number predicates — `Type(arg) is not Number ⇒ false`,
    // otherwise inspect the value. No coercion path.
    ("Number", "isFinite"),
    ("Number", "isInteger"),
    ("Number", "isNaN"),
    ("Number", "isSafeInteger"),
];

/// Pure global callables (no receiver). Same admission contract as
/// `PURE_STATIC_CALLS`: the call must fire no user code on any
/// argument value.
const PURE_GLOBAL_CALLS: &[&str] = &[
    // ToBoolean is type-cased and fires no callbacks (objects are
    // unconditionally `true`; primitives are checked structurally).
    "Boolean",
];

/// Pure global callables when every argument is a primitive
/// literal (`Lit::Str` / `Lit::Num` / `Lit::Bool` / `Lit::Null` /
/// `Lit::BigInt`). The non-literal-arg form falls through to
/// `Unknown` because the spec-defined coercion path (`ToString`,
/// `ToNumber`, …) on a non-primitive value can fire user-defined
/// `[Symbol.toPrimitive]` / `valueOf` / `toString` and
/// observably modify state.
///
/// Soundness contract per entry:
/// * `Symbol`: ECMA-262 §20.4.1.1 — `Symbol(description)` does
///   `ToString(description)` (or skips it if description is
///   undefined) and returns a fresh symbol. `ToString` on a
///   primitive literal runs no user code, so the call has no
///   observable side effect beyond the fresh symbol. `Symbol`
///   without `new`; `new Symbol(...)` throws TypeError, but
///   `new`-call form is `Expr::New` not `Expr::Call`, so this
///   rule never fires for it.
const PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS: &[&str] = &["Symbol"];

/// Built-in container constructors whose `new X()` (no args)
/// form is pure. ECMA-262 spec for each construct algorithm:
/// step 1 short-circuits when iterable/length is undefined,
/// returning a fresh empty container without invoking any user
/// code (no iterator protocol, no getters fired). Same admission
/// contract as `PURE_GLOBAL_CALLS`. `Set` / `Map` also accept
/// an Array-literal iterable; see `PURE_BUILTIN_NEW_ARRAY_ITERABLE`.
const PURE_BUILTIN_NEW_NO_ARGS: &[&str] = &["Map", "Set", "WeakMap", "WeakSet", "Array"];

/// Built-in container constructors whose 1-arg form is pure when
/// the argument is an Array literal with all-Pure elements (no
/// spreads, no holes):
///
/// * `Set`: `new Set([elt, ...])` — ECMA-262 §24.2.1.1 iterates
///   the iterable via the built-in Array iterator (no user code
///   on a fresh array literal) and calls `Set.prototype.add` per
///   element. SameValueZero on primitive keys fires no user
///   code; on object keys it's reference equality. Fresh array
///   of Pure elements ⇒ pure.
/// * `Map`: `new Map([[k, v], ...])` — ECMA-262 §24.1.1.1 same
///   iterator path on the outer Array, then `Get(entry, "0")` /
///   `Get(entry, "1")` (own data properties on a fresh entry
///   array, no getter), then `Map.prototype.set`. Pure when
///   every entry is itself a 2-element Array literal with Pure
///   key + value.
/// * `WeakSet` / `WeakMap`: NOT covered — they additionally
///   require object keys; primitives throw. Allowing them would
///   require verifying every element/key has object value class,
///   which the classifier doesn't track.
///
/// Stricter than just "Pure arg" because:
///   - `new Set(somePureFn())` could produce a non-iterable at
///     runtime (TypeError at `[Symbol.iterator]()`), which is
///     observable from the caller's standpoint.
///   - `new Set(spreadable)` invokes the iterable's
///     `[Symbol.iterator]()`, which can fire user code on
///     anything other than a literal array.
const PURE_BUILTIN_NEW_ARRAY_ITERABLE: &[&str] = &["Map", "Set"];

/// Static-property READS on these globals are Pure: the property
/// is an own data property of the receiver per ECMA-262 (no getter
/// fires) and accessing it has no observable side effect.
///
/// **Function-valued.** The resolved value is a callable. CALLING
/// it is NOT pure unless the same `(receiver, name)` pair also
/// appears in `PURE_STATIC_CALLS`. Every entry here MUST have both
/// a positive `static_function_ref_*_alias_is_pure` test AND a
/// negative `static_function_ref_*_call_remains_unknown` test
/// pinning that distinction. See AGENTS.md "Pure-call whitelist
/// soundness".
const PURE_STATIC_FUNCTION_REFS: &[(&str, &str)] = &[
    // All entries below are own data properties of the `Object`
    // built-in per ECMA-262 §20.1.2 — reads fire no getter. The
    // CALL of each is unsafe in distinct ways and intentionally
    // NOT in `PURE_STATIC_CALLS`:
    //   - `Object.defineProperty(t, k, d)` mutates `t`.
    //   - `Object.freeze(o)` mutates `o`'s descriptor table.
    //   - `Object.values(o)` / `Object.keys(o)` invoke
    //     `[[OwnPropertyKeys]]` and (for values) `[[Get]]` per
    //     key — fires user getters and Proxy traps.
    // The bare alias form `const define = Object.defineProperty;`
    // appears in real specs as a renamed shortcut.
    ("Object", "defineProperty"),
    ("Object", "defineProperties"),
    ("Object", "freeze"),
    ("Object", "values"),
    ("Object", "keys"),
    ("Object", "entries"),
    ("Object", "fromEntries"),
    ("Object", "getOwnPropertyDescriptor"),
    ("Object", "getOwnPropertyDescriptors"),
    ("Object", "getOwnPropertyNames"),
    ("Object", "getOwnPropertySymbols"),
    ("Object", "getPrototypeOf"),
    ("Object", "setPrototypeOf"),
    ("Object", "create"),
    ("Object", "assign"),
    ("Object", "is"),
    ("Object", "isFrozen"),
    ("Object", "isSealed"),
    ("Object", "isExtensible"),
    ("Object", "preventExtensions"),
    ("Object", "seal"),
    ("Object", "hasOwn"),
];

/// Receiver / global-callable names whose whitelist firing depends
/// on the chunk not having shadowed them at top level.
/// `analyze_chunk_facts` populates the shadowed-globals set, and
/// the classifier suppresses whitelist hits for any name in it —
/// e.g. `const Math = …` makes `Math.PI` fall back to `Unknown`.
pub(crate) const WHITELIST_RECEIVERS: &[&str] =
    &["Math", "Array", "Symbol", "Number", "Boolean", "Object"];

// TODO: extend the call whitelist with operations that are *Pure
// when their arguments are statically known to be primitives*
// (Number / String / Boolean / null / undefined / BigInt
// literals, or fresh literals built from those). Examples that
// become admissible under that stronger argument analysis:
//
//   - `Math.{abs, floor, ceil, round, trunc, sign, sqrt, cbrt,
//     min, max, pow, exp, log, log2, log10, log1p, sin, cos, tan,
//     asin, acos, atan, atan2, sinh, cosh, tanh, hypot, fround,
//     clz32, imul}` — `ToNumber` on a literal Number does not
//     fire user code.
//   - `JSON.parse(str)` for a `StringLiteral` argument — `ToString`
//     on a string is identity.
//   - `JSON.stringify(prim)` for a primitive literal — no
//     `toJSON` / `Symbol.toPrimitive` / `valueOf` path.
//   - `Number.parseInt(str[, radix])`, `Number.parseFloat(str)`
//     for a `StringLiteral` first arg and (optional) `Number`
//     second.
//   - `String.{fromCharCode, fromCodePoint}(...nums)` for all-
//     `NumberLiteral` args.
//   - `Array.of(...prims)` — `CreateDataPropertyOrThrow` on a
//     fresh array does not fire user code; the open question is
//     just "could a non-primitive arg do anything observable",
//     which a primitive-only gate avoids.
//   - `Object.{keys, values, entries, fromEntries, freeze,
//     getOwnPropertyNames, getOwnPropertyDescriptor, isFrozen,
//     hasOwn, assign}` — these *do* observe user callbacks
//     (getter on `[[Get]]`, ownKeys/getOwnPropertyDescriptor
//     traps on `Proxy`, mutation), so they remain UNSAFE for
//     general args. They become Pure only if the receiver is
//     itself a fresh ordinary-object literal with no accessors —
//     a separate, stricter analysis.
//
// Adding any of these requires (a) a Purity::Primitive variant
// (or a side analysis that classifies an Expr as
// "evaluates-to-primitive"), and (b) an updated admission rule
// here that gates the whitelist on that classification. Soundness
// rule: never relax in a way that admits a path firing user code
// on any argument shape (see AGENTS.md "Pure-call whitelist
// soundness").

pub(crate) fn classify_expr_purity(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    match expr {
        Expr::Lit(_) => Purity::Pure,
        Expr::Ident(_) => Purity::Pure,
        Expr::This(_) | Expr::MetaProp(_) => Purity::Pure,
        Expr::Tpl(tpl) => tpl
            .exprs
            .iter()
            .map(|e| classify_expr_purity(e, shadowed, declared_pure, graph))
            .fold(Purity::Pure, Purity::worst),
        Expr::Fn(_) | Expr::Arrow(_) => Purity::Pure,
        Expr::Class(class_expr) => {
            if class_has_static_observable(&class_expr.class, shadowed, declared_pure, graph) {
                Purity::from_reason(PurityRule::ClassStaticObservable, class_expr.class.span)
            } else {
                Purity::Pure
            }
        }
        Expr::Paren(p) => classify_expr_purity(&p.expr, shadowed, declared_pure, graph),
        Expr::Unary(u) => match u.op {
            UnaryOp::Delete => Purity::from_reason(PurityRule::DeleteOperator, u.span),
            // typeof / void / +/-/!/~ on a pure operand are pure
            // (they may coerce, but coercion of an Ident or Lit
            // doesn't run user code).
            _ => classify_expr_purity(&u.arg, shadowed, declared_pure, graph),
        },
        Expr::Bin(b) => classify_expr_purity(&b.left, shadowed, declared_pure, graph).worst(
            classify_expr_purity(&b.right, shadowed, declared_pure, graph),
        ),
        Expr::Cond(c) => classify_expr_purity(&c.test, shadowed, declared_pure, graph)
            .worst(classify_expr_purity(
                &c.cons,
                shadowed,
                declared_pure,
                graph,
            ))
            .worst(classify_expr_purity(&c.alt, shadowed, declared_pure, graph)),
        Expr::Seq(s) => s
            .exprs
            .iter()
            .map(|e| classify_expr_purity(e, shadowed, declared_pure, graph))
            .fold(Purity::Pure, Purity::worst),
        Expr::Array(arr) => arr
            .elems
            .iter()
            .flatten()
            .map(|elem| {
                let spread = elem
                    .spread
                    .map(|sp| Purity::from_reason(PurityRule::ArraySpread, sp));
                let body = classify_expr_purity(&elem.expr, shadowed, declared_pure, graph);
                spread.unwrap_or(Purity::Pure).worst(body)
            })
            .fold(Purity::Pure, Purity::worst),
        Expr::Object(obj) => obj
            .props
            .iter()
            .map(|prop| classify_prop_purity(prop, shadowed, declared_pure, graph))
            .fold(Purity::Pure, Purity::worst),
        Expr::Member(member) => classify_member_purity(member, shadowed),
        Expr::SuperProp(s) => Purity::from_reason(PurityRule::SuperProp, s.span),
        // Optional chaining (`recv?.prop`, `recv?.()`) only adds a
        // null/undefined short-circuit on top of plain member /
        // call evaluation; it doesn't introduce side effects of
        // its own. Recurse through the OptChainBase so an
        // OptChain that expands to a whitelisted static-property
        // read or a whitelisted call returns the same `Pure` /
        // `Unknown` answer the non-optional shape would. R1 in
        // DESIGN.md "Open design questions / OptChain purity".
        Expr::OptChain(opt) => match &*opt.base {
            OptChainBase::Member(member) => classify_member_purity(member, shadowed),
            OptChainBase::Call(opt_call) => classify_callee_call(
                &opt_call.callee,
                &opt_call.args,
                opt.span,
                shadowed,
                declared_pure,
                graph,
            ),
        },
        Expr::Call(call) => classify_call_purity(call, shadowed, declared_pure, graph),
        Expr::New(new_expr) => classify_new_expr_purity(new_expr, shadowed, declared_pure, graph),
        Expr::TaggedTpl(t) => Purity::from_reason(PurityRule::TaggedTpl, t.span),
        Expr::Assign(a) => Purity::from_reason(PurityRule::AssignOrUpdate, a.span),
        Expr::Update(u) => Purity::from_reason(PurityRule::AssignOrUpdate, u.span),
        Expr::Await(a) => Purity::from_reason(PurityRule::AwaitOrYield, a.span),
        Expr::Yield(y) => Purity::from_reason(PurityRule::AwaitOrYield, y.span),
        // Anything we didn't enumerate falls into the Unknown
        // bucket — soundness-first.
        other => Purity::from_reason(PurityRule::Other, other.span()),
    }
}

/// `(receiver_ident, prop_name)` for `Receiver.prop` where
/// `Receiver` is a plain `Ident` and `prop` is a static name.
/// Returns `None` for computed access (`obj[k]`), private fields,
/// or non-Ident receivers.
fn static_member_pair(member: &MemberExpr) -> Option<(&'static str, &'static str)> {
    let recv_sym = match member.obj.as_ref() {
        Expr::Ident(ident) => ident.sym.as_ref(),
        _ => return None,
    };
    let prop_sym = match &member.prop {
        MemberProp::Ident(ident) => ident.sym.as_ref(),
        _ => return None,
    };
    let recv = WHITELIST_RECEIVERS
        .iter()
        .copied()
        .find(|r| *r == recv_sym)?;
    // `prop_sym` may be borrowed from the AST; intern via the
    // whitelist tables so we return `&'static str` for downstream
    // `contains` checks.
    let prop = PURE_STATIC_PROPS
        .iter()
        .chain(PURE_STATIC_FUNCTION_REFS.iter())
        .chain(PURE_STATIC_CALLS.iter())
        .find_map(|(r, p)| (*r == recv && *p == prop_sym).then_some(*p))?;
    Some((recv, prop))
}

/// Purity of a `new X(args…)` expression. Matches:
///   * No-arg form against `PURE_BUILTIN_NEW_NO_ARGS`.
///   * 1-arg Array-literal-iterable form against
///     `PURE_BUILTIN_NEW_ARRAY_ITERABLE` (`Set` / `Map`).
///
/// Everything else (non-Ident callees, shadowed names, tagged
/// templates, other arg shapes) falls through to `Unknown`.
fn classify_new_expr_purity(
    new_expr: &NewExpr,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Expr::Ident(callee) = new_expr.callee.as_ref() else {
        return Purity::from_reason_with_detail(
            PurityRule::UnknownNew,
            new_expr.span,
            "non-ident callee".to_string(),
        );
    };
    let arg_count = new_expr.args.as_ref().map_or(0, Vec::len);
    if let Some(name) = PURE_BUILTIN_NEW_NO_ARGS
        .iter()
        .copied()
        .find(|n| *n == callee.sym.as_ref())
        && !shadowed.contains(name)
        && arg_count == 0
    {
        return Purity::Pure;
    }
    if let Some(name) = PURE_BUILTIN_NEW_ARRAY_ITERABLE
        .iter()
        .copied()
        .find(|n| *n == callee.sym.as_ref())
        && !shadowed.contains(name)
        && let Some(args) = new_expr.args.as_ref()
        && args.len() == 1
        && args[0].spread.is_none()
    {
        // Recurse through the array literal; if every element
        // classifies as Pure, the `new <Container>([...])` is
        // Pure. If not, return the failing elements' reasons
        // alongside an UnknownNew umbrella reason so the
        // diagnostic reports both the rule that gated the
        // constructor *and* which sub-expression(s) failed.
        let inner = classify_array_literal_for_iterable(
            &args[0].expr,
            name,
            shadowed,
            declared_pure,
            graph,
        );
        if inner.is_pure() {
            return Purity::Pure;
        }
        return Purity::from_reason_with_detail(
            PurityRule::UnknownNew,
            new_expr.span,
            format!("new {name}([...]) iterable arg has impure element(s)"),
        )
        .worst(inner);
    }
    Purity::from_reason_with_detail(
        PurityRule::UnknownNew,
        new_expr.span,
        callee.sym.to_string(),
    )
}

/// Classify the iterable arg of `new Set([...])` / `new Map([[k,v],...])`.
/// Returns `Purity::Pure` only when the arg is an Array literal with
/// every element a Pure expression (no spreads, no holes; for `Map`,
/// every element a 2-element Array literal of Pure entries). Map's
/// iterator path Get's [0]/[1] on each entry — fresh literal entries
/// guarantee those reads are own-data-property hits, not user-getter
/// hits on a 2-tuple-shaped object.
///
/// On failure returns a `NotPure` carrying the offending sub-expression's
/// reason(s) so the caller can attach them to the surrounding
/// `UnknownNew` verdict.
fn classify_array_literal_for_iterable(
    expr: &Expr,
    callee: &str,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Expr::Array(arr) = expr else {
        return Purity::from_reason_with_detail(
            PurityRule::Other,
            expr.span(),
            format!("new {callee} arg is not an Array literal"),
        );
    };
    arr.elems
        .iter()
        .map(|elem| {
            classify_iterable_element(
                elem.as_ref(),
                arr.span,
                callee,
                shadowed,
                declared_pure,
                graph,
            )
        })
        .fold(Purity::Pure, Purity::worst)
}

fn classify_iterable_element(
    elem: Option<&ExprOrSpread>,
    arr_span: Span,
    callee: &str,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Some(elem) = elem else {
        // Hole: `[1, , 3]`. Set treats hole as undefined (still a
        // value); Map's `Get(undefined, "0")` throws. Reject for both.
        return Purity::from_reason_with_detail(
            PurityRule::Other,
            arr_span,
            format!("new {callee} array-literal arg has hole"),
        );
    };
    if let Some(sp) = elem.spread {
        return Purity::from_reason(PurityRule::ArraySpread, sp);
    }
    match callee {
        "Set" => classify_expr_purity(&elem.expr, shadowed, declared_pure, graph),
        "Map" => classify_map_entry(&elem.expr, shadowed, declared_pure, graph),
        _ => Purity::from_reason_with_detail(
            PurityRule::Other,
            elem.expr.span(),
            format!("unsupported callee {callee} for array-iterable rule"),
        ),
    }
}

fn classify_map_entry(
    entry_expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Expr::Array(entry) = entry_expr else {
        return Purity::from_reason_with_detail(
            PurityRule::Other,
            entry_expr.span(),
            "new Map entry is not an Array literal".to_string(),
        );
    };
    if entry.elems.len() != 2 {
        return Purity::from_reason_with_detail(
            PurityRule::Other,
            entry.span,
            "new Map entry is not a 2-element Array".to_string(),
        );
    }
    entry
        .elems
        .iter()
        .map(|e| match e {
            None => Purity::from_reason_with_detail(
                PurityRule::Other,
                entry.span,
                "new Map entry has hole".to_string(),
            ),
            Some(e) if e.spread.is_some() => {
                Purity::from_reason(PurityRule::ArraySpread, e.spread.unwrap())
            }
            Some(e) => classify_expr_purity(&e.expr, shadowed, declared_pure, graph),
        })
        .fold(Purity::Pure, Purity::worst)
}

/// Purity of `member` taken as an r-value member access
/// (`recv.prop` or `recv?.prop`). Pure iff the receiver+property
/// pair is whitelisted and the receiver name isn't shadowed by a
/// chunk-top declaration. Otherwise `Unknown` — `obj.prop` on an
/// arbitrary object can fire a getter, which we can't rule out
/// statically.
fn classify_member_purity(member: &MemberExpr, shadowed: &BTreeSet<&'static str>) -> Purity {
    if let Some((recv, prop)) = static_member_pair(member)
        && !shadowed.contains(recv)
        && (PURE_STATIC_PROPS.contains(&(recv, prop))
            || PURE_STATIC_FUNCTION_REFS.contains(&(recv, prop)))
    {
        return Purity::Pure;
    }
    let detail = match (member.obj.as_ref(), &member.prop) {
        (Expr::Ident(o), MemberProp::Ident(p)) => Some(format!("{}.{}", o.sym, p.sym)),
        _ => None,
    };
    Purity::NotPure {
        reasons: vec![PurityReason {
            rule: PurityRule::UnknownMember,
            span: member.span,
            source_location: None,
            detail,
        }],
    }
}

fn classify_call_purity(
    call: &CallExpr,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Callee::Expr(callee_expr) = &call.callee else {
        return Purity::from_reason_with_detail(
            PurityRule::UnknownCall,
            call.span,
            "non-Expr callee (Super/Import)".to_string(),
        );
    };
    classify_callee_call(
        callee_expr,
        &call.args,
        call.span,
        shadowed,
        declared_pure,
        graph,
    )
}

/// Common backbone for `Expr::Call` and `OptChainBase::Call` —
/// classify a `callee_expr(args…)` invocation. Walks the same
/// whitelists and chunk-local function-body purity cache the
/// regular call classifier consults; the only difference between
/// `Expr::Call` and `OptChainBase::Call` is the null-coalesce
/// short-circuit on the optional form, which is irrelevant for
/// side-effect classification.
fn classify_callee_call(
    callee_expr: &Expr,
    args: &[ExprOrSpread],
    call_span: Span,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    // Author-declared pure binding: a chunk-local function whose
    // spec member carries `purity: "pure"`. The annotation is an
    // explicit override and wins over both the whitelist and the
    // shadowing check (the spec author asserts that THIS bound
    // value is pure regardless of what its body does or whether
    // an import shadows the name). See AGENTS.md "Declared
    // purity".
    if let Expr::Ident(ident) = callee_expr
        && declared_pure.contains(ident.sym.as_ref())
    {
        return all_args_pure(args, shadowed, declared_pure, graph);
    }
    // Chunk-local function declaration: consult the per-chunk
    // function-body purity cache. `Pure` callee + Pure args → Pure;
    // non-Pure callee inherits its reasons (so the chain points
    // back through to the unhandled construct in the function body).
    if let Expr::Ident(ident) = callee_expr
        && let Some(callee_purity) = graph.function_purity(ident.sym.as_ref())
    {
        return callee_purity.worst(all_args_pure(args, shadowed, declared_pure, graph));
    }
    // `Recv.method(args)` against PURE_STATIC_CALLS.
    if let Expr::Member(member) = callee_expr
        && let Some((recv, prop)) = static_member_pair(member)
        && !shadowed.contains(recv)
        && PURE_STATIC_CALLS.contains(&(recv, prop))
    {
        return all_args_pure(args, shadowed, declared_pure, graph);
    }
    // `globalCallable(args)` against PURE_GLOBAL_CALLS.
    if let Expr::Ident(ident) = callee_expr
        && let Some(name) = PURE_GLOBAL_CALLS
            .iter()
            .copied()
            .find(|n| *n == ident.sym.as_ref())
        && !shadowed.contains(name)
    {
        return all_args_pure(args, shadowed, declared_pure, graph);
    }
    // `globalCallable(prim_lit, …)` against
    // PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS. Every argument
    // must be a `Lit::Str/Num/Bool/Null/BigInt` (no spread, no
    // computed sub-expression). On a match the call has no
    // observable side effects (per the per-entry soundness
    // notes on the constant); if any arg is non-literal the
    // rule doesn't fire and we fall through to `Unknown`.
    if let Expr::Ident(ident) = callee_expr
        && let Some(name) = PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS
            .iter()
            .copied()
            .find(|n| *n == ident.sym.as_ref())
        && !shadowed.contains(name)
        && args
            .iter()
            .all(|arg| arg.spread.is_none() && is_primitive_literal(&arg.expr))
    {
        return Purity::Pure;
    }
    let detail = callee_summary(callee_expr);
    Purity::NotPure {
        reasons: vec![PurityReason {
            rule: PurityRule::UnknownCall,
            span: call_span,
            source_location: None,
            detail,
        }],
    }
}

fn callee_summary(callee_expr: &Expr) -> Option<String> {
    match callee_expr {
        Expr::Ident(ident) => Some(ident.sym.to_string()),
        Expr::Member(m) => match (m.obj.as_ref(), &m.prop) {
            (Expr::Ident(o), MemberProp::Ident(p)) => Some(format!("{}.{}", o.sym, p.sym)),
            _ => None,
        },
        _ => None,
    }
}

/// True for AST nodes whose evaluation produces a primitive
/// value with no user-code side effects: string/number/boolean/
/// null/bigint literals only. `Lit::Regex` is excluded because
/// it produces a fresh `RegExp` *object* (not a primitive), and
/// would fail the "no user code on coercion" admission contract
/// of `PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS`. `Expr::Tpl` is
/// excluded even when it has zero interpolations because it's
/// not a `Lit::Str` AST shape — a future refinement could add
/// it.
fn is_primitive_literal(expr: &Expr) -> bool {
    matches!(
        expr,
        Expr::Lit(Lit::Str(_))
            | Expr::Lit(Lit::Bool(_))
            | Expr::Lit(Lit::Null(_))
            | Expr::Lit(Lit::Num(_))
            | Expr::Lit(Lit::BigInt(_)),
    )
}

fn all_args_pure(
    args: &[ExprOrSpread],
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    args.iter()
        .map(|arg| {
            // Spread arg's iterator could fire side effects.
            let spread = arg
                .spread
                .map(|sp| Purity::from_reason(PurityRule::ArraySpread, sp));
            let body = classify_expr_purity(&arg.expr, shadowed, declared_pure, graph);
            spread.unwrap_or(Purity::Pure).worst(body)
        })
        .fold(Purity::Pure, Purity::worst)
}

fn classify_prop_purity(
    prop: &PropOrSpread,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    match prop {
        PropOrSpread::Spread(spread) => {
            // Spreading an arbitrary expression invokes its
            // iterator (array spread) or property iteration
            // (object spread). Either can fire a getter or a
            // user-defined `[Symbol.iterator]`.
            classify_expr_purity(&spread.expr, shadowed, declared_pure, graph).worst(
                Purity::from_reason(PurityRule::ObjectSpread, spread.expr.span()),
            )
        }
        PropOrSpread::Prop(prop) => match prop.as_ref() {
            Prop::Shorthand(_) => Purity::Pure,
            Prop::KeyValue(kv) => {
                classify_propname_purity(&kv.key, shadowed, declared_pure, graph).worst(
                    classify_expr_purity(&kv.value, shadowed, declared_pure, graph),
                )
            }
            Prop::Assign(a) => Purity::from_reason(PurityRule::ObjectAssignProp, a.key.span),
            // `{ get x() {}, set x(v) {}, m() {} }` — defining a
            // method or accessor is pure; invoking it is not, and
            // we don't invoke it during init.
            Prop::Getter(_) | Prop::Setter(_) | Prop::Method(_) => Purity::Pure,
        },
    }
}

fn classify_propname_purity(
    name: &PropName,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    match name {
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_) | PropName::BigInt(_) => {
            Purity::Pure
        }
        PropName::Computed(c) => classify_expr_purity(&c.expr, shadowed, declared_pure, graph),
    }
}

/// Whether a class declaration runs observable code at class-decl
/// time. Static blocks always run; static fields run their
/// initializer. `extends <expr>` is at-init: the expression itself
/// runs, but `extends` references are tracked as `R`-edges
/// elsewhere — here we only report whether the class itself
/// _additionally_ has observable side-effecting init code.
pub(crate) fn class_has_static_observable(
    class: &Class,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    class.body.iter().any(|member| match member {
        ClassMember::StaticBlock(_) => true,
        ClassMember::ClassProp(prop) if prop.is_static => prop
            .value
            .as_deref()
            .map(|v| classify_expr_purity(v, shadowed, declared_pure, graph) != Purity::Pure)
            .unwrap_or(false),
        ClassMember::PrivateProp(prop) if prop.is_static => prop
            .value
            .as_deref()
            .map(|v| classify_expr_purity(v, shadowed, declared_pure, graph) != Purity::Pure)
            .unwrap_or(false),
        _ => false,
    })
}
