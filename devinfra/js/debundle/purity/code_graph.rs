use super::*;

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
    pub(crate) bindings: BTreeMap<String, ChunkBinding>,
    pure_new_constructors: BTreeSet<String>,
    /// Per-binding declared-pure member-call set. Looked up by the
    /// classifier when it sees `<recv>.<prop>(args)` — if `recv` is a
    /// non-shadowed key here and `<prop>` is in the set, the call is
    /// admitted as pure with args evaluated normally. Author-trust
    /// contract; see <docs/purity_soundness.md> "Declared purity".
    declared_pure_members: BTreeMap<String, BTreeSet<String>>,
    /// Chunk-top `const X = <result-primitive init>` binding names.
    /// `const` makes the binding immutable, and a primitive value
    /// carries no user accessors, so a `ToString` / `ToNumber` /
    /// `ToPropertyKey` coercion of a reference to `X` fires no user
    /// code — `X` is admitted as result-primitive wherever it is not
    /// locally shadowed. The init's own evaluation effects are
    /// classified separately at its declaration site; only the
    /// *value* class matters here. See `collect_primitive_const_bindings`.
    pub(crate) primitive_const_bindings: BTreeSet<String>,
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
pub(crate) enum ChunkBinding {
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
pub(crate) fn param_destructuring_purity(pat: &Pat) -> Option<Purity> {
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
