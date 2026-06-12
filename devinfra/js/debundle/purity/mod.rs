mod whitelists;

pub(crate) use whitelists::{SHADOW_TRACKED_GLOBALS, WHITELIST_RECEIVERS};

use std::collections::{BTreeMap, BTreeSet};

use binding_targets::strip_parens;
use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use crate::SourceLocation;
use crate::facts::TopLevelItemView;
use whitelists::{
    PLAIN_DATA_HOSTILE_BUILTINS, PURE_BUILTIN_NEW_ARRAY_ITERABLE, PURE_BUILTIN_NEW_NO_ARGS,
    PURE_BUILTIN_NEW_STRING_LITERAL_ARG, PURE_GLOBAL_CALLS, PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS,
    PURE_OBJECT_CALLS_ON_PLAIN_DATA, PURE_STATIC_CALLS, PURE_STATIC_FUNCTION_REFS,
    PURE_STATIC_PROPS,
};

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
    pure_new_constructors: BTreeSet<String>,
    /// Per-binding declared-pure member-call set. Looked up by the
    /// classifier when it sees `<recv>.<prop>(args)` — if `recv` is a
    /// non-shadowed key here and `<prop>` is in the set, the call is
    /// admitted as pure with args evaluated normally. Author-trust
    /// contract; see AGENTS.md "Declared purity".
    declared_pure_members: BTreeMap<String, BTreeSet<String>>,
    /// Chunk-top `const X = <result-primitive init>` binding names.
    /// `const` makes the binding immutable, and a primitive value
    /// carries no user accessors, so a `ToString` / `ToNumber` /
    /// `ToPropertyKey` coercion of a reference to `X` fires no user
    /// code — `X` is admitted as result-primitive wherever it is not
    /// locally shadowed. The init's own evaluation effects are
    /// classified separately at its declaration site; only the
    /// *value* class matters here. See `collect_primitive_const_bindings`.
    primitive_const_bindings: BTreeSet<String>,
    /// Chunk-top `const X = [..]` bindings that remain static ordinary
    /// arrays under the same no-escape scan as `PlainData`, and are
    /// never used as the receiver of unknown/mutating array methods.
    /// At-init fallback uses this to avoid treating `X.map(cb)` /
    /// `X.forEach(cb)` as if those built-ins might invoke functions
    /// held inside `X`; the callback body is still modeled separately.
    plain_array_bindings: BTreeSet<String>,
    /// Purity verdict for chunk-top bindings that are imports of a
    /// function from another module, keyed by local binding name.
    /// Populated by the program-level cross-module purity oracle;
    /// empty in the strictly per-chunk path, so an imported callee with
    /// no entry stays `unknown_call` as before. A body-local binding
    /// that shadows the import is not resolved through this map.
    imported_purities: BTreeMap<String, Purity>,
    /// Bindings the author asserts are *fluent-trusted* roots
    /// (`chunk_export_purity.<chunk>.fluent_exports`, projected onto
    /// this chunk's local import bindings): every value reachable from
    /// the root through static member reads and calls is itself
    /// trusted — member reads on it are pure, calls of it are pure
    /// (arguments still classified normally), and the result carries
    /// the same trust. This is what admits builder/fluent APIs whose
    /// chain receivers are call *results*, not bindings — e.g. zod's
    /// `k.object({...}).optional().describe(...)` — which no
    /// binding-keyed surface (`declared_pure_members`,
    /// `imported_purities`) can ever reach. Seeded from
    /// `AnalysisHints::fluent_bindings` and closed over chunk-top
    /// `const X = <fluent chain>` declarations
    /// (`collect_fluent_const_bindings`). Author-trust contract: see
    /// docs/design.md A9.
    fluent_bindings: BTreeSet<String>,
}

#[derive(Debug, Clone)]
enum ChunkBinding {
    /// Chunk-top function declaration or `const f = function/arrow`.
    /// `purity` is the worst purity reachable from the body, computed
    /// by fixed-point iteration over all chunk-top functions.
    Function { purity: Purity },
    /// Chunk-top binding whose value is provably a plain object/array
    /// — `const X = <plain literal>`, or `let`/`var` whose every
    /// re-bind is also a plain literal, or the TS-enum-IIFE shape
    /// (see `is_ts_enum_iife_init_for_binding`). The chunk-wide write
    /// scan (`PlainDataWriteScanner`) rejects any post-init accessor
    /// installation, so property reads `X.k` / `X[k]` are pure.
    /// Full soundness write-up: <docs/purity_soundness.md> §
    /// "ChunkBinding::PlainData".
    PlainData,
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
        Self::build_with_declared_pure_new(body, shadowed, declared_pure, &BTreeSet::new())
    }

    pub(crate) fn build_with_declared_pure_new(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
        declared_pure_new: &BTreeSet<String>,
    ) -> Self {
        Self::build_full(
            body,
            shadowed,
            declared_pure,
            declared_pure_new,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &BTreeSet::new(),
        )
    }

    pub(crate) fn build_full(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
        declared_pure_new: &BTreeSet<String>,
        declared_pure_members: &BTreeMap<String, BTreeSet<String>>,
        imported_purities: &BTreeMap<String, Purity>,
        fluent_bindings: &BTreeSet<String>,
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
        let mut bindings: BTreeMap<String, ChunkBinding> = functions
            .iter()
            .map(|f| {
                (
                    f.name.clone(),
                    ChunkBinding::Function {
                        purity: Purity::Pure,
                    },
                )
            })
            .collect();
        // Plain-data candidates: chunk-top `const X = <plain object|array
        // literal>` whose binding cell is never written through anywhere
        // in the chunk. Function-bound `const` initializers are already
        // tracked as `Function`; this pass only adds non-function
        // plain-data shapes.
        for name in collect_plain_data_bindings(body, shadowed) {
            // Functions take precedence: a chunk-top `const f = () => ...`
            // is registered as Function and must not be re-registered as
            // PlainData (calling it is the interesting question, not
            // reading its `.length` etc.).
            bindings.entry(name).or_insert(ChunkBinding::PlainData);
        }
        let mut graph = ChunkCodeGraph {
            bindings,
            pure_new_constructors: declared_pure_new.clone(),
            declared_pure_members: declared_pure_members.clone(),
            primitive_const_bindings: collect_primitive_const_bindings(body),
            plain_array_bindings: collect_plain_array_bindings(body, shadowed),
            imported_purities: imported_purities.clone(),
            fluent_bindings: collect_fluent_const_bindings(body, fluent_bindings),
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
            ChunkBinding::PlainData => None,
        }
    }

    /// Whether `name` is bound at chunk top to a confirmed plain-data
    /// shape — a `const`-bound plain object/array literal that no
    /// statement in this chunk writes through. Member reads `name.k`
    /// / `name[pure]` on such a binding are pure (see
    /// `ChunkBinding::PlainData`).
    pub(crate) fn is_plain_data(&self, name: &str) -> bool {
        matches!(self.bindings.get(name), Some(ChunkBinding::PlainData))
    }

    /// Whether `name` is a static ordinary array binding suitable for
    /// array-method at-init fallback refinement.
    pub(crate) fn is_plain_array(&self, name: &str) -> bool {
        self.plain_array_bindings.contains(name)
    }

    pub(crate) fn is_declared_pure_new(&self, name: &str) -> bool {
        self.pure_new_constructors.contains(name)
    }

    /// Cross-module purity verdict for an imported function binding,
    /// when the program-level oracle resolved one.
    pub(crate) fn imported_purity(&self, name: &str) -> Option<Purity> {
        self.imported_purities.get(name).cloned()
    }

    /// Whether `<recv>.<prop>(args)` is admitted as pure by an author
    /// `pure_members: [<prop>, …]` annotation on the binding `recv`.
    /// Args still classified independently — declared purity covers
    /// the function value, not its arguments.
    pub(crate) fn is_declared_pure_member(&self, recv: &str, prop: &str) -> bool {
        self.declared_pure_members
            .get(recv)
            .is_some_and(|props| props.contains(prop))
    }

    /// Whether `name` is a fluent-trusted root (author-asserted via
    /// `fluent_exports`, or a chunk-top `const` derived from one — see
    /// `collect_fluent_const_bindings`).
    pub(crate) fn is_fluent_binding(&self, name: &str) -> bool {
        self.fluent_bindings.contains(name)
    }
}

/// One author-declared `purity: pure` hint the analyzer determines
/// would be inferred automatically — the binding's body classifies
/// `Pure` (or the binding admits as `PlainData`) even with the hint
/// itself removed from `declared_pure`. Surfaced as a chunk-level
/// warning so spec authors can delete the load-free hint and keep
/// only the genuinely-load-bearing ones (vendor-shape impurity
/// overrides, etc.).
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct RedundantPurityHint {
    pub binding_name: String,
    pub reason: RedundantPurityReason,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RedundantPurityReason {
    /// Chunk-local function/arrow whose body classifies `Pure` by
    /// recursive analysis (with the hint on this binding removed
    /// from `declared_pure`; hints on other bindings still apply).
    InferredPureFunction,
    /// Chunk-local binding that admits as `PlainData`. The
    /// `purity: pure` callsite-override is a no-op because the
    /// binding isn't called as `binding(...)` in any pure-relevant
    /// way that the override would gate.
    InferredPlainDataBinding,
}

/// One redundant `pure_members: [<prop>]` entry — the
/// `<binding>.<prop>(args)` call would already classify pure
/// without the spec hint (e.g. the receiver is `Array` and the
/// property is `isArray`, already covered by `PURE_STATIC_CALLS`).
/// Surfaced so spec authors can prune the redundant entry.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct RedundantPureMemberHint {
    pub binding_name: String,
    pub property: String,
    pub reason: RedundantPureMemberReason,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RedundantPureMemberReason {
    /// `(<binding>, <prop>)` is already in `PURE_STATIC_CALLS` AND
    /// `<binding>` is a whitelist receiver name (e.g. `Array`,
    /// `Number`) — the call classifies pure on its own with no
    /// `pure_members` annotation. The hint is a no-op.
    WhitelistedStaticCall,
}

/// For each name in `declared_pure`, ask "would the analyzer infer
/// this binding as Pure without the hint on itself?" by building a
/// fresh `ChunkCodeGraph` with that one name removed from
/// `declared_pure` (hints on other bindings still apply) and checking
/// the binding's classification. Hints whose answer is Yes are
/// reported as redundant.
///
/// **Semantics — per-hint independent removal, hints on other names
/// kept in place.** Removing only the hint under test catches "the
/// analyzer would have figured this out on its own given the rest
/// of the current spec." When a chain `a → b → c → …` carries
/// hints at multiple points, the per-hint check correctly reports
/// the *transitively redundant* members at the head of the chain
/// (their inference relies on hints further down) and keeps the
/// *load-bearing* members (the ones whose own body is what's
/// genuinely impure). Authors removing hints in successive
/// `/followups` rounds will see previously-redundant hints become
/// load-bearing as the supporting hints are pruned, and the loop
/// terminates when only genuinely impure bodies retain hints.
///
/// **Soundness for the surrounding debundle reshuffle:** the check
/// has no effect on classification of statements — it only emits
/// a warning. Dropping a hint based on the warning is the spec
/// author's decision and re-runs the full analysis next build.
/// The per-hint check itself is a read-only side query.
///
/// Cost: O(|declared_pure| × graph_build_cost). For typical spec
/// hint counts (single digits per chunk) this is negligible
/// compared to the per-chunk analysis itself.
pub(crate) fn detect_redundant_purity_hints(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
) -> Vec<RedundantPurityHint> {
    let mut out = Vec::new();
    for name in declared_pure {
        let mut without = declared_pure.clone();
        without.remove(name);
        let probe = ChunkCodeGraph::build(body, shadowed, &without);
        let reason = match probe.bindings.get(name) {
            Some(ChunkBinding::Function { purity }) if purity.is_pure() => {
                Some(RedundantPurityReason::InferredPureFunction)
            }
            Some(ChunkBinding::PlainData) => Some(RedundantPurityReason::InferredPlainDataBinding),
            _ => None,
        };
        if let Some(reason) = reason {
            out.push(RedundantPurityHint {
                binding_name: name.clone(),
                reason,
            });
        }
    }
    out
}

/// Walk `declared_pure_members` and flag entries the analyzer would
/// classify pure without the hint. Currently the only auto-pure path
/// for member calls is the `PURE_STATIC_CALLS` whitelist for
/// `(WHITELIST_RECEIVERS, prop)` pairs — so an entry like
/// `pure_members: [isArray]` on a binding named `Array` is a no-op
/// (`Array.isArray(...)` is already in `PURE_STATIC_CALLS`).
///
/// The `PURE_OBJECT_CALLS_ON_PLAIN_DATA` admission rule has
/// per-callsite argument-shape gates — without inspecting every
/// callsite for `<binding>.<prop>(...)` arg shapes, we can't claim
/// the spec hint is a no-op (the hint covers ALL arg shapes, while
/// the whitelist only covers plain-data args). To stay sound under
/// the "report only confirmed-redundant" contract, we don't flag
/// `pure_members: [entries|keys|values|freeze|fromEntries]` on
/// `Object` here. Spec authors can drop them manually once they've
/// verified every callsite uses a plain-data arg.
pub(crate) fn detect_redundant_pure_member_hints(
    declared_pure_members: &BTreeMap<String, BTreeSet<String>>,
) -> Vec<RedundantPureMemberHint> {
    let mut out = Vec::new();
    for (binding, props) in declared_pure_members {
        // Only whitelist-receiver bindings can ride on
        // `PURE_STATIC_CALLS`. A user-named binding (e.g. a vendor
        // namespace `b`) doesn't reach the whitelist regardless of
        // shadowing — so the hint is load-bearing there.
        let recv = WHITELIST_RECEIVERS
            .iter()
            .copied()
            .find(|r| *r == binding.as_str());
        let Some(recv) = recv else {
            continue;
        };
        for prop in props {
            if PURE_STATIC_CALLS
                .iter()
                .any(|(r, p)| *r == recv && *p == prop.as_str())
            {
                out.push(RedundantPureMemberHint {
                    binding_name: binding.clone(),
                    property: prop.clone(),
                    reason: RedundantPureMemberReason::WhitelistedStaticCall,
                });
            }
        }
    }
    out
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
    /// Parameter patterns. Default-value expressions evaluate at call
    /// time and destructuring patterns fire getters / the iterator
    /// protocol on the argument, so they participate in the body's
    /// purity classification (`classify_param_purity`).
    params: Vec<&'a Pat>,
    /// Block-bodied function/arrow.
    block_body: Option<&'a BlockStmt>,
    /// Concise-arrow expression body (`(x) => expr`).
    expr_body: Option<&'a Expr>,
    /// Every binding-ident name introduced by this function — its
    /// params plus every `BindingIdent` declared anywhere in its body
    /// (vars, nested function params, catch bindings, …). Over-approx
    /// is sound: these names lexically shadow chunk-top bindings,
    /// whitelist receivers, and spec-annotated names, so references
    /// to them inside the body must not resolve through the global
    /// tables / chunk graph.
    param_and_body_bindings: BTreeSet<String>,
}

impl ChunkFunction<'_> {
    /// Drive a `Visit` visitor over this function's parameter
    /// patterns and body. Params are included because default-value
    /// expressions evaluate at call time exactly like body code —
    /// both the purity walk (`BodyPurityCollector`) and the
    /// call-graph walk (`CallCollector`) must see them (a call edge
    /// hidden in a param default would otherwise skip SCC ordering
    /// and freeze the callee at its optimistic `Pure` init). Block
    /// bodies recurse via `visit_with`; concise-arrow expression
    /// bodies fire `visit_expr` directly so the visitor's
    /// `visit_call_expr` / `visit_expr` overrides catch the body.
    fn visit_body_with<V: Visit + ?Sized>(&self, visitor: &mut V) {
        for pat in &self.params {
            pat.visit_with(visitor);
        }
        if let Some(block) = self.block_body {
            block.visit_with(visitor);
        }
        if let Some(expr) = self.expr_body {
            expr.visit_with(visitor);
        }
    }
}

/// Collects every `BindingIdent` name it visits. Used to gather the
/// names a function introduces (params + body declarations) so reads
/// on a like-named chunk-top PlainData candidate aren't mis-classified
/// as pure when the candidate is lexically shadowed.
struct BindingNameCollector {
    names: BTreeSet<String>,
}

impl Visit for BindingNameCollector {
    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.names.insert(node.id.sym.to_string());
        node.visit_children_with(self);
    }
}

/// Names bound by `params` and by `body` (the function being
/// classified). Conservative over-approximation: collects every
/// `BindingIdent` (including those in nested scopes) — sound because
/// extra names only ever cause more reads to be treated as impure.
fn collect_function_bindings<'p>(
    params: impl Iterator<Item = &'p Pat>,
    block_body: Option<&BlockStmt>,
    expr_body: Option<&Expr>,
) -> BTreeSet<String> {
    let mut collector = BindingNameCollector {
        names: BTreeSet::new(),
    };
    for pat in params {
        pat.visit_with(&mut collector);
    }
    if let Some(block) = block_body {
        block.visit_with(&mut collector);
    }
    if let Some(expr) = expr_body {
        expr.visit_with(&mut collector);
    }
    collector.names
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
    let block_body = fn_decl.function.body.as_ref();
    out.push(ChunkFunction {
        name: fn_decl.ident.sym.to_string(),
        params: fn_decl.function.params.iter().map(|p| &p.pat).collect(),
        block_body,
        expr_body: None,
        param_and_body_bindings: collect_function_bindings(
            fn_decl.function.params.iter().map(|p| &p.pat),
            block_body,
            None,
        ),
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
                let block_body = fn_expr.function.body.as_ref();
                out.push(ChunkFunction {
                    name,
                    params: fn_expr.function.params.iter().map(|p| &p.pat).collect(),
                    block_body,
                    expr_body: None,
                    param_and_body_bindings: collect_function_bindings(
                        fn_expr.function.params.iter().map(|p| &p.pat),
                        block_body,
                        None,
                    ),
                });
            }
            Expr::Arrow(arrow) => match arrow.body.as_ref() {
                BlockStmtOrExpr::BlockStmt(block) => {
                    out.push(ChunkFunction {
                        name,
                        params: arrow.params.iter().collect(),
                        block_body: Some(block),
                        expr_body: None,
                        param_and_body_bindings: collect_function_bindings(
                            arrow.params.iter(),
                            Some(block),
                            None,
                        ),
                    });
                }
                BlockStmtOrExpr::Expr(expr) => {
                    out.push(ChunkFunction {
                        name,
                        params: arrow.params.iter().collect(),
                        block_body: None,
                        expr_body: Some(expr.as_ref()),
                        param_and_body_bindings: collect_function_bindings(
                            arrow.params.iter(),
                            None,
                            Some(expr.as_ref()),
                        ),
                    });
                }
            },
            _ => {}
        }
    }
}

/// Collect chunk-top `const` / `let` / `var`-bound bindings whose
/// initializer(s) are plain-literal data shapes (plain object literal
/// with no accessors/methods/spreads/computed keys/`__proto__`, or
/// plain array literal) AND whose binding cell is never written
/// through anywhere in the chunk's AST. See `ChunkBinding::PlainData`
/// for the soundness argument.
///
/// `var` admission notes:
///
/// * `var X = init` at chunk top is hoisted but the initializer
///   evaluates at the source line. Pre-init reads on `X` see
///   `undefined`; member access `undefined.k` throws a TypeError —
///   a spec-mandated throw that fires no user-defined code, so
///   classifying member reads on `X` as pure remains sound (the
///   "pure" claim is "fires no user code", not "always succeeds").
/// * Multiple `var X = init_n` statements at chunk top are legal:
///   each is a no-op redeclaration of the binding cell plus an
///   assignment at the init line. Every init must independently be
///   a plain-literal shape; if any decl's init is non-plain, the
///   name is disqualified. The same name appearing both via a
///   `var X = plain` and later via `X = nonPlain` (ident assign)
///   is caught by the regular `PlainDataWriteScanner` ident-assign
///   check.
/// * `var X;` with no initializer contributes nothing to the
///   candidate; if accompanied by a later `var X = plain` /
///   `X = plain` write the binding still admits (init from the
///   plain decl/assign). If alone, X stays undefined forever and
///   isn't admitted (member-reads on `undefined` throw —
///   technically still pure, but uninteresting).
///
/// Returns the list of qualified names; the caller registers them as
/// `ChunkBinding::PlainData` in the graph.
fn collect_plain_data_bindings(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
) -> Vec<String> {
    // Aggregate every chunk-top init expression per binding name.
    // The same name can have multiple inits for `var`-bound bindings
    // (`var X = a; var X = b;`); every init must independently pass
    // the plain-literal shape check or the name disqualifies.
    let mut inits_by_name: BTreeMap<String, Vec<(&Expr, VarDeclKind)>> = BTreeMap::new();
    for item in body {
        for (name, init, kind) in plain_data_var_candidates(item.as_module_item()) {
            inits_by_name.entry(name).or_default().push((init, kind));
        }
    }
    let candidates: BTreeSet<String> = inits_by_name
        .into_iter()
        .filter(|(name, inits)| {
            inits
                .iter()
                .all(|(init, kind)| is_plain_data_init(init, name, *kind))
        })
        .map(|(name, _)| name)
        .collect();
    if candidates.is_empty() {
        return Vec::new();
    }
    // Scan the entire chunk body (including function/class bodies) for
    // anything that could install an accessor or change the prototype
    // of any candidate, or let the candidate's object reference ESCAPE
    // into code we can't analyze:
    //
    // * member writes / updates / deletes on `X.k` / `X[k]`;
    // * calls to the hostile builtins in `PLAIN_DATA_HOSTILE_BUILTINS`
    //   (`Object.{defineProperty,defineProperties,setPrototypeOf,assign}`,
    //   `Reflect.{defineProperty,set,setPrototypeOf,deleteProperty}`)
    //   with `X` as the first argument;
    // * any *escaping* bare reference to `X` — as a call/new argument,
    //   array element, object property value, alias RHS (`const Y = X`,
    //   `obj.k = X`), or any other position not on the scanner's short
    //   list of provably non-capturing reads (member receiver, spread
    //   source, `typeof`/`!`/`void` operand, return value / concise
    //   arrow body). Once the reference is aliased, a write through the
    //   alias (`Object.defineProperty(Y, …)`) would defeat the
    //   name-based write scan, so escape itself disqualifies.
    //
    // Plain data-property writes (`X.foo = bar`) don't install
    // accessors and don't change the prototype chain, but the
    // conservative check rejects them anyway — the cost is dropping
    // a few PlainData candidates that would still be sound, and the
    // benefit is one short rule that's easy to audit.
    let mut scanner = PlainDataWriteScanner {
        candidates: &candidates,
        shadowed,
        disqualified: BTreeSet::new(),
        shadowing_scopes: Vec::new(),
    };
    for item in body {
        item.as_module_item().visit_with(&mut scanner);
    }
    let disqualified = scanner.disqualified;
    candidates
        .into_iter()
        .filter(|n| !disqualified.contains(n))
        .collect()
}

const SAFE_PLAIN_ARRAY_METHODS: &[&str] = &["filter", "flatMap", "forEach", "map", "slice"];
const ARRAY_PRODUCING_METHODS: &[&str] = &["filter", "flatMap", "map", "slice"];

fn collect_plain_array_bindings(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
) -> BTreeSet<String> {
    let mut const_inits = Vec::new();
    for item in body {
        const_inits.extend(
            plain_data_var_candidates(item.as_module_item())
                .into_iter()
                .filter_map(|(name, init, kind)| {
                    (kind == VarDeclKind::Const).then_some((name, init))
                }),
        );
    }
    let mut candidates = BTreeSet::new();
    loop {
        let before = candidates.len();
        for (name, init) in &const_inits {
            if expr_returns_plain_array(init, &candidates) {
                candidates.insert(name.clone());
            }
        }
        if candidates.len() == before {
            break;
        }
    }
    if candidates.is_empty() {
        return BTreeSet::new();
    }

    let mut plain_data_scanner = PlainDataWriteScanner {
        candidates: &candidates,
        shadowed,
        disqualified: BTreeSet::new(),
        shadowing_scopes: Vec::new(),
    };
    let mut method_scanner = PlainArrayMethodScanner {
        candidates: &candidates,
        disqualified: BTreeSet::new(),
        shadowing_scopes: Vec::new(),
    };
    for item in body {
        let item = item.as_module_item();
        item.visit_with(&mut plain_data_scanner);
        item.visit_with(&mut method_scanner);
    }

    let mut disqualified = plain_data_scanner.disqualified;
    disqualified.extend(method_scanner.disqualified);
    candidates
        .into_iter()
        .filter(|name| !disqualified.contains(name))
        .collect()
}

fn expr_returns_plain_array(expr: &Expr, known_plain_arrays: &BTreeSet<String>) -> bool {
    match strip_parens(expr) {
        Expr::Array(_) => true,
        Expr::Ident(ident) => known_plain_arrays.contains(ident.sym.as_ref()),
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return false;
            };
            let Expr::Member(member) = strip_parens(callee) else {
                return false;
            };
            matches!(
                &member.prop,
                MemberProp::Ident(prop) if ARRAY_PRODUCING_METHODS.contains(&prop.sym.as_ref())
            ) && expr_is_plain_array_receiver(member.obj.as_ref(), known_plain_arrays)
        }
        _ => false,
    }
}

fn expr_is_plain_array_receiver(expr: &Expr, known_plain_arrays: &BTreeSet<String>) -> bool {
    match strip_parens(expr) {
        Expr::Array(_) => true,
        Expr::Ident(ident) => known_plain_arrays.contains(ident.sym.as_ref()),
        Expr::Call(_) => expr_returns_plain_array(expr, known_plain_arrays),
        _ => false,
    }
}

/// Chunk-top `const X = <init>` names whose init evaluates to a
/// **primitive** value, resolving references to earlier such consts.
/// `const` guarantees the binding never rebinds, and a primitive
/// carries no user accessors, so coercing a reference to the name
/// fires no user code regardless of how the init was computed (the
/// init's own evaluation effects are classified at its declaration
/// site — only the resulting value class matters here). `let` / `var`
/// are excluded: a later non-primitive rebind would invalidate the
/// claim.
///
/// Forward pass in source order: a const resolves only references to
/// consts declared before it — exactly the set visible without
/// hitting the temporal dead zone. Function/arrow inits never reach
/// here (`plain_data_var_candidates` filters them) and are not
/// primitive anyway.
fn collect_primitive_const_bindings(body: &[TopLevelItemView<'_>]) -> BTreeSet<String> {
    let mut primitives: BTreeSet<String> = BTreeSet::new();
    let no_local_shadow: BTreeSet<String> = BTreeSet::new();
    for item in body {
        for (name, init, kind) in plain_data_var_candidates(item.as_module_item()) {
            if kind == VarDeclKind::Const
                && !primitives.contains(&name)
                && is_result_primitive(init, &primitives, &no_local_shadow)
            {
                primitives.insert(name);
            }
        }
    }
    primitives
}

/// Close the author-asserted fluent roots (`seed`, the projected
/// `fluent_exports` locals) over chunk-top `const X = <fluent chain>`
/// declarations: `const Base = k.object({...})` makes `Base` a fluent
/// root too, so `Base.extend({...})` downstream is admitted. `const`
/// only — a `let`/`var` cell can be rebound to an untrusted value.
///
/// Only the *value class* is derived here (the value came out of the
/// trusted API, so the author's deep-purity assertion covers it); the
/// init's own evaluation effects — impure arguments anywhere in the
/// chain — are classified separately at the declaration site by
/// `classify_fluent_chain`. This mirrors the
/// `primitive_const_bindings` value-class/evaluation split.
///
/// Fixpoint loop rather than a single forward pass so a chain rooted
/// in a later-declared const (legal for lazily-evaluated references,
/// and cheap to cover) still closes; bounded by one insertion per
/// chunk-top const.
fn collect_fluent_const_bindings(
    body: &[TopLevelItemView<'_>],
    seed: &BTreeSet<String>,
) -> BTreeSet<String> {
    let mut fluent = seed.clone();
    if fluent.is_empty() {
        return fluent;
    }
    loop {
        let mut changed = false;
        for item in body {
            for (name, init, kind) in plain_data_var_candidates(item.as_module_item()) {
                if kind == VarDeclKind::Const
                    && !fluent.contains(&name)
                    && fluent_chain_root(init).is_some_and(|root| fluent.contains(root))
                {
                    fluent.insert(name);
                    changed = true;
                }
            }
        }
        if !changed {
            break;
        }
    }
    fluent
}

/// The root identifier of a fluent chain — a chain of static
/// (non-computed) member reads, calls, and optional-chaining forms of
/// those, hanging off a bare `Ident`. Returns `None` for any other
/// shape: computed members are excluded because admitting the lookup
/// would additionally require the key expression to be
/// ToPropertyKey-safe, and `new` is excluded because construction off
/// a fluent API is not part of the asserted contract (builder APIs
/// chain calls, not constructors).
fn fluent_chain_root(expr: &Expr) -> Option<&str> {
    match strip_parens(expr) {
        Expr::Ident(ident) => Some(ident.sym.as_ref()),
        Expr::Member(member) => match &member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => fluent_chain_root(&member.obj),
            MemberProp::Computed(_) => None,
        },
        Expr::Call(call) => match &call.callee {
            Callee::Expr(callee) => fluent_chain_root(callee),
            _ => None,
        },
        Expr::OptChain(opt) => match &*opt.base {
            OptChainBase::Member(member) => match &member.prop {
                MemberProp::Ident(_) | MemberProp::PrivateName(_) => fluent_chain_root(&member.obj),
                MemberProp::Computed(_) => None,
            },
            OptChainBase::Call(opt_call) => fluent_chain_root(&opt_call.callee),
        },
        _ => None,
    }
}

/// Yield `(name, init)` pairs for every chunk-top single-declarator
/// `const X = <init>` / `let X = <init>` / `var X = <init>` whose
/// initializer is NOT a function/arrow expression (those go through
/// `Function`). Comma-list declarations are pre-split by
/// `top_level_item_views`, so each `VarDecl` we see here has exactly
/// one `decl`.
///
/// All three keyword forms flow through the same scanner; the
/// scanner's check is uniform ("no member writes or hostile builtin
/// calls, all ident writes have plain-literal RHS"), so the
/// distinction is only at the candidate-collection stage:
///
/// * `const` — binding cell is immutable at the language level; only
///   the chunk-wide checks for accessor installation post-init
///   matter.
/// * `let` — every `X = rhs` ident assign anywhere in the chunk must
///   have a plain-literal RHS (enforced by `PlainDataWriteScanner`).
/// * `var` — same as `let`, PLUS multiple chunk-top `var X = init`
///   redeclarations are valid; the caller checks every init for a
///   given name against the plain-literal shape rule. Pre-init reads
///   on a hoisted `var X` see `undefined` and `undefined.k` throws a
///   spec-mandated TypeError — sound for the read-purity claim ("no
///   user code fires") because the throw is engine-emitted, not a
///   user getter.
fn plain_data_var_candidates(item: &ModuleItem) -> Vec<(String, &Expr, VarDeclKind)> {
    let var = match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(var) => var,
            _ => return Vec::new(),
        },
        _ => return Vec::new(),
    };
    let mut out = Vec::new();
    for decl in &var.decls {
        let Pat::Ident(binding) = &decl.name else {
            continue;
        };
        let Some(init) = decl.init.as_deref() else {
            continue;
        };
        if matches!(init, Expr::Fn(_) | Expr::Arrow(_)) {
            continue;
        }
        out.push((binding.id.sym.to_string(), init, var.kind));
    }
    out
}

/// Pure syntactic check for "plain-literal data shape": an object
/// literal whose own properties are exclusively `KeyValue` /
/// `Shorthand` / object spread (`{...src}`) with non-computed,
/// non-`__proto__` keys, or an array literal whose own elements are
/// values or array spreads (holes are fine — they read as
/// `undefined`, no getter).
///
/// **Spreads are permitted.** `{...src}` evaluates `src`'s own
/// enumerable properties via `CopyDataProperties` and writes the
/// resulting values to the target via `CreateDataPropertyOrThrow` —
/// which always produces a data descriptor, regardless of the
/// source's descriptor shape. So even `{...sourceWithGetters}`
/// yields a plain-data target. The spread itself fires source
/// getters AT INIT (impure event, classified independently by the
/// existing `ObjectSpread`/`ArraySpread` rules), but the resulting
/// receiver has no accessor channels, which is what this check
/// gates.
///
/// Value sub-expressions are intentionally NOT validated here: the
/// surrounding statement's at-init purity is the existing
/// classifier's concern, and the safety of post-init member reads
/// depends only on the receiver's shape (no accessors at init + no
/// post-init accessor installation, the latter enforced by
/// `PlainDataWriteScanner`).
fn is_plain_data_init(expr: &Expr, binding: &str, kind: VarDeclKind) -> bool {
    match expr {
        Expr::Paren(p) => is_plain_data_init(&p.expr, binding, kind),
        Expr::Object(obj) => obj.props.iter().all(is_plain_data_prop),
        Expr::Array(_) => true,
        // TS-enum-style IIFE: `((p) => (p.A = "a", p.B = "b", p))(X || {})`
        // produces a plain object at runtime by mutating the parameter
        // through data-property writes only and returning it. See
        // `is_ts_enum_iife_init_for_binding` for the syntactic shape
        // and soundness argument.
        Expr::Call(_) if kind == VarDeclKind::Var => {
            is_ts_enum_iife_init_for_binding(expr, binding)
        }
        _ => false,
    }
}

/// Classify a var-declaration initializer with binding context. Most
/// initializers use ordinary expression purity; the TypeScript enum
/// IIFE exception needs to know which binding is being initialized so
/// it can require the `X || {}` / `X || (X = {})` argument to refer to
/// that same `X`.
pub(crate) fn classify_var_decl_purity(
    var: &VarDecl,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    var.decls
        .iter()
        .filter_map(|decl| {
            let init = decl.init.as_deref()?;
            let Pat::Ident(binding) = &decl.name else {
                // Destructuring declarators (`const {a} = o`,
                // `const [x] = o`) fire getters / the iterator
                // protocol on the initializer value — user code the
                // init expression's own classification doesn't see.
                // Simplest sound rule: any non-Ident pattern makes
                // the statement not pure, regardless of init shape.
                // (A fresh-plain-data-literal RHS would be sound to
                // admit; deliberately not done — the shape is rare
                // at chunk top and the uniform rule is easier to
                // audit.)
                let pattern_purity = param_destructuring_purity(&decl.name)
                    .unwrap_or_else(|| Purity::from_reason(PurityRule::Other, decl.span));
                return Some(pattern_purity.worst(classify_expr_purity(
                    init,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                )));
            };
            let name = binding.id.sym.as_ref();
            if var.kind == VarDeclKind::Var
                && graph.is_plain_data(name)
                && is_ts_enum_iife_init_for_binding(init, name)
            {
                Some(Purity::Pure)
            } else {
                Some(classify_expr_purity(
                    init,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ))
            }
        })
        .fold(Purity::Pure, Purity::worst)
}

/// Recognize the TypeScript-emit "enum" IIFE shape that initializes a
/// `var`-bound binding to a plain object built by parameter-mutation:
///
///     var X = ((p) => (p.A = "a", p.B = "b", p))(X || {});
///     var X = ((p) => (p["A"] = "a", p["B"] = "b", p))(X || (X = {}));
///     var X = ((p) => p)({});
///
/// Returns `true` iff every constraint below holds; the caller treats a
/// `true` result the same as any other `is_plain_data_init` admission
/// (the binding admits as `PlainData` if no chunk-wide member writes /
/// hostile builtin calls etc. follow). The scanner's scope-tracking
/// for function/arrow params handles the inner `p.K = …` writes when
/// `p` shadows the binding name (the canonical TS shape uses the same
/// name for the param).
///
/// **Soundness contract:**
///
/// At runtime, the binding holds the object returned by the IIFE.
/// We require that object to have only data properties:
///
/// 1. **Inline IIFE callable.** The callee is a syntactic `Expr::Arrow`
///    or `Expr::Fn`. Excludes any case where the callable could be
///    replaced at runtime with a different function.
/// 2. **Single Ident parameter.** Pattern parameters (destructuring,
///    rest, default) are out of scope — the parameter is the alias for
///    the mutating writes, and treating non-Ident patterns would
///    require deeper escape analysis.
/// 3. **Single non-spread positional argument.** Either an empty object
///    literal, `X || {}`, or `X || (X = {})` where `X` is the binding
///    being initialized. This excludes passing arbitrary existing
///    objects into the mutating body.
/// 4. **Body mutates only the parameter and returns it.** Body is either:
///    * `Expr::Ident(p)` alone — degenerate "return param unchanged"
///      shape; the IIFE returns the arg as-is, which is plain by (3).
///    * `Expr::Seq` whose last element is `Expr::Ident(p)` and every
///      preceding element is `p.K = primLit` or `p[strLit] = primLit`
///      or `p[numLit] = primLit` — a parameter-mutation followed by
///      return. Function-expression IIFEs may use expression
///      statements for the writes followed by `return p`.
///
/// **Why the result is plain:**
///
/// The argument starts as a plain object (by 3). The body's writes are
/// all `param.K = primitiveLiteral` data-property assignments — these
/// invoke `[[Set]]` on the param, which on a target with no existing
/// accessor for `K` runs `CreateDataProperty` and produces a data
/// descriptor. None of the writes can install an accessor, change the
/// prototype, or call user-defined code. The returned object is the
/// same object passed in, now carrying additional data properties.
/// Reads `X.K` on the binding after the IIFE fires no user code.
///
/// **What is NOT admitted (and why):**
///
/// * **Multi-statement bodies** that do anything other than parameter
///   mutation — `console.log(p)`, `Object.defineProperty(p, …)`,
///   nested IIFEs, etc.
///
/// **Numeric-enum reverse-mapping** `param[(param.X = 0)] = "X"` IS
/// admitted via the nested-assignment-as-computed-key branch in
/// `is_ts_enum_iife_property_write` — the inner assignment is itself
/// a `param.X = primLit` data-property write, the outer is a
/// data-property write keyed by the primitive that inner evaluates
/// to. Both writes are sound.
fn is_ts_enum_iife_init_for_binding(expr: &Expr, binding: &str) -> bool {
    let mut cur = expr;
    while let Expr::Paren(p) = cur {
        cur = &p.expr;
    }
    let Expr::Call(call) = cur else {
        return false;
    };
    is_ts_enum_iife_call_for_binding(call, binding)
}

/// `CallExpr`-level form of `is_ts_enum_iife_init_for_binding`, used
/// both from the var-init admission and by `PlainDataWriteScanner` to
/// exempt the vetted `X || (X = {})` argument occurrence of the enum
/// binding from the escape scan.
fn is_ts_enum_iife_call_for_binding(call: &CallExpr, binding: &str) -> bool {
    let Callee::Expr(callee_expr) = &call.callee else {
        return false;
    };
    let mut callee = callee_expr.as_ref();
    while let Expr::Paren(p) = callee {
        callee = &p.expr;
    }
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return false;
    }
    if !is_ts_enum_iife_arg(&call.args[0].expr, binding) {
        return false;
    }
    match callee {
        Expr::Arrow(arrow) => {
            if arrow.params.len() != 1 {
                return false;
            }
            let Pat::Ident(param_ident) = &arrow.params[0] else {
                return false;
            };
            let param_name = param_ident.id.sym.as_ref();
            match arrow.body.as_ref() {
                BlockStmtOrExpr::Expr(body_expr) => {
                    is_ts_enum_iife_body_expr(strip_parens(body_expr.as_ref()), param_name)
                }
                BlockStmtOrExpr::BlockStmt(block) => is_ts_enum_iife_body_block(block, param_name),
            }
        }
        Expr::Fn(function) => {
            if function.function.params.len() != 1 {
                return false;
            }
            let Pat::Ident(param_ident) = &function.function.params[0].pat else {
                return false;
            };
            let Some(body) = &function.function.body else {
                return false;
            };
            is_ts_enum_iife_body_block(body, param_ident.id.sym.as_ref())
        }
        _ => false,
    }
}

/// IIFE argument must be a plain object literal, a self-assigning
/// short-circuit (`X || (X = {})`), or a plain short-circuit
/// (`X || {}`). Walks through `Paren` wrappers and accepts the
/// short-circuit form recursively on the right side.
fn is_ts_enum_iife_arg(expr: &Expr, binding: &str) -> bool {
    match expr {
        Expr::Paren(p) => is_ts_enum_iife_arg(&p.expr, binding),
        Expr::Object(obj) => obj.props.is_empty(),
        Expr::Bin(b) if b.op == BinaryOp::LogicalOr => {
            matches!(strip_parens(b.left.as_ref()), Expr::Ident(id) if id.sym.as_ref() == binding)
                && is_ts_enum_iife_arg(&b.right, binding)
        }
        Expr::Assign(a) if a.op == AssignOp::Assign => {
            matches!(
                &a.left,
                AssignTarget::Simple(SimpleAssignTarget::Ident(id)) if id.id.sym.as_ref() == binding
            ) && matches!(strip_parens(a.right.as_ref()), Expr::Object(obj) if obj.props.is_empty())
        }
        _ => false,
    }
}

/// Arrow body must be either `p` (return param unchanged) or a
/// sequence expression `(p.K1 = lit, p.K2 = lit, …, p)` with the
/// trailing `p` as the return value.
fn is_ts_enum_iife_body_expr(expr: &Expr, param: &str) -> bool {
    if matches!(expr, Expr::Ident(id) if id.sym.as_ref() == param) {
        return true;
    }
    let Expr::Seq(seq) = expr else {
        return false;
    };
    if seq.exprs.is_empty() {
        return false;
    }
    let (last, rest) = seq.exprs.split_last().expect("non-empty checked above");
    let last_inner = strip_parens(last.as_ref());
    if !matches!(last_inner, Expr::Ident(id) if id.sym.as_ref() == param) {
        return false;
    }
    rest.iter()
        .all(|e| is_ts_enum_iife_property_write(strip_parens(e.as_ref()), param))
}

fn is_ts_enum_iife_body_block(block: &BlockStmt, param: &str) -> bool {
    let Some((last, rest)) = block.stmts.split_last() else {
        return false;
    };
    if !rest.iter().all(|stmt| match stmt {
        Stmt::Expr(expr) => is_ts_enum_iife_property_write(strip_parens(expr.expr.as_ref()), param),
        _ => false,
    }) {
        return false;
    }
    let Stmt::Return(ret) = last else {
        return false;
    };
    let Some(arg) = ret.arg.as_deref() else {
        return false;
    };
    is_ts_enum_iife_body_expr(strip_parens(arg), param)
}

/// One step of the IIFE body:
///
/// * `param.K = primLit` — forward map (static key).
/// * `param[strLit] = primLit` / `param[numLit] = primLit` —
///   forward map (literal computed key).
/// * `param[(param.K = primLit)] = primLit` /
///   `param[(param["K"] = primLit)] = primLit` — TypeScript's
///   numeric-enum reverse-mapping shape. The inner assignment is
///   itself a `param.X = primLit` data-property write that
///   evaluates to the assigned primitive, which then becomes the
///   outer Member's computed key. Both writes are data-property
///   writes on the param; reads on the post-IIFE binding see only
///   data properties.
///
/// Only primitive-literal RHS is accepted at both nesting levels;
/// non-literal RHS could theoretically return an accessor-bearing
/// object — the property write would still install it as a data
/// descriptor on the param, but the stricter primitive-literal RHS
/// keeps the soundness story uniform with `is_plain_data_prop`.
fn is_ts_enum_iife_property_write(expr: &Expr, param: &str) -> bool {
    let Expr::Assign(assign) = expr else {
        return false;
    };
    if assign.op != AssignOp::Assign {
        return false;
    }
    let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assign.left else {
        return false;
    };
    if !matches!(member.obj.as_ref(), Expr::Ident(id) if id.sym.as_ref() == param) {
        return false;
    }
    let key_ok = match &member.prop {
        MemberProp::Ident(ident) => ident.sym.as_ref() != "__proto__",
        MemberProp::Computed(c) => {
            let key = strip_parens(c.expr.as_ref());
            matches!(key, Expr::Lit(Lit::Str(s)) if s.value.to_string_lossy() != "__proto__")
                || matches!(key, Expr::Lit(Lit::Num(_)))
                // TS numeric-enum reverse-mapping: the computed key
                // is itself a `param.X = primLit` data-property write
                // that evaluates to a primitive used as the outer
                // key. Both the inner and outer writes are sound
                // data-property writes; recurse to verify the inner
                // matches the same shape this function admits.
                || is_ts_enum_iife_property_write(key, param)
        }
        MemberProp::PrivateName(_) => false,
    };
    if !key_ok {
        return false;
    }
    matches!(
        assign.right.as_ref(),
        Expr::Lit(Lit::Str(_))
            | Expr::Lit(Lit::Num(_))
            | Expr::Lit(Lit::Bool(_))
            | Expr::Lit(Lit::Null(_))
            | Expr::Lit(Lit::BigInt(_))
    )
}

fn is_plain_data_prop(prop: &PropOrSpread) -> bool {
    let PropOrSpread::Prop(prop) = prop else {
        // `PropOrSpread::Spread` is the `{...src}` form — permitted
        // (see `is_plain_data_init` doc-comment for the
        // CopyDataProperties soundness argument).
        return true;
    };
    match prop.as_ref() {
        Prop::Shorthand(_) => true,
        Prop::KeyValue(kv) => match &kv.key {
            // `{__proto__: X}` in an object literal SETS the prototype
            // (ES262 §13.2.5.5); if X carries accessor properties, reads
            // on the parent literal fire them. Reject explicitly.
            // The computed form `{["__proto__"]: X}` does NOT set the
            // prototype (per spec), but it's a computed key — rejected
            // below.
            PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
            PropName::Str(s) => s.value.to_string_lossy() != "__proto__",
            PropName::Num(_) | PropName::BigInt(_) => true,
            PropName::Computed(_) => false,
        },
        // Methods, getters, setters, and `{key=value}` shorthand assign
        // are all rejected — only data properties qualify.
        Prop::Getter(_) | Prop::Setter(_) | Prop::Method(_) | Prop::Assign(_) => false,
    }
}

/// Visitor that disqualifies plain-data candidates seen as the target
/// of a member write/update/delete, as the first argument to one of a
/// small set of accessor- / prototype-installing built-ins, or in any
/// ESCAPING position.
///
/// **Escape analysis.** The write scan above is name-based: it only
/// catches mutations spelled through the candidate's own identifier.
/// An alias (`const Y = X; Object.defineProperty(Y, …)`) or any other
/// captured reference (call argument, array element, object property
/// value) defeats it. The scanner therefore treats every bare
/// candidate `Ident` as an escape — and disqualifies — unless the
/// occurrence is in one of a short list of provably non-capturing
/// read positions:
///
/// * member-access receiver (`X.k`, `X[k]`, `X?.k`) — a read;
/// * spread source (`{...X}`, `[...X]`, `f(...X)`) — copies values /
///   iterates; the receiver object's identity is not captured;
/// * `typeof X` / `!X` / `void X` operands — transient inspection;
/// * single argument of `Object.{keys,values,entries,freeze,
///   fromEntries}` with the global `Object` unshadowed — read-only /
///   descriptor-tightening builtins that install no accessors (the
///   same set `PURE_OBJECT_CALLS_ON_PLAIN_DATA` admits);
/// * the vetted `X || (X = {})` argument of a recognized TS-enum
///   IIFE init (`is_ts_enum_iife_call_for_binding`);
/// * `return X` / concise arrow body `() => X` — accepted by scope
///   decision: consumers of chunk-internal return values (like
///   importers of an exported PlainData binding) are assumed not to
///   install accessors on it; see docs/purity_soundness.md §
///   "ChunkBinding::PlainData" for the residual-assumption note.
///   Export specifiers (`export { X }`) are non-escaping under the
///   same assumption.
///
/// **Function/arrow parameter scope tracking.** When entering a
/// function or arrow body whose parameters include names that
/// collide with candidates, those names refer to the inner parameter
/// (NOT the outer chunk-top binding) for the duration of the body.
/// Writes on the inner alias would otherwise spuriously disqualify
/// the outer candidate — the canonical TS-enum IIFE shape uses
/// `(function (X) { X["A"] = "a"; })(X || (X = {}))` with the param
/// named the same as the binding. The scanner pushes a "shadowing"
/// set on each function/arrow scope and checks it before
/// disqualifying.
///
/// Block-scoped `let X` / `const X` / inner `var X` declarations also
/// shadow the outer X within their scope, but tracking those
/// requires walking block scopes (a larger change). For now only
/// function/arrow parameters are tracked — the cases this misses
/// are conservative false negatives (we reject an outer X that
/// would have been admissible), never false positives.
struct PlainDataWriteScanner<'a> {
    candidates: &'a BTreeSet<String>,
    /// Chunk-top shadowed-globals set (A8): consulted by the
    /// `Object.{keys,…}(X)` non-escaping-position exemption, which
    /// must not fire when the chunk rebinds `Object`.
    shadowed: &'a BTreeSet<&'static str>,
    disqualified: BTreeSet<String>,
    /// Stack of per-scope candidate-shadowing sets. Each scope entry
    /// is the subset of `candidates` shadowed by the function/arrow
    /// parameters at that level. A name is "shadowed at this point
    /// in the traversal" iff it appears in any active stack entry.
    shadowing_scopes: Vec<BTreeSet<String>>,
}

impl PlainDataWriteScanner<'_> {
    fn is_shadowed(&self, name: &str) -> bool {
        self.shadowing_scopes
            .iter()
            .any(|scope| scope.contains(name))
    }

    fn with_scope<F: FnOnce(&mut Self)>(&mut self, scope: BTreeSet<String>, f: F) {
        self.shadowing_scopes.push(scope);
        f(self);
        self.shadowing_scopes.pop();
    }

    /// Collect the subset of `candidates` shadowed by `params`.
    /// Parameter defaults/rest/destructuring all introduce bindings
    /// for the whole parameter scope. That matters for emitted helper
    /// shapes like `(i, m = helper, d = m.f || (m.f = [])) => ...`:
    /// the `m.f = []` write targets the parameter `m`, not a
    /// chunk-top plain-data object also named `m`.
    fn shadowed_by_params<'a, I>(&self, params: I) -> BTreeSet<String>
    where
        I: IntoIterator<Item = &'a Pat>,
    {
        let mut out = BTreeSet::new();
        for param in params {
            self.collect_shadowed_by_pat(param, &mut out);
        }
        out
    }

    fn collect_shadowed_by_pat(&self, pat: &Pat, out: &mut BTreeSet<String>) {
        match pat {
            Pat::Ident(ident) => {
                let name = ident.id.sym.as_ref();
                if self.candidates.contains(name) {
                    out.insert(name.to_string());
                }
            }
            Pat::Rest(rest) => self.collect_shadowed_by_pat(&rest.arg, out),
            Pat::Assign(assign) => self.collect_shadowed_by_pat(&assign.left, out),
            Pat::Array(array) => {
                for elem in array.elems.iter().flatten() {
                    self.collect_shadowed_by_pat(elem, out);
                }
            }
            Pat::Object(object) => {
                for prop in &object.props {
                    match prop {
                        ObjectPatProp::KeyValue(kv) => self.collect_shadowed_by_pat(&kv.value, out),
                        ObjectPatProp::Assign(assign) => {
                            let name = assign.key.id.sym.as_ref();
                            if self.candidates.contains(name) {
                                out.insert(name.to_string());
                            }
                        }
                        ObjectPatProp::Rest(rest) => self.collect_shadowed_by_pat(&rest.arg, out),
                    }
                }
            }
            Pat::Invalid(_) | Pat::Expr(_) => {}
        }
    }
}

/// Defensive visitor that collects candidate-named idents in a
/// compound assignment target (e.g. inside `[a, ...rest] = …` or
/// `({x} = …)`) so the scanner can disqualify them. The scanner
/// uses this only on shapes outside the explicit Member / Simple-Ident
/// arms — destructuring etc.
struct IdentVisitor<'a> {
    candidates: &'a BTreeSet<String>,
    hit: BTreeSet<String>,
}

impl Visit for IdentVisitor<'_> {
    fn visit_ident(&mut self, node: &Ident) {
        if self.candidates.contains(node.sym.as_ref()) {
            self.hit.insert(node.sym.to_string());
        }
    }
}

impl PlainDataWriteScanner<'_> {
    fn disqualify_if_candidate(&mut self, name: &str) {
        if self.is_shadowed(name) {
            // The reference targets a shadowing inner binding (e.g. a
            // function parameter), not the outer chunk-top candidate.
            // Writes on the inner binding don't affect the outer
            // object so they don't disqualify it.
            return;
        }
        if self.candidates.contains(name) {
            self.disqualified.insert(name.to_string());
        }
    }

    /// If `expr` is a member access whose receiver is a candidate Ident,
    /// disqualify that candidate. Handles plain Member and OptChain Member
    /// receivers, walking through `Paren` wrappers.
    fn disqualify_member_receiver(&mut self, expr: &Expr) {
        let mut cur = expr;
        loop {
            match cur {
                Expr::Paren(p) => cur = &p.expr,
                Expr::Member(member) => {
                    if let Expr::Ident(recv) = member.obj.as_ref() {
                        self.disqualify_if_candidate(recv.sym.as_ref());
                    }
                    return;
                }
                Expr::OptChain(opt) => match &*opt.base {
                    OptChainBase::Member(member) => {
                        if let Expr::Ident(recv) = member.obj.as_ref() {
                            self.disqualify_if_candidate(recv.sym.as_ref());
                        }
                        return;
                    }
                    OptChainBase::Call(_) => return,
                },
                _ => return,
            }
        }
    }
}

impl Visit for PlainDataWriteScanner<'_> {
    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        match &node.left {
            // `X.k = …` / `X[k] = …` — member write. Disqualify
            // regardless of RHS shape: even when the RHS is a
            // plain literal, the write installs a property on the
            // existing `X` object (whose other reads we were
            // promising to keep pure post-init). Plain data writes
            // are sound for read-purity but the conservative rule
            // is "X must never be written through" so the chunk-
            // wide invariant reads simply.
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                if let Expr::Ident(recv) = member.obj.as_ref() {
                    self.disqualify_if_candidate(recv.sym.as_ref());
                }
            }
            // `X = rhs` — plain identifier reassignment. Only
            // candidates registered as `let` can encounter this
            // legitimately (`const` re-assign is a syntax error).
            // Admit only when `rhs` is itself a plain-literal data
            // shape — anything else (a call, an Ident referencing
            // an unknown source, a binary op, etc.) could produce
            // an accessor-bearing value at runtime, which would
            // break the read-purity invariant for subsequent
            // member reads on `X`.
            //
            // For non-`let` candidates this branch can still fire
            // (e.g. an inner-scope shadow assignment in a function
            // body) but disqualifying conservatively is safe —
            // false negatives never violate soundness, and tracking
            // scopes here would be a larger lift than the case
            // requires.
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                if self.candidates.contains(binding.sym.as_ref())
                    && !is_plain_data_init(&node.right, binding.sym.as_ref(), VarDeclKind::Let)
                {
                    self.disqualify_if_candidate(binding.sym.as_ref());
                }
            }
            // Compound assignment targets (paren wrappers, opt-chains)
            // and destructuring targets (`AssignTarget::Pat`) don't
            // come up in chunk-top binding mutator patterns we
            // intend to admit; disqualify defensively if they
            // mention a candidate name.
            _ => {
                let mut idents = IdentVisitor {
                    candidates: self.candidates,
                    hit: BTreeSet::new(),
                };
                node.left.visit_with(&mut idents);
                for name in idents.hit {
                    self.disqualify_if_candidate(&name);
                }
            }
        }
        // For `=` the RHS still needs to descend (e.g. an inner
        // nested write to a candidate inside the RHS expression).
        // For compound `+=` etc., even on an admitted plain-RHS
        // shape, the read-modify-write semantics make it unsafe;
        // SWC's `AssignExpr` carries `op`, but we already
        // disqualified all simple-Ident assigns whose RHS isn't a
        // plain literal — and any compound op produces a non-literal
        // value at runtime (`X += {a:1}` is `X = X + {a:1}` which
        // is a string/number). So no further check needed.
        node.right.visit_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        // `X++` / `--X` on the binding cell itself produces a
        // number; not a plain-literal shape. Disqualify Ident
        // targets in addition to the existing Member-target rule.
        if let Expr::Ident(ident) = node.arg.as_ref() {
            self.disqualify_if_candidate(ident.sym.as_ref());
        }
        self.disqualify_member_receiver(&node.arg);
        node.visit_children_with(self);
    }

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete {
            self.disqualify_member_receiver(&node.arg);
        }
        // `typeof X` / `!X` / `void X` are transient inspections of
        // the value — the reference isn't captured. Skip the bare
        // candidate Ident so the escape default doesn't fire.
        if matches!(node.op, UnaryOp::TypeOf | UnaryOp::Bang | UnaryOp::Void)
            && matches!(strip_parens(&node.arg), Expr::Ident(_))
        {
            return;
        }
        node.visit_children_with(self);
    }

    // ESCAPE DEFAULT: any bare candidate Ident reached by the
    // traversal is a captured reference (alias RHS, call/new arg,
    // array element, object property value, shorthand prop, …) and
    // disqualifies. The non-capturing read positions on the short
    // list in the struct doc-comment are skipped by the overrides
    // below before traversal reaches the Ident.
    fn visit_ident(&mut self, node: &Ident) {
        self.disqualify_if_candidate(node.sym.as_ref());
    }

    // Binding positions (declarator names, params, catch bindings)
    // introduce a binding rather than reading the candidate's value
    // — not an escape.
    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    // `export { X }` is non-escaping by scope decision (see the
    // struct doc-comment): importers of an exported PlainData
    // binding are assumed not to install accessors on it.
    fn visit_export_named_specifier(&mut self, _node: &ExportNamedSpecifier) {}

    // `import { <exported> as <local> }` — the local side is a
    // BindingIdent (already skipped); the imported side is the SOURCE
    // module's export name, pure module metadata that never reads any
    // local binding's value. Without this override the escape default
    // fired on the imported-name `Ident`, disqualifying an unrelated
    // local candidate that happens to share its spelling — real
    // bundles import hundreds of single-letter vendor export names,
    // so candidate enums named `j`/`a`/… were lost to coincidence.
    fn visit_import_named_specifier(&mut self, _node: &ImportNamedSpecifier) {}

    // Member-access receivers are reads, not captures. Skip an
    // Ident receiver (candidate or not); everything else (nested
    // receivers, computed keys) is traversed normally.
    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if !matches!(strip_parens(&node.obj), Expr::Ident(_)) {
            node.obj.visit_with(self);
        }
        node.prop.visit_with(self);
    }

    // Spread sources (`{...X}`, `[...X]`, `f(...X)`) copy values /
    // iterate; the receiver object's identity is not captured.
    fn visit_spread_element(&mut self, node: &SpreadElement) {
        if matches!(strip_parens(&node.expr), Expr::Ident(_)) {
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_expr_or_spread(&mut self, node: &ExprOrSpread) {
        if node.spread.is_some() && matches!(strip_parens(&node.expr), Expr::Ident(_)) {
            return;
        }
        node.visit_children_with(self);
    }

    // `return X` is non-escaping by scope decision (see the struct
    // doc-comment's residual-assumption note).
    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let Some(arg) = node.arg.as_deref()
            && matches!(strip_parens(arg), Expr::Ident(_))
        {
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        // Hostile accessor-/prototype-installing builtins with the
        // candidate as first arg. Redundant with the escape default
        // (a call arg is an escape) but kept as an explicit,
        // self-documenting check.
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.as_ref()
            && let (Expr::Ident(obj), MemberProp::Ident(prop)) = (member.obj.as_ref(), &member.prop)
            && PLAIN_DATA_HOSTILE_BUILTINS
                .iter()
                .any(|(r, p)| *r == obj.sym.as_ref() && *p == prop.sym.as_ref())
            && let Some(arg) = node.args.first()
            && arg.spread.is_none()
            && let Expr::Ident(target) = arg.expr.as_ref()
        {
            self.disqualify_if_candidate(target.sym.as_ref());
        }
        // `Object.{keys,values,entries,freeze,fromEntries}(X)` with
        // the global `Object` unshadowed — read-only /
        // descriptor-tightening builtins that install no accessors
        // (the same set `PURE_OBJECT_CALLS_ON_PLAIN_DATA` admits).
        // Skip the argument so the escape default doesn't fire.
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.as_ref()
            && let (Expr::Ident(obj), MemberProp::Ident(prop)) = (member.obj.as_ref(), &member.prop)
            && obj.sym.as_ref() == "Object"
            && !self.shadowed.contains("Object")
            && PURE_OBJECT_CALLS_ON_PLAIN_DATA
                .iter()
                .any(|(_, p)| *p == prop.sym.as_ref())
            && node.args.len() == 1
            && node.args[0].spread.is_none()
            && matches!(strip_parens(&node.args[0].expr), Expr::Ident(_))
        {
            // Receiver `Object` and the static prop carry no
            // candidate refs; nothing else to visit.
            return;
        }
        // The vetted `X || (X = {})` argument of a recognized
        // TS-enum IIFE init: the only callee shapes the recognizer
        // admits are inline arrow/function expressions whose bodies
        // mutate their own parameter — visit the callee (the
        // param-scope tracking exempts same-named param writes) and
        // skip the vetted argument.
        if self
            .candidates
            .iter()
            .any(|c| !self.is_shadowed(c) && is_ts_enum_iife_call_for_binding(node, c))
        {
            node.callee.visit_with(self);
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let scope = self.shadowed_by_params(node.params.iter());
        self.with_scope(scope, |s| {
            for param in &node.params {
                param.visit_with(s);
            }
            // A concise body that is a bare candidate Ident is the
            // `() => X` return position — non-escaping by the same
            // scope decision as `return X`.
            match node.body.as_ref() {
                BlockStmtOrExpr::Expr(expr) if matches!(strip_parens(expr), Expr::Ident(_)) => {}
                body => body.visit_with(s),
            }
        });
    }

    fn visit_function(&mut self, node: &Function) {
        let scope = self.shadowed_by_params(node.params.iter().map(|p| &p.pat));
        self.with_scope(scope, |s| node.visit_children_with(s));
    }
}

struct PlainArrayMethodScanner<'a> {
    candidates: &'a BTreeSet<String>,
    disqualified: BTreeSet<String>,
    shadowing_scopes: Vec<BTreeSet<String>>,
}

impl PlainArrayMethodScanner<'_> {
    fn is_shadowed(&self, name: &str) -> bool {
        self.shadowing_scopes
            .iter()
            .any(|scope| scope.contains(name))
    }

    fn with_scope<F: FnOnce(&mut Self)>(&mut self, scope: BTreeSet<String>, f: F) {
        self.shadowing_scopes.push(scope);
        f(self);
        self.shadowing_scopes.pop();
    }

    fn disqualify_if_candidate(&mut self, name: &str) {
        if !self.is_shadowed(name) && self.candidates.contains(name) {
            self.disqualified.insert(name.to_string());
        }
    }

    fn shadowed_by_params<'a, I>(&self, params: I) -> BTreeSet<String>
    where
        I: IntoIterator<Item = &'a Pat>,
    {
        let mut out = BTreeSet::new();
        for param in params {
            self.collect_shadowed_by_pat(param, &mut out);
        }
        out
    }

    fn collect_shadowed_by_pat(&self, pat: &Pat, out: &mut BTreeSet<String>) {
        match pat {
            Pat::Ident(ident) => {
                let name = ident.id.sym.as_ref();
                if self.candidates.contains(name) {
                    out.insert(name.to_string());
                }
            }
            Pat::Rest(rest) => self.collect_shadowed_by_pat(&rest.arg, out),
            Pat::Assign(assign) => self.collect_shadowed_by_pat(&assign.left, out),
            Pat::Array(array) => {
                for elem in array.elems.iter().flatten() {
                    self.collect_shadowed_by_pat(elem, out);
                }
            }
            Pat::Object(object) => {
                for prop in &object.props {
                    match prop {
                        ObjectPatProp::KeyValue(kv) => self.collect_shadowed_by_pat(&kv.value, out),
                        ObjectPatProp::Assign(assign) => {
                            let name = assign.key.sym.as_ref();
                            if self.candidates.contains(name) {
                                out.insert(name.to_string());
                            }
                        }
                        ObjectPatProp::Rest(rest) => self.collect_shadowed_by_pat(&rest.arg, out),
                    }
                }
            }
            Pat::Expr(_) | Pat::Invalid(_) => {}
        }
    }
}

impl Visit for PlainArrayMethodScanner<'_> {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = strip_parens(callee)
            && let Expr::Ident(recv) = strip_parens(member.obj.as_ref())
        {
            let allowed = match &member.prop {
                MemberProp::Ident(prop) => SAFE_PLAIN_ARRAY_METHODS.contains(&prop.sym.as_ref()),
                MemberProp::Computed(_) | MemberProp::PrivateName(_) => false,
            };
            if !allowed {
                self.disqualify_if_candidate(recv.sym.as_ref());
            }
        }
        node.visit_children_with(self);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let scope = self.shadowed_by_params(node.params.iter());
        self.with_scope(scope, |s| {
            for param in &node.params {
                param.visit_with(s);
            }
            node.body.visit_with(s);
        });
    }

    fn visit_function(&mut self, node: &Function) {
        let scope = self.shadowed_by_params(node.params.iter().map(|p| &p.pat));
        self.with_scope(scope, |s| node.visit_children_with(s));
    }
}

fn classify_function_body(
    function: &ChunkFunction<'_>,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    // Names bound by this function (its params and every binding
    // declared anywhere in its body) lexically shadow EVERY outer
    // resolution path of the same name: chunk-top PlainData
    // candidates, chunk-top function bindings, whitelist receivers
    // (`Math`, `Object`, …), pure-new builtins (`Map`, `Set`, …),
    // and spec-annotated `declared_pure` / `pure_members` /
    // `pure_new` bindings. A reference to a shadowed name inside
    // the body is a *different value* than the one the global
    // tables / chunk graph / spec author describe, so none of those
    // shortcuts may fire for it. Over-approximation (collecting
    // bindings from nested scopes too) is sound: extra names only
    // make more reads conservative. Lexical scope is constant
    // across one classification tree (nested function/arrow bodies
    // are not walked), so this single set suffices for the whole
    // body.
    let local_shadowed = &function.param_and_body_bindings;
    // Destructuring parameters fire getters (object patterns) or
    // the iterator protocol (array patterns) on the caller's
    // argument at call time — user code the body's expression walk
    // never sees. A function with any destructuring param is not
    // pure-callable. Default-value expressions are classified by
    // the body walk below (`visit_body_with` drives the visitor
    // over the param patterns too).
    let param_purity = function
        .params
        .iter()
        .filter_map(|pat| param_destructuring_purity(pat))
        .fold(Purity::Pure, Purity::worst);
    let mut collector = BodyPurityCollector {
        purity: Purity::Pure,
        shadowed,
        local_shadowed,
        declared_pure,
        graph,
    };
    function.visit_body_with(&mut collector);
    param_purity.worst(collector.purity)
}

/// `Some(NotPure)` when `pat` contains a destructuring pattern that
/// fires user code on the bound value at evaluation time: object
/// patterns run `[[Get]]` per key (user getters / proxy traps),
/// array patterns run the iterator protocol. Plain idents and rest
/// params (fresh array from the arguments list) contribute nothing;
/// default-value expressions are classified separately as
/// expressions by the body walk.
fn param_destructuring_purity(pat: &Pat) -> Option<Purity> {
    match pat {
        Pat::Ident(_) => None,
        Pat::Rest(rest) => param_destructuring_purity(&rest.arg),
        Pat::Assign(assign) => param_destructuring_purity(&assign.left),
        Pat::Array(arr) => Some(Purity::from_reason_with_detail(
            PurityRule::DestructuringPattern,
            arr.span,
            "array destructuring fires the iterator protocol on the bound value".to_string(),
        )),
        Pat::Object(obj) => Some(Purity::from_reason_with_detail(
            PurityRule::DestructuringPattern,
            obj.span,
            "object destructuring fires [[Get]] (user getters / proxy traps) on the bound value"
                .to_string(),
        )),
        Pat::Invalid(_) | Pat::Expr(_) => Some(Purity::from_reason(PurityRule::Other, pat.span())),
    }
}

/// Visitor that walks a function body and accumulates the worst
/// purity of every top-level expression encountered. Skips nested
/// function/arrow/method/getter/setter bodies (those are separate
/// lazy scopes — their purity, if needed, comes from their own
/// graph entry or from the caller's `Unknown` fallback).
struct BodyPurityCollector<'a> {
    purity: Purity,
    shadowed: &'a BTreeSet<&'static str>,
    /// Every name bound by the function whose body is being walked
    /// (its params / body declarations). References to such names
    /// must not resolve through any global table, chunk-graph
    /// binding, or spec annotation — the lexical binding is a
    /// different value than the outer one those describe.
    local_shadowed: &'a BTreeSet<String>,
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
        let p = classify_expr_purity(
            expr,
            self.shadowed,
            self.local_shadowed,
            self.declared_pure,
            self.graph,
        );
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

    // Iteration statements fire user code beyond their
    // sub-expressions: `for-of` (and `for await-of`) runs the
    // iterated value's `[Symbol.iterator]` / `[Symbol.asyncIterator]`
    // protocol; `for-in` runs `[[OwnPropertyKeys]]` /
    // `[[GetOwnPropertyDescriptor]]` per step, which a Proxy traps.
    // The RHS expression's own evaluation is classified by the
    // regular recursion, but the protocol firing is invisible to it
    // — flag the statement itself.
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        let reason = Purity::from_reason_with_detail(
            PurityRule::IterationProtocol,
            node.span,
            "for-of fires the iterator protocol on the iterated value".to_string(),
        );
        self.purity = std::mem::replace(&mut self.purity, Purity::Pure).worst(reason);
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        let reason = Purity::from_reason_with_detail(
            PurityRule::IterationProtocol,
            node.span,
            "for-in enumeration can fire proxy traps on the enumerated value".to_string(),
        );
        self.purity = std::mem::replace(&mut self.purity, Purity::Pure).worst(reason);
        node.visit_children_with(self);
    }

    // `const {a} = o` / `const [x] = o` inside a body fire getters /
    // the iterator protocol on the initializer value — effects the
    // init expression's own classification doesn't cover.
    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Some(reason) = param_destructuring_purity(&node.name) {
            self.purity = std::mem::replace(&mut self.purity, Purity::Pure).worst(reason);
        }
        node.visit_children_with(self);
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
    /// A coercing operator (`+`, `-`, relational, loose equality,
    /// `~`, unary `+`/`-`, template interpolation, `in`,
    /// `instanceof`) whose operand is not statically known to be a
    /// primitive. ToPrimitive / ToNumber / ToString /
    /// `[Symbol.hasInstance]` / proxy-`has` on an object operand
    /// fires user code.
    CoercingOperator,
    /// A computed property key (`obj[key]`, `{[key]: v}`,
    /// `class { [key]() {} }`) whose key expression is not
    /// statically known to be a primitive (or a whitelisted
    /// `Symbol.*` well-known symbol). ToPropertyKey on an object
    /// key fires `toString` / `[Symbol.toPrimitive]`.
    ToPropertyKeyCoercion,
    /// `for-of` / `for await-of` / `for-in` — iteration fires the
    /// iterator protocol or proxy enumeration traps on the
    /// iterated value.
    IterationProtocol,
    /// A destructuring pattern (declarator name or function
    /// parameter) — object patterns fire `[[Get]]`, array patterns
    /// fire the iterator protocol on the bound value.
    DestructuringPattern,
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

pub(crate) fn classify_expr_purity(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    match expr {
        Expr::Lit(_) => Purity::Pure,
        Expr::Ident(_) => Purity::Pure,
        Expr::This(_) | Expr::MetaProp(_) => Purity::Pure,
        // Template interpolation runs ToString on each interpolated
        // value — an object operand fires user `toString` /
        // `[Symbol.toPrimitive]`. Each interpolation must therefore
        // be statically primitive-valued in addition to being pure
        // to evaluate.
        Expr::Tpl(tpl) => tpl
            .exprs
            .iter()
            .map(|e| {
                let p = classify_expr_purity(e, shadowed, local_shadowed, declared_pure, graph);
                if is_result_primitive(e, &graph.primitive_const_bindings, local_shadowed) {
                    p
                } else {
                    p.worst(Purity::from_reason_with_detail(
                        PurityRule::CoercingOperator,
                        e.span(),
                        "template interpolation runs ToString on a possibly-object value"
                            .to_string(),
                    ))
                }
            })
            .fold(Purity::Pure, Purity::worst),
        Expr::Fn(_) | Expr::Arrow(_) => Purity::Pure,
        Expr::Class(class_expr) => {
            if class_has_static_observable(
                &class_expr.class,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            ) {
                Purity::from_reason(PurityRule::ClassStaticObservable, class_expr.class.span)
            } else {
                Purity::Pure
            }
        }
        Expr::Paren(p) => {
            classify_expr_purity(&p.expr, shadowed, local_shadowed, declared_pure, graph)
        }
        Expr::Unary(u) => {
            let arg_purity =
                classify_expr_purity(&u.arg, shadowed, local_shadowed, declared_pure, graph);
            match u.op {
                UnaryOp::Delete => Purity::from_reason(PurityRule::DeleteOperator, u.span),
                // `typeof` has no coercion path; `void` discards;
                // `!` runs ToBoolean, which is type-cased and fires
                // no user code on any value (ECMA-262 §7.1.2).
                // Pure iff the operand is.
                UnaryOp::TypeOf | UnaryOp::Void | UnaryOp::Bang => arg_purity,
                // Unary `+` / `-` / `~` run ToNumber / ToNumeric —
                // on an object operand that fires user `valueOf` /
                // `[Symbol.toPrimitive]`. Pure only when the
                // operand is statically primitive-valued.
                UnaryOp::Plus | UnaryOp::Minus | UnaryOp::Tilde => {
                    if is_result_primitive(&u.arg, &graph.primitive_const_bindings, local_shadowed)
                    {
                        arg_purity
                    } else {
                        arg_purity.worst(Purity::from_reason_with_detail(
                            PurityRule::CoercingOperator,
                            u.span,
                            format!("unary {} runs ToNumber on a possibly-object operand", u.op),
                        ))
                    }
                }
            }
        }
        Expr::Bin(b) => {
            let operands =
                classify_expr_purity(&b.left, shadowed, local_shadowed, declared_pure, graph)
                    .worst(classify_expr_purity(
                        &b.right,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    ));
            match b.op {
                // No-coercion operators: short-circuit logicals
                // evaluate operands as-is; strict (in)equality is
                // type-cased with no ToPrimitive path (ECMA-262
                // §7.2.16 IsStrictlyEqual).
                BinaryOp::LogicalAnd
                | BinaryOp::LogicalOr
                | BinaryOp::NullishCoalescing
                | BinaryOp::EqEqEq
                | BinaryOp::NotEqEq => operands,
                // `in` runs ToPropertyKey on the LHS and HasProperty
                // on the RHS (proxy `has` trap); `instanceof` runs
                // GetMethod(RHS, @@hasInstance) and reads
                // `RHS.prototype` (proxy `get` trap). The
                // interesting RHS is always an object, so no
                // primitive-operand gate can admit these.
                BinaryOp::In | BinaryOp::InstanceOf => {
                    operands.worst(Purity::from_reason_with_detail(
                        PurityRule::CoercingOperator,
                        b.span,
                        format!(
                            "`{}` can fire proxy traps / @@hasInstance on its operand",
                            b.op
                        ),
                    ))
                }
                // Every remaining operator (arithmetic, relational,
                // loose equality, bitwise, shifts, `+`) coerces its
                // operands via ToPrimitive / ToNumeric, which fires
                // user `valueOf` / `toString` / `[Symbol.toPrimitive]`
                // on object operands. Pure only when both operands
                // are statically primitive-valued.
                _ => {
                    if is_result_primitive(&b.left, &graph.primitive_const_bindings, local_shadowed)
                        && is_result_primitive(
                            &b.right,
                            &graph.primitive_const_bindings,
                            local_shadowed,
                        )
                    {
                        operands
                    } else {
                        operands.worst(Purity::from_reason_with_detail(
                            PurityRule::CoercingOperator,
                            b.span,
                            format!(
                                "binary {} runs ToPrimitive on a possibly-object operand",
                                b.op
                            ),
                        ))
                    }
                }
            }
        }
        Expr::Cond(c) => {
            classify_expr_purity(&c.test, shadowed, local_shadowed, declared_pure, graph)
                .worst(classify_expr_purity(
                    &c.cons,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ))
                .worst(classify_expr_purity(
                    &c.alt,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ))
        }
        Expr::Seq(s) => s
            .exprs
            .iter()
            .map(|e| classify_expr_purity(e, shadowed, local_shadowed, declared_pure, graph))
            .fold(Purity::Pure, Purity::worst),
        Expr::Array(arr) => {
            classify_array_literal_purity(arr, shadowed, local_shadowed, declared_pure, graph)
        }
        Expr::Object(obj) => obj
            .props
            .iter()
            .map(|prop| classify_prop_purity(prop, shadowed, local_shadowed, declared_pure, graph))
            .fold(Purity::Pure, Purity::worst),
        Expr::Member(member) => {
            classify_member_purity(member, shadowed, local_shadowed, declared_pure, graph)
        }
        Expr::SuperProp(s) => Purity::from_reason(PurityRule::SuperProp, s.span),
        // Optional chaining (`recv?.prop`, `recv?.()`) only adds a
        // null/undefined short-circuit on top of plain member /
        // call evaluation; it doesn't introduce side effects of
        // its own. Recurse through the OptChainBase so an
        // OptChain that expands to a whitelisted static-property
        // read or a whitelisted call returns the same `Pure` /
        // `Unknown` answer the non-optional shape would. R1 in
        // docs/design.md "Open design questions / OptChain purity".
        Expr::OptChain(opt) => match &*opt.base {
            OptChainBase::Member(member) => {
                classify_member_purity(member, shadowed, local_shadowed, declared_pure, graph)
            }
            OptChainBase::Call(opt_call) => classify_callee_call(
                &opt_call.callee,
                &opt_call.args,
                opt.span,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            ),
        },
        Expr::Call(call) => {
            classify_call_purity(call, shadowed, local_shadowed, declared_pure, graph)
        }
        Expr::New(new_expr) => {
            classify_new_expr_purity(new_expr, shadowed, local_shadowed, declared_pure, graph)
        }
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

fn classify_array_literal_purity(
    arr: &ArrayLit,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    arr.elems
        .iter()
        .flatten()
        .map(|elem| {
            classify_array_literal_element_purity(
                elem,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            )
        })
        .fold(Purity::Pure, Purity::worst)
}

fn classify_array_literal_element_purity(
    elem: &ExprOrSpread,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    if let Some(sp) = elem.spread {
        return classify_fresh_array_spread_source(
            &elem.expr,
            None,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        )
        .unwrap_or_else(|| Purity::from_reason(PurityRule::ArraySpread, sp));
    }
    classify_expr_purity(&elem.expr, shadowed, local_shadowed, declared_pure, graph)
}

/// Array spread is only side-effect-free when the source is known to
/// evaluate to a fresh ordinary Array whose own element expressions
/// are pure. This admits literal and conditional-literal shapes like
/// `...[1, 2]` and `...(flag ? ["a"] : [])` while preserving the
/// conservative `array_spread` verdict for arbitrary iterables.
///
/// When `for_iterable` is `Some(callee)`, the Array arm classifies
/// elements for `new Set(...)` / `new Map(...)` iterable semantics
/// instead of generic array-literal purity.
fn classify_fresh_array_spread_source(
    expr: &Expr,
    for_iterable: Option<&str>,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Option<Purity> {
    match expr {
        Expr::Paren(p) => classify_fresh_array_spread_source(
            &p.expr,
            for_iterable,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        ),
        Expr::Array(arr) => Some(if let Some(callee) = for_iterable {
            arr.elems
                .iter()
                .map(|elem| {
                    classify_iterable_element(
                        elem.as_ref(),
                        arr.span,
                        callee,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    )
                })
                .fold(Purity::Pure, Purity::worst)
        } else {
            classify_array_literal_purity(arr, shadowed, local_shadowed, declared_pure, graph)
        }),
        Expr::Cond(cond) => Some(
            classify_expr_purity(&cond.test, shadowed, local_shadowed, declared_pure, graph)
                .worst(classify_fresh_array_spread_source(
                    &cond.cons,
                    for_iterable,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                )?)
                .worst(classify_fresh_array_spread_source(
                    &cond.alt,
                    for_iterable,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                )?),
        ),
        _ => None,
    }
}

/// Borrow the `(recv_ident_sym, prop_sym)` pair for a static-ident
/// member access spelled as either `recv.prop` (`Expr::Member`) or
/// `recv?.prop` (`Expr::OptChain { base: OptChainBase::Member }`).
/// Returns `None` for any other shape (chained member access,
/// computed access, non-Ident receivers, private names, OptCall
/// bases). The returned references borrow from the AST node and
/// outlive only the surrounding match — short-lived by design,
/// because the `pure_members` admission only needs the strings to
/// look up the per-binding declared-pure set.
fn static_member_obj_prop(expr: &Expr) -> Option<(&str, &str)> {
    let member = match expr {
        Expr::Member(member) => member,
        Expr::OptChain(opt) => match opt.base.as_ref() {
            OptChainBase::Member(member) => member,
            OptChainBase::Call(_) => return None,
        },
        _ => return None,
    };
    let Expr::Ident(recv) = member.obj.as_ref() else {
        return None;
    };
    let MemberProp::Ident(prop) = &member.prop else {
        return None;
    };
    Some((recv.sym.as_ref(), prop.sym.as_ref()))
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
///   * Spec-declared `purity: pure_new` bindings, with all args pure.
///
/// Everything else (non-Ident callees, shadowed names, tagged
/// templates, other arg shapes) falls through to `Unknown`.
fn classify_new_expr_purity(
    new_expr: &NewExpr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
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
        && !local_shadowed.contains(name)
        && arg_count == 0
    {
        return Purity::Pure;
    }
    // `new X("literal")` against PURE_BUILTIN_NEW_STRING_LITERAL_ARG.
    // The argument must be a string LITERAL (no spread): the
    // admission arguments on the table are about parsing a string —
    // a non-literal expression would additionally need value-class
    // tracking to prove it can't be an object whose ToString fires
    // user code.
    if let Some(name) = PURE_BUILTIN_NEW_STRING_LITERAL_ARG
        .iter()
        .copied()
        .find(|n| *n == callee.sym.as_ref())
        && !shadowed.contains(name)
        && !local_shadowed.contains(name)
        && let Some(args) = new_expr.args.as_ref()
        && args.len() == 1
        && args[0].spread.is_none()
        && matches!(args[0].expr.as_ref(), Expr::Lit(Lit::Str(_)))
    {
        return Purity::Pure;
    }
    if let Some(name) = PURE_BUILTIN_NEW_ARRAY_ITERABLE
        .iter()
        .copied()
        .find(|n| *n == callee.sym.as_ref())
        && !shadowed.contains(name)
        && !local_shadowed.contains(name)
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
            local_shadowed,
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
    // `pure_new` is an author trust contract on the chunk-top
    // binding; a body-local binding of the same name is a different
    // value the contract doesn't cover.
    if !local_shadowed.contains(callee.sym.as_ref())
        && graph.is_declared_pure_new(callee.sym.as_ref())
    {
        let args = new_expr.args.as_deref().unwrap_or(&[]);
        let arg_purity = all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
        if arg_purity.is_pure() {
            return Purity::Pure;
        }
        return Purity::from_reason_with_detail(
            PurityRule::UnknownNew,
            new_expr.span,
            format!(
                "new {}(...) has impure argument(s) despite pure_new annotation",
                callee.sym
            ),
        )
        .worst(arg_purity);
    }
    Purity::from_reason_with_detail(
        PurityRule::UnknownNew,
        new_expr.span,
        callee.sym.to_string(),
    )
}

/// Classify the iterable arg of `new Set([...])` / `new Map([[k,v],...])`.
/// Returns `Purity::Pure` only when the arg is an Array literal with
/// every element a Pure expression (no holes; spreads only when the
/// source is a fresh Array literal, optionally behind a pure
/// conditional; for `Map`, every expanded element is a 2-element
/// Array literal of Pure entries). Map's iterator path Get's [0]/[1]
/// on each entry — fresh literal entries guarantee those reads are
/// own-data-property hits, not user-getter hits on a 2-tuple-shaped
/// object.
///
/// On failure returns a `NotPure` carrying the offending sub-expression's
/// reason(s) so the caller can attach them to the surrounding
/// `UnknownNew` verdict.
fn classify_array_literal_for_iterable(
    expr: &Expr,
    callee: &str,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
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
                local_shadowed,
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
    local_shadowed: &BTreeSet<String>,
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
        return classify_fresh_array_spread_source(
            &elem.expr,
            Some(callee),
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        )
        .unwrap_or_else(|| Purity::from_reason(PurityRule::ArraySpread, sp));
    }
    match callee {
        "Set" => classify_expr_purity(&elem.expr, shadowed, local_shadowed, declared_pure, graph),
        "Map" => classify_map_entry(&elem.expr, shadowed, local_shadowed, declared_pure, graph),
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
    local_shadowed: &BTreeSet<String>,
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
            Some(e) => {
                classify_expr_purity(&e.expr, shadowed, local_shadowed, declared_pure, graph)
            }
        })
        .fold(Purity::Pure, Purity::worst)
}

/// Purity of `member` taken as an r-value member access
/// (`recv.prop` or `recv?.prop`). Pure iff one of:
///
/// * The receiver+property pair is whitelisted on a non-shadowed
///   global (e.g. `Math.PI`, `Object.freeze`).
/// * The receiver is a chunk-local `Ident` bound to a confirmed
///   `ChunkBinding::PlainData` shape (see `ChunkCodeGraph::build` for
///   the soundness argument). For computed access, the key
///   sub-expression must itself be pure; non-computed (`X.k`) and
///   private-name (`X.#k`) reads are unconditionally pure.
///
/// Otherwise `Unknown` — `obj.prop` on an arbitrary object can fire
/// a getter, which we can't rule out statically.
fn classify_member_purity(
    member: &MemberExpr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    if let Some((recv, prop)) = static_member_pair(member)
        && !shadowed.contains(recv)
        && !local_shadowed.contains(recv)
        && (PURE_STATIC_PROPS.contains(&(recv, prop))
            || PURE_STATIC_FUNCTION_REFS.contains(&(recv, prop)))
    {
        return Purity::Pure;
    }
    if let Expr::Ident(recv) = member.obj.as_ref()
        && graph.is_plain_data(recv.sym.as_ref())
        // A function param / local binding of the same name lexically
        // shadows the chunk-top PlainData const, so `recv` here is the
        // local (possibly a getter-bearing object), not the plain-data
        // shape. Admitting it as pure would be a soundness hole.
        && !local_shadowed.contains(recv.sym.as_ref())
    {
        return match &member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => Purity::Pure,
            // Computed access runs ToPropertyKey on the key VALUE
            // before the (accessor-free) lookup on the PlainData
            // receiver — an object key fires user `toString` /
            // `[Symbol.toPrimitive]`. The key must therefore be
            // statically primitive-valued (or a well-known Symbol)
            // in addition to being pure to evaluate.
            MemberProp::Computed(computed) => {
                let key_purity = classify_expr_purity(
                    &computed.expr,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                );
                if is_safe_property_key(
                    &computed.expr,
                    shadowed,
                    local_shadowed,
                    &graph.primitive_const_bindings,
                ) {
                    key_purity
                } else {
                    key_purity.worst(Purity::from_reason_with_detail(
                        PurityRule::ToPropertyKeyCoercion,
                        computed.span,
                        "computed key runs ToPropertyKey on a possibly-object value".to_string(),
                    ))
                }
            }
        };
    }
    // Member read on a fluent chain (`k.string().description`,
    // including the bare-root `k.object` read inside a larger chain
    // when classified standalone): the author's `fluent_exports`
    // assertion covers member reads on the root and on every derived
    // value. Static (non-computed) properties only — a computed read
    // would additionally need a ToPropertyKey-safe key, so it falls
    // through to the conservative verdict. The chain verdict carries
    // any inner call-argument impurity.
    if let MemberProp::Ident(_) | MemberProp::PrivateName(_) = &member.prop
        && let Some(chain) =
            classify_fluent_chain(&member.obj, shadowed, local_shadowed, declared_pure, graph)
    {
        return chain;
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
    local_shadowed: &BTreeSet<String>,
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
        local_shadowed,
        declared_pure,
        graph,
    )
}

/// Classify `expr` as a fluent chain: `Some(purity)` when the
/// expression is a chain of static member reads / calls rooted in a
/// fluent-trusted binding (`ChunkCodeGraph::fluent_bindings`), `None`
/// when it isn't a fluent chain at all (caller falls through to the
/// regular arms).
///
/// The returned purity is the combined verdict of **every call
/// argument throughout the chain** — the author's assertion covers
/// the API's own functions and their results, not the evaluation of
/// caller-supplied arguments, so
/// `k.object(sideEffect()).describe("x")` must surface
/// `sideEffect()`'s impurity even though the chain shape is trusted.
/// Trust propagation mirrors `fluent_chain_root` exactly: static
/// members and calls only — a computed member or `new` breaks the
/// chain (falls back to the conservative classifier).
///
/// A body-local binding shadowing the root makes the called value a
/// different value than the one the author annotated, so the chain is
/// not trusted then (same rule as every other author-trust arm).
fn classify_fluent_chain(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Option<Purity> {
    match strip_parens(expr) {
        Expr::Ident(ident)
            if graph.is_fluent_binding(ident.sym.as_ref())
                && !local_shadowed.contains(ident.sym.as_ref()) =>
        {
            Some(Purity::Pure)
        }
        Expr::Member(member) => match &member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => {
                classify_fluent_chain(&member.obj, shadowed, local_shadowed, declared_pure, graph)
            }
            MemberProp::Computed(_) => None,
        },
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return None;
            };
            classify_fluent_chain(callee, shadowed, local_shadowed, declared_pure, graph).map(
                |chain| {
                    chain.worst(all_args_pure(
                        &call.args,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    ))
                },
            )
        }
        Expr::OptChain(opt) => match &*opt.base {
            OptChainBase::Member(member) => match &member.prop {
                MemberProp::Ident(_) | MemberProp::PrivateName(_) => classify_fluent_chain(
                    &member.obj,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ),
                MemberProp::Computed(_) => None,
            },
            OptChainBase::Call(opt_call) => classify_fluent_chain(
                &opt_call.callee,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            )
            .map(|chain| {
                chain.worst(all_args_pure(
                    &opt_call.args,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ))
            }),
        },
        _ => None,
    }
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
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    // Author-declared pure binding: a chunk-local function whose
    // spec member carries `purity: "pure"`. The annotation is an
    // explicit override and wins over both the whitelist and the
    // chunk-top (A8) shadowing check — the spec author asserts that
    // THIS bound value is pure regardless of what its body does or
    // whether an import shadows the name. It does NOT win over a
    // body-local binding of the same name (`local_shadowed`): a
    // function param or local var is a *different value* than the
    // chunk-top binding the author annotated, so the trust contract
    // doesn't cover it. See AGENTS.md "Declared purity".
    if let Expr::Ident(ident) = callee_expr
        && declared_pure.contains(ident.sym.as_ref())
        && !local_shadowed.contains(ident.sym.as_ref())
    {
        return all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
    }
    // Author-declared pure member call: a binding whose spec member
    // carries `pure_members: [<prop>, …]`. Admits
    // `<binding>.<prop>(args)` as pure with args still classified
    // independently. Static identifier-property only — computed
    // access (`<binding>[expr](...)`) and private fields fall back
    // to the regular classifier path. Both the call-then-opt form
    // (`b?.forwardRef(args)`, callee = `Expr::OptChain(Member)`)
    // and the opt-call form (`b.forwardRef?.(args)`, callee =
    // `Expr::Member`) qualify under the same admission rule via
    // `static_member_obj_prop`. As with `purity: pure` on a
    // direct-Ident call, the annotation overrides chunk-top (A8)
    // shadowing — the spec author asserts THIS bound value is pure
    // regardless of where it came from — but NOT a body-local
    // binding of the same name (a param/local is a different value
    // than the annotated chunk-top binding). See AGENTS.md
    // "Declared purity".
    if let Some((recv, prop)) = static_member_obj_prop(callee_expr)
        && !local_shadowed.contains(recv)
        && graph.is_declared_pure_member(recv, prop)
    {
        return all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
    }
    // Fluent chain rooted in an author-asserted `fluent_exports`
    // binding: the callee may be arbitrarily deep
    // (`k.object({...}).optional().describe`), where every
    // intermediate receiver is a call *result* — unreachable by any
    // binding-keyed arm above. `classify_fluent_chain` validated and
    // carried every inner call's argument purity; this call's own
    // args are still classified normally.
    if let Some(chain) =
        classify_fluent_chain(callee_expr, shadowed, local_shadowed, declared_pure, graph)
    {
        return chain.worst(all_args_pure(
            args,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        ));
    }
    // Vite/Rollup namespace facade:
    // `Object.defineProperty({ __proto__: null, ...exports },
    // Symbol.toStringTag, { value: "Module" })`. The mutation targets a
    // fresh object and installs a data descriptor only, so no user code
    // fires. The generic `Object.defineProperty(t, ...)` form remains
    // unknown below.
    if is_pure_object_define_property_on_fresh_namespace(
        callee_expr,
        args,
        shadowed,
        local_shadowed,
        declared_pure,
        graph,
    ) {
        return Purity::Pure;
    }
    // Chunk-local function declaration: consult the per-chunk
    // function-body purity cache. `Pure` callee + Pure args → Pure;
    // non-Pure callee inherits its reasons (so the chain points
    // back through to the unhandled construct in the function body).
    // A body-local binding of the same name shadows the chunk-top
    // function — the called value is unknown then.
    if let Expr::Ident(ident) = callee_expr
        && !local_shadowed.contains(ident.sym.as_ref())
        && let Some(callee_purity) = graph.function_purity(ident.sym.as_ref())
    {
        return callee_purity.worst(all_args_pure(
            args,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        ));
    }
    // Imported function with a cross-module purity verdict from the
    // program-level oracle (`imported_purities`). Same shape as the
    // chunk-local arm: a `Pure` import + Pure args → Pure; an impure
    // import inherits its reasons. A body-local binding of the same name
    // shadows the import, so the called value is unknown then.
    if let Expr::Ident(ident) = callee_expr
        && !local_shadowed.contains(ident.sym.as_ref())
        && let Some(callee_purity) = graph.imported_purity(ident.sym.as_ref())
    {
        return callee_purity.worst(all_args_pure(
            args,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        ));
    }
    // `Recv.method(args)` against PURE_STATIC_CALLS.
    if let Expr::Member(member) = callee_expr
        && let Some((recv, prop)) = static_member_pair(member)
        && !shadowed.contains(recv)
        && !local_shadowed.contains(recv)
        && PURE_STATIC_CALLS.contains(&(recv, prop))
    {
        return all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
    }
    // `Object.{entries,keys,values,freeze}(<plain-data arg>)` /
    // `Object.fromEntries(<entry-array literal>)` — admitted when
    // the argument is structurally a plain ordinary literal (no
    // accessors / methods / `__proto__` / computed keys / spread)
    // OR a chunk-top `PlainData` binding whose accessor-free shape
    // is enforced by `collect_plain_data_bindings` /
    // `PlainDataWriteScanner`. See `PURE_OBJECT_CALLS_ON_PLAIN_DATA`
    // for the per-entry soundness argument.
    if let Expr::Member(member) = callee_expr
        && let Some((recv, prop)) = static_member_pair(member)
        && !shadowed.contains(recv)
        && !local_shadowed.contains(recv)
        && PURE_OBJECT_CALLS_ON_PLAIN_DATA.contains(&(recv, prop))
        && args.len() == 1
        && args[0].spread.is_none()
        && is_pure_plain_data_arg_for(
            prop,
            &args[0].expr,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        )
    {
        return Purity::Pure;
    }
    // `globalCallable(args)` against PURE_GLOBAL_CALLS.
    if let Expr::Ident(ident) = callee_expr
        && let Some(name) = PURE_GLOBAL_CALLS
            .iter()
            .copied()
            .find(|n| *n == ident.sym.as_ref())
        && !shadowed.contains(name)
        && !local_shadowed.contains(name)
    {
        return all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
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
        && !local_shadowed.contains(name)
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

fn is_pure_object_define_property_on_fresh_namespace(
    callee_expr: &Expr,
    args: &[ExprOrSpread],
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let Expr::Member(member) = strip_parens(callee_expr) else {
        return false;
    };
    if !matches!(
        (member.obj.as_ref(), &member.prop),
        (Expr::Ident(obj), MemberProp::Ident(prop))
            if obj.sym.as_ref() == "Object" && prop.sym.as_ref() == "defineProperty"
    ) || shadowed.contains("Object")
        || local_shadowed.contains("Object")
        || args.len() != 3
        || args.iter().any(|arg| arg.spread.is_some())
    {
        return false;
    }
    is_fresh_namespace_object_literal(
        &args[0].expr,
        shadowed,
        local_shadowed,
        declared_pure,
        graph,
    ) && is_symbol_to_string_tag(&args[1].expr, shadowed, local_shadowed)
        && is_data_descriptor_literal(
            &args[2].expr,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        )
}

fn is_fresh_namespace_object_literal(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let Expr::Object(obj) = strip_parens(expr) else {
        return false;
    };
    obj.props.iter().all(|prop| match prop {
        PropOrSpread::Spread(_) => false,
        PropOrSpread::Prop(prop) => match prop.as_ref() {
            Prop::Shorthand(_) => true,
            Prop::KeyValue(kv) => {
                if prop_name_is(&kv.key, "__proto__") {
                    return matches!(strip_parens(&kv.value), Expr::Lit(Lit::Null(_)));
                }
                prop_name_is_static_data_key(&kv.key)
                    && classify_expr_purity(
                        &kv.value,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    )
                    .is_pure()
            }
            Prop::Getter(_) | Prop::Setter(_) | Prop::Method(_) | Prop::Assign(_) => false,
        },
    })
}

fn is_symbol_to_string_tag(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
) -> bool {
    let Expr::Member(member) = strip_parens(expr) else {
        return false;
    };
    matches!(
        (member.obj.as_ref(), &member.prop),
        (Expr::Ident(obj), MemberProp::Ident(prop))
            if obj.sym.as_ref() == "Symbol"
                && prop.sym.as_ref() == "toStringTag"
                && !shadowed.contains("Symbol")
                && !local_shadowed.contains("Symbol")
    )
}

fn is_data_descriptor_literal(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let Expr::Object(obj) = strip_parens(expr) else {
        return false;
    };
    obj.props.iter().all(|prop| match prop {
        PropOrSpread::Spread(_) => false,
        PropOrSpread::Prop(prop) => match prop.as_ref() {
            Prop::KeyValue(kv) => {
                prop_name_is_static_data_key(&kv.key)
                    && !prop_name_is(&kv.key, "get")
                    && !prop_name_is(&kv.key, "set")
                    && classify_expr_purity(
                        &kv.value,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    )
                    .is_pure()
            }
            Prop::Shorthand(_)
            | Prop::Getter(_)
            | Prop::Setter(_)
            | Prop::Method(_)
            | Prop::Assign(_) => false,
        },
    })
}

fn prop_name_is_static_data_key(name: &PropName) -> bool {
    matches!(
        name,
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_) | PropName::BigInt(_)
    )
}

fn prop_name_is(name: &PropName, expected: &str) -> bool {
    match name {
        PropName::Ident(ident) => ident.sym.as_ref() == expected,
        PropName::Str(s) => s.value.to_string_lossy() == expected,
        PropName::Num(_) | PropName::BigInt(_) | PropName::Computed(_) => false,
    }
}

/// Whether `arg` is a sound argument shape for the
/// `PURE_OBJECT_CALLS_ON_PLAIN_DATA` admission rule at the given
/// `Object.<prop>` callsite. Two admissible shapes:
///
/// * **Fresh plain-data literal.** An `Expr::Object` with only
///   `Prop::KeyValue` / `Prop::Shorthand` (no `__proto__`, computed
///   keys, methods, getters, setters, or `Prop::Assign`), or an
///   `Expr::Array` (literal). The literal itself must classify
///   pure — that catches impure value sub-expressions and spreads
///   of non-plain-data sources (the existing
///   `classify_array_spread_source` gate). For `Object.fromEntries`
///   the Array form is required AND each element must itself be a
///   2-element Array literal with pure values, paralleling the
///   `new Map([[k, v], …])` gate.
///
/// * **`PlainData` chunk-top binding.** A bare `Expr::Ident` whose
///   `name` is registered as `ChunkBinding::PlainData` in the
///   chunk graph. `collect_plain_data_bindings` only registers
///   bindings whose initializers pass `is_plain_data_init` (same
///   plain-data shape predicate) AND whose chunk-wide write scan
///   ruled out accessor installation post-init. So a PlainData
///   binding carries the same "no accessor channels at any
///   program point" invariant as a fresh literal.
///
/// Returning `false` falls back to `Unknown` — the soundness-first
/// default.
fn is_pure_plain_data_arg_for(
    prop: &str,
    arg: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let arg = strip_parens(arg);
    if prop == "fromEntries" {
        // `Object.fromEntries(I)` iterates I. Restrict I to a fresh
        // Array literal whose every element is a 2-element Array
        // literal with pure values — same admission shape as
        // `new Map([[k, v], …])` (PURE_BUILTIN_NEW_ARRAY_ITERABLE).
        return matches!(arg, Expr::Array(_))
            && is_fresh_entry_array_for_from_entries(
                arg,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            );
    }
    match arg {
        // PlainData chunk-top binding read: provably plain-data
        // shape, no accessor channels anywhere in the chunk — unless a
        // local binding of the same name lexically shadows it, in which
        // case `ident` is the local (possibly getter-bearing) value.
        Expr::Ident(ident) => {
            graph.is_plain_data(ident.sym.as_ref()) && !local_shadowed.contains(ident.sym.as_ref())
        }
        // Fresh object literal with no accessor channels.
        Expr::Object(obj) => {
            obj.props.iter().all(is_plain_data_prop)
                && classify_expr_purity(arg, shadowed, local_shadowed, declared_pure, graph)
                    .is_pure()
        }
        // Fresh array literal; element purity (including spread
        // sources) is handled by the standard literal classifier.
        Expr::Array(_) => {
            classify_expr_purity(arg, shadowed, local_shadowed, declared_pure, graph).is_pure()
        }
        Expr::Call(call) if prop == "freeze" => {
            let Callee::Expr(callee) = &call.callee else {
                return false;
            };
            is_pure_object_define_property_on_fresh_namespace(
                callee,
                &call.args,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            )
        }
        _ => false,
    }
}

/// `Object.fromEntries([[k1, v1], [k2, v2], …])` admission shape.
/// Every outer element must be a 2-element Array literal whose
/// element expressions classify pure (key + value). No spreads at
/// either level: a spread fires the iterable's `[Symbol.iterator]`
/// which can call user code outside the literal-only domain we
/// claim sound here. Matches the
/// `PURE_BUILTIN_NEW_ARRAY_ITERABLE`-style entry test for `Map`.
fn is_fresh_entry_array_for_from_entries(
    arg: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let Expr::Array(outer) = arg else {
        return false;
    };
    outer.elems.iter().all(|elem| {
        let Some(elem) = elem else {
            return false;
        };
        if elem.spread.is_some() {
            return false;
        }
        let Expr::Array(entry) = strip_parens(&elem.expr) else {
            return false;
        };
        if entry.elems.len() != 2 {
            return false;
        }
        entry.elems.iter().flatten().all(|kv| {
            kv.spread.is_none()
                && classify_expr_purity(&kv.expr, shadowed, local_shadowed, declared_pure, graph)
                    .is_pure()
        })
    })
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

/// Whether evaluating `expr` is statically guaranteed to produce a
/// **primitive** value, so a parent operator's ToPrimitive /
/// ToString / ToNumber / ToPropertyKey coercion of the RESULT
/// cannot fire user code. This predicate only answers "is the
/// resulting value primitive?" — the operand's own evaluation
/// effects are classified separately by the regular purity
/// recursion.
///
/// Admitted result-primitive shapes:
/// * primitive literals (string / number / boolean / null / bigint;
///   regex literals are objects and excluded);
/// * template literals (always evaluate to a string);
/// * any unary operator except `delete` (`typeof` → string, `void`
///   → undefined, `!` → boolean, `+`/`-`/`~` → number/bigint);
/// * any binary operator except the short-circuit logicals
///   (`&&` / `||` / `??` return one of their operands verbatim;
///   everything else — arithmetic, relational, equality, bitwise,
///   `in`, `instanceof` — returns a number/string/bigint/boolean);
/// * conditionals whose both branches are result-primitive;
/// * a reference to a chunk-top `const` provably bound to a
///   primitive value (`primitives`, from
///   `collect_primitive_const_bindings`), when the name is not
///   locally shadowed.
///
/// The global `undefined` is deliberately NOT admitted: it is an
/// ordinary (shadowable) global binding, not a keyword, and the
/// shadowed-globals pass doesn't track it. Over-restriction is the
/// acceptable failure mode.
///
/// No admitted shape can produce a Symbol, so ToString on a
/// result-primitive value never hits the symbol TypeError path.
fn is_result_primitive(
    expr: &Expr,
    primitives: &BTreeSet<String>,
    local_shadowed: &BTreeSet<String>,
) -> bool {
    match strip_parens(expr) {
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_)) => true,
        Expr::Tpl(_) => true,
        // A chunk-top `const` provably bound to a primitive value: the
        // binding is immutable and the value carries no user accessors,
        // so coercing it fires no user code — unless a local binding
        // shadows the name in the current scope.
        Expr::Ident(id) => {
            primitives.contains(id.sym.as_ref()) && !local_shadowed.contains(id.sym.as_ref())
        }
        Expr::Unary(u) => u.op != UnaryOp::Delete,
        Expr::Bin(b) => !matches!(
            b.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ),
        Expr::Cond(c) => {
            is_result_primitive(&c.cons, primitives, local_shadowed)
                && is_result_primitive(&c.alt, primitives, local_shadowed)
        }
        _ => false,
    }
}

/// Whether `key` is a safe computed property key: ToPropertyKey on
/// its value fires no user code. True for result-primitive
/// expressions (string/number coercion of a primitive is
/// engine-only) and for whitelisted well-known-symbol reads
/// (`Symbol.iterator` etc. — Symbols are valid property keys with
/// no coercion path), provided neither the global `Symbol` nor a
/// local binding shadows the receiver.
fn is_safe_property_key(
    key: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    primitives: &BTreeSet<String>,
) -> bool {
    if is_result_primitive(key, primitives, local_shadowed) {
        return true;
    }
    if let Expr::Member(member) = strip_parens(key)
        && let Some((recv, prop)) = static_member_pair(member)
        && recv == "Symbol"
        && !shadowed.contains(recv)
        && !local_shadowed.contains(recv)
        && PURE_STATIC_PROPS.contains(&(recv, prop))
    {
        return true;
    }
    false
}

fn all_args_pure(
    args: &[ExprOrSpread],
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    args.iter()
        .map(|arg| {
            // Spread arg's iterator could fire side effects.
            let spread = arg
                .spread
                .map(|sp| Purity::from_reason(PurityRule::ArraySpread, sp));
            let body =
                classify_expr_purity(&arg.expr, shadowed, local_shadowed, declared_pure, graph);
            spread.unwrap_or(Purity::Pure).worst(body)
        })
        .fold(Purity::Pure, Purity::worst)
}

fn classify_prop_purity(
    prop: &PropOrSpread,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    match prop {
        PropOrSpread::Spread(spread) => {
            // Spreading an arbitrary expression invokes its
            // iterator (array spread) or property iteration
            // (object spread). Either can fire a getter or a
            // user-defined `[Symbol.iterator]`.
            classify_expr_purity(&spread.expr, shadowed, local_shadowed, declared_pure, graph)
                .worst(Purity::from_reason(
                    PurityRule::ObjectSpread,
                    spread.expr.span(),
                ))
        }
        PropOrSpread::Prop(prop) => match prop.as_ref() {
            Prop::Shorthand(_) => Purity::Pure,
            Prop::KeyValue(kv) => {
                classify_propname_purity(&kv.key, shadowed, local_shadowed, declared_pure, graph)
                    .worst(classify_expr_purity(
                        &kv.value,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    ))
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
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    match name {
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_) | PropName::BigInt(_) => {
            Purity::Pure
        }
        // Defining a property under a computed key runs
        // ToPropertyKey on the key value — an object key fires user
        // `toString` / `[Symbol.toPrimitive]`. The key must be
        // statically primitive-valued (or a well-known Symbol) in
        // addition to being pure to evaluate.
        PropName::Computed(c) => {
            let key_purity =
                classify_expr_purity(&c.expr, shadowed, local_shadowed, declared_pure, graph);
            if is_safe_property_key(
                &c.expr,
                shadowed,
                local_shadowed,
                &graph.primitive_const_bindings,
            ) {
                key_purity
            } else {
                key_purity.worst(Purity::from_reason_with_detail(
                    PurityRule::ToPropertyKeyCoercion,
                    c.span,
                    "computed key runs ToPropertyKey on a possibly-object value".to_string(),
                ))
            }
        }
    }
}

/// Whether a class declaration runs observable code at class-decl
/// time. Eagerly-evaluated class parts (aligned with the fact
/// collector's `visit_eager_member_parts` / `visit_class_decl`):
///
/// * **decorators** (class-level or member-level) — decorator
///   application CALLS the decorator function at class-definition
///   time; any decorator present is observable.
/// * **`extends <expr>`** — the superclass expression evaluates at
///   class-definition time; an impure expression (e.g.
///   `extends f()`) is observable. A pure-classified expression
///   (Ident etc.) is admitted: reading `.prototype` off a plain
///   constructor fires no user code (function `prototype` is an
///   own data property; the Proxy-superclass case falls under
///   assumption A11, intrinsic/exotic-object integrity).
/// * **computed member keys** (any member kind, static or not) —
///   key expressions evaluate eagerly AND their values undergo
///   ToPropertyKey (user `toString` on object keys), so the key
///   must be pure and statically primitive-valued (or a well-known
///   `Symbol.*`).
/// * **static field / static auto-accessor initializers** — run at
///   class-definition time; impure initializer is observable.
///
/// Static blocks always run. Instance field/accessor initializers,
/// constructor and method bodies are lazy and not consulted.
pub(crate) fn class_has_static_observable(
    class: &Class,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let value_impure = |v: &Expr| {
        !classify_expr_purity(v, shadowed, local_shadowed, declared_pure, graph).is_pure()
    };
    let key_observable = |key: &PropName| match key {
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_) | PropName::BigInt(_) => false,
        PropName::Computed(c) => {
            value_impure(&c.expr)
                || !is_safe_property_key(
                    &c.expr,
                    shadowed,
                    local_shadowed,
                    &graph.primitive_const_bindings,
                )
        }
    };
    if !class.decorators.is_empty() {
        return true;
    }
    if let Some(super_class) = class.super_class.as_deref()
        && value_impure(super_class)
    {
        return true;
    }
    class.body.iter().any(|member| match member {
        ClassMember::StaticBlock(_) => true,
        ClassMember::Method(method) => {
            !method.function.decorators.is_empty() || key_observable(&method.key)
        }
        ClassMember::PrivateMethod(method) => !method.function.decorators.is_empty(),
        ClassMember::Constructor(_) => false,
        ClassMember::ClassProp(prop) => {
            !prop.decorators.is_empty()
                || key_observable(&prop.key)
                || (prop.is_static && prop.value.as_deref().is_some_and(value_impure))
        }
        ClassMember::PrivateProp(prop) => {
            !prop.decorators.is_empty()
                || (prop.is_static && prop.value.as_deref().is_some_and(value_impure))
        }
        ClassMember::AutoAccessor(accessor) => {
            !accessor.decorators.is_empty()
                || (match &accessor.key {
                    Key::Public(name) => key_observable(name),
                    Key::Private(_) => false,
                })
                || (accessor.is_static && accessor.value.as_deref().is_some_and(value_impure))
        }
        ClassMember::TsIndexSignature(_) | ClassMember::Empty(_) => false,
    })
}

#[cfg(test)]
mod classifier_tests;

#[cfg(test)]
mod graph_purity_tests;

#[cfg(test)]
mod redundant_hints_tests;
