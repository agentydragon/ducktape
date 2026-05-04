//! Static schedule validation for `materialize_logical_modules`.
//!
//! Background: see <DESIGN.md>. This module is the validator core
//! of the principled debundler design. It treats debundling as a
//! scheduling problem:
//!
//! 1. For each top-level statement in the source chunk, compute the
//!    bindings it declares, the bindings it reads at-init, the
//!    bindings it reads lazily (inside function/method bodies, etc.),
//!    whether it has an observable side effect, and its source
//!    ordinal.
//! 2. Map each statement to its destination module (logical module
//!    or residual entry) using the spec's binding assignment.
//! 3. Build the imports graph `I`: edge `M_S → M_b` for every
//!    `(S, b)` where statement `S` lives in module `M_S` and `b`
//!    is owned by `M_b ≠ M_S`, irrespective of whether the read is
//!    at-init or lazy. Each edge of `I` corresponds to one
//!    emitted `import` directive — `I` is exactly the graph the
//!    ESM linker walks for evaluation order.
//! 4. Validate: `I ∪ S` must be acyclic. Cycles are the
//!    unrealizable case — no ESM evaluation order can satisfy the
//!    spec's assignment without TDZ on at-init reads or wrong
//!    side-effect ordering. `materialize_logical_modules` aborts
//!    when this validator reports cycles.
//!
//! The output is a JSON report listing the cycles + their evidence
//! (which `(statement, binding)` pairs form each cycle), plus
//! recommendations for any unowned bindings that some logical
//! module reads. The report is written next to the existing
//! manifests as `<chunk_id>.schedule.json`.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use petgraph::algo::{greedy_feedback_arc_set, tarjan_scc, toposort};
use petgraph::graph::DiGraph;
use petgraph::graphmap::DiGraphMap;
use petgraph::visit::EdgeRef;
use serde::Serialize;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

/// Index into the materializer's `module_plans` list, identifying a
/// logical module produced by the spec.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct LogicalModuleIndex(pub usize);

/// Identity of a module the schedule validator reasons about. The
/// residual entry is a first-class variant rather than a sentinel
/// index, so callers can't accidentally treat it as a normal logical
/// module.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub enum ModuleId {
    Logical(LogicalModuleIndex),
    ResidualEntry,
}

/// Position of a top-level statement in a chunk's source body.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize)]
#[serde(transparent)]
pub struct StatementOrdinal(pub usize);

/// Local name of a binding in a chunk's top-level scope. Stays a
/// plain `String` (the actual JavaScript identifier text); the alias
/// is documentation. See DESIGN.md "Identifiers and types".
pub type BindingName = String;

/// How a top-level binding in the chunk relates to the split. See
/// DESIGN.md "Two binding kinds".
#[derive(Debug, Clone)]
pub enum BindingKind {
    /// Declared by a top-level `var/let/const/function/class` in this
    /// chunk; the spec assigns it to a logical module (or the
    /// residual entry).
    Owned { owner: ModuleId },
    /// Introduced by an `import { imported_name as <local> } from
    /// "<source>"` in the chunk's top-level body. The value lives in
    /// another chunk; logical modules can re-export it under their
    /// own public name. Multiple modules may re-export the same
    /// imported binding (under different public names).
    Imported {
        /// The original imported name from the source chunk (e.g. "j"
        /// for `import { j as a } from "..."`).
        imported_name: BindingName,
        /// Output-tree-rooted absolute path of the import source
        /// (e.g. `"static/vendor.js"`). Already resolved against the
        /// chunk's directory + the artifact's source-chunk index;
        /// emit-time path resolution is just `relative(dest_dir,
        /// imported_from)`.
        imported_from: String,
        /// `module → public export name` for each logical module that
        /// re-exports this binding. Empty when no logical module
        /// re-exports it (read-only references stay implicit and are
        /// resolved by `source_chunk_imports_for_moved_body`).
        re_exported_by: BTreeMap<ModuleId, BindingName>,
    },
}

/// A logical module produced by the spec for the current chunk.
/// Projection of `ModulePlan` carrying the fields downstream emit
/// helpers consume (`cross_module_imports_for_body`,
/// `source_chunk_imports_for_moved_body`, etc.).
#[derive(Debug, Clone)]
pub struct LogicalModule {
    pub id: String,
    /// Chunk-relative path the module emits to (e.g. `"runtime/foo.js"`).
    pub target_file: String,
    /// Local-name → exported-name map for the bindings this module
    /// owns. Empty when the module re-exports only imported
    /// bindings.
    pub rename_map: BTreeMap<BindingName, BindingName>,
}

/// Single per-chunk schedule. Carries everything downstream code
/// needs to validate cycles and emit modules in an order that
/// respects `I ∪ S`.
#[derive(Debug, Clone)]
pub struct Schedule {
    pub chunk_id: String,
    pub facts: Vec<StatementFacts>,
    pub bindings: BTreeMap<BindingName, BindingKind>,
    pub logical_modules: Vec<LogicalModule>,
    pub dep_graph: ModuleDepGraph,
    /// Topological linearization of `I ∪ S`, dependency-first
    /// (the module at index 0 must evaluate before any other; the
    /// last module — typically the residual entry — evaluates
    /// last). Empty when `dep_graph` has cycles (validation will
    /// reject the spec). Used by the emitter to author each
    /// module's `import` directive list in an order that steers
    /// ECMA-262's linker DFS toward an `I ∪ S`-respecting
    /// evaluation order; see DESIGN.md "Lemma 2".
    pub linker_order: Vec<ModuleId>,
}

impl Schedule {
    /// Build a schedule from chunk facts + the binding catalogue +
    /// spec-derived logical modules. `bindings` should already have
    /// every `Owned` binding the spec assigned and every `Imported`
    /// binding the spec re-exports.
    pub fn build(
        chunk_id: String,
        facts: Vec<StatementFacts>,
        bindings: BTreeMap<BindingName, BindingKind>,
        logical_modules: Vec<LogicalModule>,
    ) -> Self {
        let ownership = owned_view(&bindings);
        let dep_graph = build_module_dep_graph(&facts, &ownership);
        let linker_order = compute_linker_order(&dep_graph, &logical_modules);
        Self {
            chunk_id,
            facts,
            bindings,
            logical_modules,
            dep_graph,
            linker_order,
        }
    }

    /// Position of `id` in `linker_order`, if present. Used by the
    /// emitter to sort each module's `import` directives so that
    /// ECMA-262's depth-first link traversal evaluates dependencies
    /// before dependents.
    pub fn linker_position(&self, id: ModuleId) -> Option<usize> {
        self.linker_order.iter().position(|&m| m == id)
    }

    /// Render `id` to a human-readable label (used in cycle reports).
    pub fn module_name(&self, id: ModuleId) -> String {
        match id {
            ModuleId::ResidualEntry => "<residual_entry>".to_string(),
            ModuleId::Logical(LogicalModuleIndex(idx)) => self
                .logical_modules
                .get(idx)
                .map(|m| m.id.clone())
                .unwrap_or_else(|| format!("<module#{idx}>")),
        }
    }

    /// Which logical module owns a binding (by local name), if any.
    /// Returns `None` for names that aren't `Owned` in this schedule
    /// (e.g. globals, imported bindings, names not in the spec).
    pub fn owner_of(&self, name: &str) -> Option<ModuleId> {
        self.bindings.get(name).and_then(|kind| match kind {
            BindingKind::Owned { owner } => Some(*owner),
            BindingKind::Imported { .. } => None,
        })
    }

    /// Lookup a logical module by index.
    pub fn logical_module(&self, idx: LogicalModuleIndex) -> Option<&LogicalModule> {
        self.logical_modules.get(idx.0)
    }

    /// Run SCC analysis over the dep graph + compute assignment
    /// recommendations for unowned bindings. Spec authors consume the
    /// resulting report to (a) fix any cycles and (b) make implicit
    /// assignments explicit.
    pub fn validate(&self) -> ScheduleReport {
        let mut report = validate_schedule(&self.dep_graph, &|id| self.module_name(id));
        report.recommendations = build_recommendations(self);
        report.linker_order = self
            .linker_order
            .iter()
            .map(|id| self.module_name(*id))
            .collect();
        report
    }
}

#[derive(Debug, Clone)]
pub struct StatementFacts {
    pub ordinal: StatementOrdinal,
    pub declared: BTreeSet<BindingName>,
    pub reads_at_init: BTreeSet<BindingName>,
    /// Reads happening only inside lazy syntactic positions (function
    /// bodies, instance class-field initializers, getters/setters,
    /// constructor bodies). May overlap with `reads_at_init` if the
    /// same name appears in both eager and lazy positions of the
    /// statement.
    pub reads_lazy: BTreeSet<BindingName>,
    pub has_side_effect: bool,
    pub kind: StatementKind,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StatementKind {
    /// `var X = ...`, `let X = ...`, `const X = ...`. RHS reads at-init.
    VarDecl,
    /// `function X() { ... }`. Hoisted; no at-init reads from body.
    FnDecl,
    /// `class X { ... }`. Extends, decorators, computed keys, and
    /// static blocks read at-init.
    ClassDecl,
    /// `export { ... }`, `export X`, etc. Lazy reads (re-exports).
    Export,
    /// `import { ... } from ...`. Linked, no at-init body code.
    Import,
    /// Bare expression / control-flow / etc. that doesn't declare a
    /// top-level binding.
    SideEffect,
}

/// Walk the chunk's module body and produce one `StatementFacts`
/// entry per top-level statement, in source order.
///
/// Multi-declarator `var/let/const` statements are split into
/// per-declarator entries before analysis, so each row declares
/// a single name and `stmt_owner` (in `build_module_dep_graph`)
/// returns an unambiguous owner. Without the split, a chunk like
/// `const A = 1, B = readsX;` with `{A → mod_a, B → mod_b}`
/// would attribute `B`'s read of `X` to `mod_a` (the first
/// declared name's owner), inventing or hiding cycles. The
/// emitter splits the same comma-lists separately at lower-time
/// (`split_var_decl` in `logical_modules.rs`); this pre-split
/// just teaches the analyzer the same view.
/// Locate the first top-level `await` expression in `module`'s
/// body, if any. Returns the source-order ordinal of the offending
/// statement (in the post-comma-list-split view that
/// `analyze_chunk_facts` uses, so reports align with statement
/// indices in `<chunk_id>.schedule.json`).
///
/// "Top-level" excludes function/method/arrow/getter/setter
/// bodies and class instance-field initializers — those are lazy
/// scopes that may legitimately contain `await` without making
/// the module a top-level-await module.
pub fn find_top_level_await(module: &Module) -> Option<StatementOrdinal> {
    let body = split_comma_list_var_decls(&module.body);
    for (ordinal, item) in body.iter().enumerate() {
        let mut finder = TopLevelAwaitFinder::default();
        item.visit_with(&mut finder);
        if finder.found {
            return Some(StatementOrdinal(ordinal));
        }
    }
    None
}

#[derive(Default)]
struct TopLevelAwaitFinder {
    found: bool,
}

impl Visit for TopLevelAwaitFinder {
    fn visit_await_expr(&mut self, _node: &AwaitExpr) {
        self.found = true;
    }

    // Lazy boundaries — `await` inside any of these is the body's
    // own concern (and only legal if the body is itself `async`).
    fn visit_function(&mut self, _node: &Function) {}
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {}
    fn visit_method_prop(&mut self, _node: &MethodProp) {}
    fn visit_getter_prop(&mut self, _node: &GetterProp) {}
    fn visit_setter_prop(&mut self, _node: &SetterProp) {}

    // Class-member handling mirrors `AtInitReadCollector::visit_class_member`:
    //   - computed property keys are eager (evaluated at class-decl
    //     time) regardless of `is_static`;
    //   - `is_static` field initializers + static blocks are eager;
    //   - instance field initializers are evaluated per-`new`, so
    //     they're lazy from the class-decl's POV;
    //   - method bodies are functions and the `visit_function`
    //     override above keeps them lazy.
    fn visit_class_member(&mut self, member: &ClassMember) {
        match member {
            ClassMember::Method(method) => {
                self.visit_prop_name(&method.key);
            }
            ClassMember::PrivateMethod(_) => {}
            ClassMember::Constructor(_) => {}
            ClassMember::ClassProp(prop) => {
                self.visit_prop_name(&prop.key);
                if prop.is_static
                    && let Some(value) = &prop.value
                {
                    value.visit_with(self);
                }
            }
            ClassMember::PrivateProp(prop) => {
                if prop.is_static
                    && let Some(value) = &prop.value
                {
                    value.visit_with(self);
                }
            }
            ClassMember::StaticBlock(block) => {
                block.visit_with(self);
            }
            ClassMember::TsIndexSignature(_) | ClassMember::Empty(_) => {}
            ClassMember::AutoAccessor(accessor) => {
                if let Key::Public(name) = &accessor.key {
                    self.visit_prop_name(name);
                }
                if accessor.is_static
                    && let Some(value) = &accessor.value
                {
                    value.visit_with(self);
                }
            }
        }
    }

    fn visit_prop_name(&mut self, name: &PropName) {
        if let PropName::Computed(computed) = name {
            computed.expr.visit_with(self);
        }
    }
}

pub fn analyze_chunk_facts(
    module: &Module,
    declared_pure: &BTreeSet<String>,
) -> Vec<StatementFacts> {
    let body = split_comma_list_var_decls(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build(&body, &shadowed, declared_pure);
    body.iter()
        .enumerate()
        .map(|(ordinal, item)| {
            analyze_item(
                StatementOrdinal(ordinal),
                item,
                &shadowed,
                declared_pure,
                &graph,
            )
        })
        .collect()
}

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
    fn build(
        body: &[ModuleItem],
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
            let old = self.function_purity(name).expect("seeded by build");
            let combined = old.worst(new_purity);
            if combined != old {
                self.bindings
                    .insert(name.clone(), ChunkBinding::Function { purity: combined });
                if let Some(callers) = callers_in_scc.get(&i) {
                    pending.extend(callers.iter().copied());
                }
            }
        }
    }

    /// Purity of the chunk-local function bound to `name`, if any.
    /// Returns `None` for non-function bindings (imports, vars,
    /// classes) and for names not bound at chunk top.
    fn function_purity(&self, name: &str) -> Option<Purity> {
        match self.bindings.get(name)? {
            ChunkBinding::Function { purity } => Some(*purity),
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

fn collect_chunk_functions(body: &[ModuleItem]) -> Vec<ChunkFunction<'_>> {
    let mut out = Vec::new();
    for item in body {
        match item {
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
        self.purity = self.purity.worst(p);
    }

    // Statement-level effects that don't surface as an Impure /
    // Unknown sub-expression. `throw e` alters control flow
    // observably even when `e` is a Pure literal; `debugger`
    // pauses execution observably to a host attached to the
    // process. Both make the enclosing function not Pure.
    fn visit_throw_stmt(&mut self, node: &ThrowStmt) {
        self.purity = self.purity.worst(Purity::Impure);
        // Still recurse so the thrown expression contributes its
        // own purity (e.g. `throw io()` should also see the call).
        node.arg.visit_with(self);
    }

    fn visit_debugger_stmt(&mut self, _node: &DebuggerStmt) {
        self.purity = self.purity.worst(Purity::Impure);
    }
}

/// Replace every multi-declarator top-level `var/let/const`
/// (including the form nested in an `export` decl) with N
/// single-declarator statements preserving source order. Other
/// statement kinds pass through unchanged.
fn split_comma_list_var_decls(body: &[ModuleItem]) -> Vec<ModuleItem> {
    let mut out = Vec::with_capacity(body.len());
    for item in body {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) if var.decls.len() > 1 => {
                for decl in &var.decls {
                    let single = VarDecl {
                        span: var.span,
                        ctxt: var.ctxt,
                        kind: var.kind,
                        declare: var.declare,
                        decls: vec![decl.clone()],
                    };
                    out.push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(single)))));
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                match &export_decl.decl {
                    Decl::Var(var) if var.decls.len() > 1 => {
                        for decl in &var.decls {
                            let single = VarDecl {
                                span: var.span,
                                ctxt: var.ctxt,
                                kind: var.kind,
                                declare: var.declare,
                                decls: vec![decl.clone()],
                            };
                            out.push(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                                span: export_decl.span,
                                decl: Decl::Var(Box::new(single)),
                            })));
                        }
                    }
                    _ => out.push(item.clone()),
                }
            }
            _ => out.push(item.clone()),
        }
    }
    out
}

/// Walk `body` and collect the subset of `WHITELIST_RECEIVERS`
/// that are declared at the chunk's top-level scope (`var/let/const`,
/// `function`, `class`, exported decls) or bound by an import
/// specifier (default / namespace / named). The classifier consults
/// this set to skip the whitelist for any receiver the chunk
/// shadows — `const Math = …` and
/// `import { Math } from "./userland"` both make `Math.PI` an
/// Unknown read, not the global constant. See DESIGN.md A8.
fn compute_shadowed_globals(body: &[ModuleItem]) -> BTreeSet<&'static str> {
    let mut shadowed = BTreeSet::new();
    let try_shadow = |name: &str, into: &mut BTreeSet<&'static str>| {
        if let Some(global) = WHITELIST_RECEIVERS.iter().copied().find(|r| *r == name) {
            into.insert(global);
        }
    };
    for item in body {
        for name in collect_declared_names(item) {
            try_shadow(name.as_str(), &mut shadowed);
        }
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
            for spec in &import.specifiers {
                let local = match spec {
                    ImportSpecifier::Named(named) => named.local.sym.as_ref(),
                    ImportSpecifier::Default(default) => default.local.sym.as_ref(),
                    ImportSpecifier::Namespace(namespace) => namespace.local.sym.as_ref(),
                };
                try_shadow(local, &mut shadowed);
            }
        }
    }
    shadowed
}

fn analyze_item(
    ordinal: StatementOrdinal,
    item: &ModuleItem,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> StatementFacts {
    let kind = classify_item(item);
    let declared = collect_declared_names(item);
    let mut at_init = AtInitReadCollector::default();
    item.visit_with(&mut at_init);
    let mut lazy = LazyReadCollector::default();
    item.visit_with(&mut lazy);
    let has_side_effect = item_has_side_effect(item, kind, shadowed, declared_pure, graph);
    StatementFacts {
        ordinal,
        declared,
        reads_at_init: at_init.names,
        reads_lazy: lazy.names,
        has_side_effect,
        kind,
    }
}

/// Three-state expression-level purity (DESIGN.md "Module dep
/// graphs"). `Pure` is statically provably free of observable
/// side effects; `Impure` is provably side-effecting (assignment,
/// update, await, yield); `Unknown` covers the long tail (calls,
/// `new`, member access — could be a getter — etc.) and is
/// treated as `Impure` by `has_side_effect` for soundness.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum Purity {
    Pure,
    Impure,
    Unknown,
}

impl Purity {
    /// Combine two purity assessments — the worst (most
    /// side-effecting) wins. `Impure` dominates `Unknown`
    /// dominates `Pure`.
    fn worst(self, other: Self) -> Self {
        match (self, other) {
            (Purity::Impure, _) | (_, Purity::Impure) => Purity::Impure,
            (Purity::Unknown, _) | (_, Purity::Unknown) => Purity::Unknown,
            _ => Purity::Pure,
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
    ("Object", "freeze"),
    ("Object", "values"),
    ("Object", "keys"),
];

/// Receiver / global-callable names whose whitelist firing depends
/// on the chunk not having shadowed them at top level.
/// `analyze_chunk_facts` populates the shadowed-globals set, and
/// the classifier suppresses whitelist hits for any name in it —
/// e.g. `const Math = …` makes `Math.PI` fall back to `Unknown`.
const WHITELIST_RECEIVERS: &[&str] = &["Math", "Array", "Symbol", "Number", "Boolean", "Object"];

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

fn classify_expr_purity(
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
                Purity::Impure
            } else {
                Purity::Pure
            }
        }
        Expr::Paren(p) => classify_expr_purity(&p.expr, shadowed, declared_pure, graph),
        Expr::Unary(u) => match u.op {
            UnaryOp::Delete => Purity::Impure,
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
        Expr::Array(arr) => {
            let mut acc = Purity::Pure;
            for elem in arr.elems.iter().flatten() {
                if elem.spread.is_some() {
                    // Spread invokes the iterator protocol; could
                    // be impure even on a literal.
                    acc = acc.worst(Purity::Unknown);
                }
                acc = acc.worst(classify_expr_purity(
                    &elem.expr,
                    shadowed,
                    declared_pure,
                    graph,
                ));
            }
            acc
        }
        Expr::Object(obj) => {
            let mut acc = Purity::Pure;
            for prop in &obj.props {
                acc = acc.worst(classify_prop_purity(prop, shadowed, declared_pure, graph));
            }
            acc
        }
        Expr::Member(member) => {
            if let Some((recv, prop)) = static_member_pair(member)
                && !shadowed.contains(recv)
                && (PURE_STATIC_PROPS.contains(&(recv, prop))
                    || PURE_STATIC_FUNCTION_REFS.contains(&(recv, prop)))
            {
                return Purity::Pure;
            }
            // `obj.prop` on an arbitrary object can fire a getter;
            // we can't tell statically.
            Purity::Unknown
        }
        Expr::SuperProp(_) | Expr::OptChain(_) => Purity::Unknown,
        Expr::Call(call) => classify_call_purity(call, shadowed, declared_pure, graph),
        Expr::New(_) | Expr::TaggedTpl(_) => Purity::Unknown,
        Expr::Assign(_) | Expr::Update(_) => Purity::Impure,
        Expr::Await(_) | Expr::Yield(_) => Purity::Impure,
        // Anything we didn't enumerate falls into the Unknown
        // bucket — soundness-first.
        _ => Purity::Unknown,
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

fn classify_call_purity(
    call: &CallExpr,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Callee::Expr(callee_expr) = &call.callee else {
        return Purity::Unknown;
    };
    // Author-declared pure binding: a chunk-local function whose
    // spec member carries `purity: "pure"`. The annotation is an
    // explicit override and wins over both the whitelist and the
    // shadowing check (the spec author asserts that THIS bound
    // value is pure regardless of what its body does or whether
    // an import shadows the name). See AGENTS.md "Declared
    // purity".
    if let Expr::Ident(ident) = callee_expr.as_ref()
        && declared_pure.contains(ident.sym.as_ref())
    {
        return all_args_pure(&call.args, shadowed, declared_pure, graph);
    }
    // Chunk-local function declaration: consult the per-chunk
    // function-body purity cache. `Pure` callee + Pure args → Pure;
    // `Impure` callee → Impure (no matter the args); `Unknown`
    // callee inherits.
    if let Expr::Ident(ident) = callee_expr.as_ref()
        && let Some(callee_purity) = graph.function_purity(ident.sym.as_ref())
    {
        return callee_purity.worst(all_args_pure(&call.args, shadowed, declared_pure, graph));
    }
    // `Recv.method(args)` against PURE_STATIC_CALLS.
    if let Expr::Member(member) = callee_expr.as_ref()
        && let Some((recv, prop)) = static_member_pair(member)
        && !shadowed.contains(recv)
        && PURE_STATIC_CALLS.contains(&(recv, prop))
    {
        return all_args_pure(&call.args, shadowed, declared_pure, graph);
    }
    // `globalCallable(args)` against PURE_GLOBAL_CALLS.
    if let Expr::Ident(ident) = callee_expr.as_ref()
        && let Some(name) = PURE_GLOBAL_CALLS
            .iter()
            .copied()
            .find(|n| *n == ident.sym.as_ref())
        && !shadowed.contains(name)
    {
        return all_args_pure(&call.args, shadowed, declared_pure, graph);
    }
    Purity::Unknown
}

fn all_args_pure(
    args: &[ExprOrSpread],
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let mut acc = Purity::Pure;
    for arg in args {
        if arg.spread.is_some() {
            // Spread arg's iterator could fire side effects.
            acc = acc.worst(Purity::Unknown);
        }
        acc = acc.worst(classify_expr_purity(
            &arg.expr,
            shadowed,
            declared_pure,
            graph,
        ));
    }
    acc
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
            classify_expr_purity(&spread.expr, shadowed, declared_pure, graph)
                .worst(Purity::Unknown)
        }
        PropOrSpread::Prop(prop) => match prop.as_ref() {
            Prop::Shorthand(_) => Purity::Pure,
            Prop::KeyValue(kv) => {
                classify_propname_purity(&kv.key, shadowed, declared_pure, graph).worst(
                    classify_expr_purity(&kv.value, shadowed, declared_pure, graph),
                )
            }
            Prop::Assign(_) => Purity::Impure,
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
fn class_has_static_observable(
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

fn item_has_side_effect(
    item: &ModuleItem,
    kind: StatementKind,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    match kind {
        StatementKind::Import | StatementKind::Export | StatementKind::FnDecl => false,
        StatementKind::VarDecl => var_decl_of_item(item)
            .iter()
            .flat_map(|var| var.decls.iter())
            .any(|d| match d.init.as_deref() {
                Some(init) => {
                    classify_expr_purity(init, shadowed, declared_pure, graph) != Purity::Pure
                }
                None => false,
            }),
        StatementKind::ClassDecl => class_of_item(item)
            .map(|c| class_has_static_observable(c, shadowed, declared_pure, graph))
            .unwrap_or(false),
        StatementKind::SideEffect => match item {
            ModuleItem::Stmt(Stmt::Expr(expr)) => {
                classify_expr_purity(&expr.expr, shadowed, declared_pure, graph) != Purity::Pure
            }
            // Bare blocks, control flow, loops, etc. — soundness-first.
            _ => true,
        },
    }
}

fn var_decl_of_item(item: &ModuleItem) -> Option<&VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Var(var) => Some(var),
            _ => None,
        },
        _ => None,
    }
}

fn class_of_item(item: &ModuleItem) -> Option<&Class> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(cls))) => Some(&cls.class),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Class(cls) => Some(&cls.class),
            _ => None,
        },
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl)) => match &decl.decl {
            DefaultDecl::Class(cls) => Some(&cls.class),
            _ => None,
        },
        _ => None,
    }
}

fn classify_item(item: &ModuleItem) -> StatementKind {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(_)) => StatementKind::Import,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Var(_) => StatementKind::VarDecl,
            Decl::Fn(_) => StatementKind::FnDecl,
            Decl::Class(_) => StatementKind::ClassDecl,
            _ => StatementKind::Export,
        },
        ModuleItem::ModuleDecl(_) => StatementKind::Export,
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(_))) => StatementKind::VarDecl,
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => StatementKind::FnDecl,
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(_))) => StatementKind::ClassDecl,
        _ => StatementKind::SideEffect,
    }
}

fn collect_declared_names(item: &ModuleItem) -> BTreeSet<String> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_names(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => declaration_names(&decl.decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl)) => match &decl.decl {
            DefaultDecl::Fn(fn_expr) => fn_expr
                .ident
                .as_ref()
                .map(|id| std::iter::once(id.sym.to_string()).collect())
                .unwrap_or_default(),
            DefaultDecl::Class(class_expr) => class_expr
                .ident
                .as_ref()
                .map(|id| std::iter::once(id.sym.to_string()).collect())
                .unwrap_or_default(),
            _ => BTreeSet::new(),
        },
        _ => BTreeSet::new(),
    }
}

fn declaration_names(decl: &Decl) -> BTreeSet<String> {
    match decl {
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(|declarator| binding_names(&declarator.name))
            .collect(),
        Decl::Fn(fn_decl) => std::iter::once(fn_decl.ident.sym.to_string()).collect(),
        Decl::Class(class_decl) => std::iter::once(class_decl.ident.sym.to_string()).collect(),
        _ => BTreeSet::new(),
    }
}

fn binding_names(pattern: &Pat) -> Vec<String> {
    let mut out = Vec::new();
    walk_pattern(pattern, &mut out);
    out
}

fn walk_pattern(pattern: &Pat, out: &mut Vec<String>) {
    match pattern {
        Pat::Ident(id) => out.push(id.id.sym.to_string()),
        Pat::Array(arr) => {
            for element in arr.elems.iter().flatten() {
                walk_pattern(element, out);
            }
        }
        Pat::Object(obj) => {
            for prop in &obj.props {
                match prop {
                    ObjectPatProp::KeyValue(kv) => walk_pattern(&kv.value, out),
                    ObjectPatProp::Assign(assign) => out.push(assign.key.id.sym.to_string()),
                    ObjectPatProp::Rest(rest) => walk_pattern(&rest.arg, out),
                }
            }
        }
        Pat::Rest(rest) => walk_pattern(&rest.arg, out),
        Pat::Assign(assign) => walk_pattern(&assign.left, out),
        _ => {}
    }
}

/// Visitor that collects ident reads happening at-init only. Stops
/// at function bodies, method bodies, instance class-field
/// initializers, getter/setter bodies, and other lazy positions.
#[derive(Default)]
struct AtInitReadCollector {
    names: BTreeSet<String>,
}

impl Visit for AtInitReadCollector {
    fn visit_ident(&mut self, node: &Ident) {
        self.names.insert(node.sym.to_string());
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    // Export specifiers don't fire reads at module-init: ESM treats
    // them as a static export entry, linked lazily when consumers
    // import. Counting them as at-init reads adds spurious `R`
    // edges (and, post-Phase-5 where R ⊆ I, spurious `I` edges).
    // `export var X = ...` / `export class X {}` etc. are still
    // visited via `ExportDecl`; only the bare-specifier forms are
    // suppressed here.
    fn visit_named_export(&mut self, _node: &NamedExport) {}
    fn visit_export_all(&mut self, _node: &ExportAll) {}

    // Function bodies are lazy — references inside don't read at-init.
    fn visit_function(&mut self, _node: &Function) {}
    fn visit_fn_decl(&mut self, _node: &FnDecl) {}
    fn visit_fn_expr(&mut self, _node: &FnExpr) {}
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {}
    fn visit_method_prop(&mut self, _node: &MethodProp) {}
    fn visit_getter_prop(&mut self, _node: &GetterProp) {}
    fn visit_setter_prop(&mut self, _node: &SetterProp) {}

    fn visit_class(&mut self, node: &Class) {
        // Decorators on the class are eager.
        for decorator in &node.decorators {
            decorator.visit_with(self);
        }
        // Extends-clause is eager.
        if let Some(super_class) = &node.super_class {
            super_class.visit_with(self);
        }
        for member in &node.body {
            self.visit_class_member(member);
        }
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        match member {
            ClassMember::Method(method) => {
                // Method's name (computed key) is eager; body is lazy.
                self.visit_prop_name(&method.key);
            }
            ClassMember::PrivateMethod(_) => {}
            ClassMember::Constructor(_) => {}
            ClassMember::ClassProp(prop) => {
                // Computed keys are eager regardless of static-ness.
                self.visit_prop_name(&prop.key);
                if prop.is_static {
                    if let Some(value) = &prop.value {
                        value.visit_with(self);
                    }
                }
                // Instance field initializers are evaluated per-
                // instance — lazy from the class-decl's POV.
            }
            ClassMember::PrivateProp(prop) => {
                if prop.is_static {
                    if let Some(value) = &prop.value {
                        value.visit_with(self);
                    }
                }
            }
            ClassMember::StaticBlock(block) => {
                // Static block runs at class-decl time.
                block.visit_with(self);
            }
            ClassMember::TsIndexSignature(_) | ClassMember::Empty(_) => {}
            ClassMember::AutoAccessor(accessor) => {
                // accessor.key is a `Key` enum (Public/Private); for
                // public computed keys descend into the expression.
                if let Key::Public(name) = &accessor.key {
                    self.visit_prop_name(name);
                }
                if accessor.is_static {
                    if let Some(value) = &accessor.value {
                        value.visit_with(self);
                    }
                }
            }
        }
    }

    fn visit_prop_name(&mut self, name: &PropName) {
        if let PropName::Computed(computed) = name {
            computed.expr.visit_with(self);
        }
    }
}

/// Visitor that collects ident reads happening inside lazy syntactic
/// positions only — function bodies, method bodies, constructor
/// bodies, instance class-field initializers, getter/setter bodies.
/// Inverse boundary semantics from `AtInitReadCollector`.
#[derive(Default)]
struct LazyReadCollector {
    names: BTreeSet<String>,
    in_lazy: bool,
}

impl LazyReadCollector {
    fn descend_lazy<F: FnOnce(&mut Self)>(&mut self, f: F) {
        let prev = std::mem::replace(&mut self.in_lazy, true);
        f(self);
        self.in_lazy = prev;
    }
}

impl Visit for LazyReadCollector {
    fn visit_ident(&mut self, node: &Ident) {
        if self.in_lazy {
            self.names.insert(node.sym.to_string());
        }
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    fn visit_function(&mut self, node: &Function) {
        self.descend_lazy(|s| node.visit_children_with(s));
    }
    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.descend_lazy(|s| node.visit_children_with(s));
    }
    fn visit_method_prop(&mut self, node: &MethodProp) {
        node.key.visit_with(self);
        self.descend_lazy(|s| node.function.visit_with(s));
    }
    fn visit_getter_prop(&mut self, node: &GetterProp) {
        node.key.visit_with(self);
        self.descend_lazy(|s| {
            if let Some(body) = &node.body {
                body.visit_with(s);
            }
        });
    }
    fn visit_setter_prop(&mut self, node: &SetterProp) {
        node.key.visit_with(self);
        node.param.visit_with(self);
        self.descend_lazy(|s| {
            if let Some(body) = &node.body {
                body.visit_with(s);
            }
        });
    }

    fn visit_class(&mut self, node: &Class) {
        for decorator in &node.decorators {
            decorator.visit_with(self);
        }
        if let Some(super_class) = &node.super_class {
            super_class.visit_with(self);
        }
        for member in &node.body {
            self.visit_class_member(member);
        }
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        match member {
            ClassMember::Method(method) => {
                self.visit_prop_name(&method.key);
                self.descend_lazy(|s| method.function.visit_with(s));
            }
            ClassMember::PrivateMethod(method) => {
                self.descend_lazy(|s| method.function.visit_with(s));
            }
            ClassMember::Constructor(ctor) => {
                self.descend_lazy(|s| ctor.visit_children_with(s));
            }
            ClassMember::ClassProp(prop) => {
                self.visit_prop_name(&prop.key);
                if prop.is_static {
                    if let Some(value) = &prop.value {
                        value.visit_with(self);
                    }
                } else if let Some(value) = &prop.value {
                    self.descend_lazy(|s| value.visit_with(s));
                }
            }
            ClassMember::PrivateProp(prop) => {
                if prop.is_static {
                    if let Some(value) = &prop.value {
                        value.visit_with(self);
                    }
                } else if let Some(value) = &prop.value {
                    self.descend_lazy(|s| value.visit_with(s));
                }
            }
            ClassMember::StaticBlock(block) => {
                block.visit_with(self);
            }
            ClassMember::TsIndexSignature(_) | ClassMember::Empty(_) => {}
            ClassMember::AutoAccessor(accessor) => {
                if let Key::Public(name) = &accessor.key {
                    self.visit_prop_name(name);
                }
                if accessor.is_static {
                    if let Some(value) = &accessor.value {
                        value.visit_with(self);
                    }
                } else if let Some(value) = &accessor.value {
                    self.descend_lazy(|s| value.visit_with(s));
                }
            }
        }
    }

    fn visit_prop_name(&mut self, name: &PropName) {
        if let PropName::Computed(computed) = name {
            computed.expr.visit_with(self);
        }
    }
}

/// Why a particular `(from, to)` edge exists in the module dep
/// graph. The realizability gate distinguishes them: `AtInitRead`
/// edges constrain ESM evaluation order under TDZ semantics,
/// `LazyRead` edges only constrain it via the linker's depth-first
/// walk (lazy reads themselves don't fire until after evaluation
/// completes), and `SideEffect` edges encode source-order
/// ordering of side-effecting top-level statements between two
/// modules. An `I ∪ S` SCC is realizable iff every cross-module
/// edge between its members is a `LazyRead` — `AtInitRead` and
/// `SideEffect` cross-module edges both make the SCC
/// unrealizable (`AtInitRead` causes TDZ during cycle
/// evaluation; `SideEffect` has no consistent topological emit
/// order satisfying the constraint).
#[derive(Debug, Clone, Copy, Eq, PartialEq, Hash, Ord, PartialOrd)]
pub enum EdgeKind {
    /// At-init read: a top-level statement in `from` reads a
    /// binding owned by `to` synchronously, before any function
    /// body in `from` runs. `R ⊆ I`. Cross-module at-init reads
    /// inside an `I ∪ S` SCC make the spec unrealizable (TDZ).
    AtInitRead,
    /// Lazy read: a function/method/getter body inside `from`
    /// references a binding owned by `to`. The reference still
    /// emits an `import` directive in `from`'s body (so the edge
    /// is in `I`), but the read itself doesn't fire at-init —
    /// only after `to` has finished evaluating. Lazy-only cycles
    /// in `I` are realizable.
    LazyRead,
    /// Side-effect ordering: `from` has a side-effecting
    /// top-level statement that appears *after* a side-effecting
    /// statement in `to` in original source order. For ESM emit
    /// to preserve the original side-effect order, `to` must
    /// evaluate before `from`. `S` cycles are unrealizable
    /// (no consistent evaluation order satisfies the constraint).
    SideEffect,
}

/// One reason an edge `(from, to)` exists, with the source
/// statement ordinal that produced it.
#[derive(Debug, Clone)]
pub struct EdgeReason {
    pub kind: EdgeKind,
    pub statement_ordinal: StatementOrdinal,
    /// Binding being read, or the literal `<side-effect>` for
    /// `EdgeKind::SideEffect` rows.
    pub binding: BindingName,
}

/// Per-edge metadata. One physical `(from, to)` ESM `import`
/// directive can be backed by multiple reasons (e.g. several
/// at-init reads of bindings owned by the same target module);
/// they're all kept here so cycle reports can show every
/// triggering statement.
#[derive(Debug, Clone, Default)]
pub struct EdgeMetadata {
    pub reasons: Vec<EdgeReason>,
}

impl EdgeMetadata {
    /// `true` if at least one reason is an at-init read. The
    /// realizability gate uses this to decide whether an
    /// `I ∪ S` SCC contains an `R` cross-module edge.
    pub fn has_at_init_read(&self) -> bool {
        self.reasons.iter().any(|r| r.kind == EdgeKind::AtInitRead)
    }

    /// `true` if at least one reason is a side-effect ordering
    /// edge. `S` edges in an SCC make it unrealizable: the
    /// constraint is "predecessor must evaluate before
    /// successor", and a cycle has no topological emit order
    /// satisfying every such edge.
    pub fn has_side_effect_ordering(&self) -> bool {
        self.reasons.iter().any(|r| r.kind == EdgeKind::SideEffect)
    }

    /// `true` if this edge constrains the realizable evaluation
    /// order — an at-init read (`R`) or a side-effect ordering
    /// (`S`) edge. Lazy-only edges don't, because the reads they
    /// represent fire after every module in the cycle has
    /// finished evaluating.
    pub fn constrains_realizability(&self) -> bool {
        self.has_at_init_read() || self.has_side_effect_ordering()
    }
}

/// Module dep graph built from per-statement facts and a binding →
/// module assignment.
///
/// Backed by `petgraph::DiGraphMap`: one edge per directed
/// `(from, to)` pair, weight = `EdgeMetadata`. Multiple reasons
/// for the same physical edge (e.g. several at-init reads of
/// bindings owned by the same target module) accumulate into the
/// edge's reason list. Cycle detection runs through petgraph's
/// `tarjan_scc`.
#[derive(Debug, Clone, Default)]
pub struct ModuleDepGraph {
    pub graph: DiGraphMap<ModuleId, EdgeMetadata>,
}

impl ModuleDepGraph {
    fn record_reason(
        &mut self,
        from: ModuleId,
        to: ModuleId,
        kind: EdgeKind,
        statement_ordinal: StatementOrdinal,
        binding: BindingName,
    ) {
        if from == to {
            return;
        }
        if !self.graph.contains_edge(from, to) {
            self.graph.add_edge(from, to, EdgeMetadata::default());
        }
        // Safe: we just ensured the edge exists.
        let weight = self
            .graph
            .edge_weight_mut(from, to)
            .expect("edge was just added");
        weight.reasons.push(EdgeReason {
            kind,
            statement_ordinal,
            binding,
        });
    }

    /// Iterate edges as `(from, to, &EdgeMetadata)`.
    pub fn iter_edges(&self) -> impl Iterator<Item = (ModuleId, ModuleId, &EdgeMetadata)> + '_ {
        self.graph.all_edges()
    }

    /// Edge metadata, if the edge exists.
    pub fn edge(&self, from: ModuleId, to: ModuleId) -> Option<&EdgeMetadata> {
        self.graph.edge_weight(from, to)
    }

    /// `true` if the directed edge `(from, to)` is present and at
    /// least one of its reasons is an at-init read.
    pub fn has_at_init_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.graph
            .edge_weight(from, to)
            .is_some_and(EdgeMetadata::has_at_init_read)
    }

    /// `true` if the edge `(from, to)` exists and constrains
    /// realizable evaluation order (at-init read or side-effect
    /// ordering). Used by the realizability gate to decide
    /// whether an `I ∪ S` SCC is unrealizable.
    pub fn has_realizability_constraining_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.graph
            .edge_weight(from, to)
            .is_some_and(EdgeMetadata::constrains_realizability)
    }
}

/// Build the imports graph `I ∪ S` (per DESIGN.md "Module dep
/// graphs"): an edge `(M, M')` for every cross-module reference,
/// at-init or lazy, plus side-effect ordering edges between any
/// two modules with side-effecting top-level statements. Each
/// `I` edge corresponds to exactly one emitted `import { b } from
/// "<M'>"` directive in `M`'s body — so the graph's `I` slice is
/// exactly the graph the ESM linker walks for evaluation order.
///
/// A binding referenced both eagerly and lazily inside the same
/// statement (e.g. `class A extends B { method() { return B; } }`)
/// produces two `EdgeReason`s on the same `(from, to)` edge: one
/// `AtInitRead` (the extends-clause) and one `LazyRead` (the
/// method body). The realizability gate cares about the kind, so
/// keep both.
pub fn build_module_dep_graph(
    facts: &[StatementFacts],
    binding_assignment: &BTreeMap<BindingName, ModuleId>,
) -> ModuleDepGraph {
    let mut graph = ModuleDepGraph::default();
    let stmt_owner = |stmt: &StatementFacts| -> ModuleId {
        stmt.declared
            .iter()
            .filter_map(|name| binding_assignment.get(name).copied())
            .next()
            .unwrap_or(ModuleId::ResidualEntry)
    };
    let record_read = |graph: &mut ModuleDepGraph,
                       from: ModuleId,
                       binding: &BindingName,
                       ordinal: StatementOrdinal,
                       kind: EdgeKind| {
        let Some(&to) = binding_assignment.get(binding) else {
            return; // not a chunk-owned binding (global, ImportSpecifier, never-declared)
        };
        graph.record_reason(from, to, kind, ordinal, binding.clone());
    };
    for stmt in facts {
        let from = stmt_owner(stmt);
        for binding in &stmt.reads_at_init {
            record_read(
                &mut graph,
                from,
                binding,
                stmt.ordinal,
                EdgeKind::AtInitRead,
            );
        }
        for binding in &stmt.reads_lazy {
            record_read(&mut graph, from, binding, stmt.ordinal, EdgeKind::LazyRead);
        }
    }

    // Side-effect ordering edges (`S` per DESIGN.md "Module dep
    // graphs"). For every pair of side-effecting statements
    // (S₁, S₂) with `S₁.ordinal < S₂.ordinal` and
    // `home(S₁) ≠ home(S₂)`, add edge `(home(S₂), home(S₁))` —
    // home(S₂) depends on home(S₁), so home(S₁) must evaluate
    // first.
    //
    // Walk in source order; track which modules have already
    // contributed a side-effect statement. For each new
    // side-effecting statement, the home of the new statement
    // must come *after* every previously-seen side-effecting
    // module — add an edge to each such predecessor.
    //
    // `has_side_effect` is computed by `classify_expr_purity` so
    // pure literal initializers (`const X = 42`,
    // `const X = { a: 1 }`, function/class declarations without
    // observable static init) don't contribute to S. Without
    // that precision the cross-module S graph would be dense
    // enough to reject realistic specs for trivially pure const
    // sequences.
    //
    // Each `(from, to)` S-edge is recorded once even if the same
    // pair has multiple side-effecting statements crossing it —
    // the cycle report doesn't need to fan out into thousands of
    // S rows when the cycle structure is determined by the first
    // crossing.
    let mut seen_modules: BTreeSet<ModuleId> = BTreeSet::new();
    for stmt in facts.iter().filter(|s| s.has_side_effect) {
        let from = stmt_owner(stmt);
        let predecessors: Vec<ModuleId> = seen_modules
            .iter()
            .copied()
            .filter(|&m| m != from)
            .collect();
        for to in predecessors {
            let already_has_side_effect = graph
                .edge(from, to)
                .is_some_and(|md| md.reasons.iter().any(|r| r.kind == EdgeKind::SideEffect));
            if !already_has_side_effect {
                graph.record_reason(
                    from,
                    to,
                    EdgeKind::SideEffect,
                    stmt.ordinal,
                    "<side-effect>".to_string(),
                );
            }
        }
        seen_modules.insert(from);
    }

    graph
}

/// Result of validating a module dep graph.
#[derive(Debug, Clone, Serialize)]
pub struct ScheduleReport {
    pub kind: &'static str,
    pub cycles: Vec<CycleReport>,
    /// One entry per binding the spec hasn't claimed but that is
    /// referenced by at least one logical module. Spec authors
    /// resolve each entry by copying a chosen owner into the spec.
    pub recommendations: Vec<AssignmentRecommendation>,
    /// Topological linearization of `I ∪ S` rooted at the entry,
    /// dependency-first. Empty when the dep graph has cycles
    /// (validation rejects). Captured here so debug tooling can
    /// see the linker's evaluation order without re-running
    /// materialization. See DESIGN.md "Lemma 2".
    #[serde(rename = "linkerOrder")]
    pub linker_order: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CycleReport {
    pub modules: Vec<String>,
    pub evidence: Vec<CycleEdge>,
    /// Spec-author-actionable cut: a near-minimum set of
    /// realizability-constraining (`at-init` or `side-effect`)
    /// reasons whose removal would lift the cycle's realizability
    /// violation. Computed by [`compute_realizability_cut`].
    ///
    /// The cut never includes `lazy` reasons — lazy edges don't
    /// constrain ESM evaluation order, so removing one cannot help
    /// fix a cycle. Each entry corresponds to (and shares its
    /// shape with) a row in `evidence`.
    ///
    /// The algorithm is iterative: while the working subgraph
    /// still has an SCC carrying a cross-module
    /// realizability-constraining edge, run petgraph's
    /// `greedy_feedback_arc_set` (Eades-Lin-Smyth, 1993,
    /// `O(V + E)`) on the offending sub-SCC, pick the first FAS
    /// edge with an `R` or `S` reason, append its constraining
    /// reasons to the cut, remove it from the working graph, and
    /// repeat. Sound (every iteration removes one constraining
    /// edge from a problematic SCC) and heuristic-minimum
    /// (petgraph's FAS approximates within a constant factor on
    /// dense instances).
    #[serde(rename = "cut")]
    pub cut: Vec<CycleEdge>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CycleEdge {
    pub from: String,
    pub to: String,
    #[serde(rename = "statementOrdinal")]
    pub statement_ordinal: StatementOrdinal,
    pub binding: BindingName,
    /// Edge kind — `at-init`, `lazy`, or `side-effect`. Lets
    /// downstream consumers (cycle-evidence visualizers, spec
    /// authors triaging which edges to break) tell at a glance
    /// which reasons are actually realizability-constraining
    /// (`at-init` and `side-effect`) vs. inert-but-graph-present
    /// (`lazy`).
    pub kind: &'static str,
}

/// Spec author actionable: "binding X has no owner; here are the
/// modules that read it, and which assignments are cycle-safe."
#[derive(Debug, Clone, Serialize)]
pub struct AssignmentRecommendation {
    pub binding: BindingName,
    pub candidates: Vec<RecommendationCandidate>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RecommendationCandidate {
    pub module: String,
    #[serde(rename = "readKind")]
    pub read_kind: RecommendationReadKind,
    #[serde(rename = "cycleSafe")]
    pub cycle_safe: bool,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecommendationReadKind {
    /// The candidate module reads this binding at-init (initializer
    /// expression, extends-clause, computed key, etc.).
    AtInit,
    /// The candidate module reads this binding only inside lazy
    /// positions (function/method bodies, instance class-field
    /// initializers, getter/setter bodies). Contributes to the
    /// imports graph `I` (the emit must carry the corresponding
    /// `import` directive), but not to the at-init read sub-graph
    /// `R`. Lazy-only candidates need the same SCC check as
    /// at-init ones — see [`is_assignment_cycle_safe`].
    LazyOnly,
}

/// Find SCCs in the dep graph and produce a report listing every
/// non-trivial cycle (size > 1 OR a self-loop). Trivial single-node
/// non-self-loop SCCs are dropped.
pub fn validate_schedule(
    graph: &ModuleDepGraph,
    module_name: &dyn Fn(ModuleId) -> String,
) -> ScheduleReport {
    let sccs = tarjan_scc(&graph.graph);
    let mut cycles = Vec::new();
    for scc in sccs {
        let in_scc: HashSet<ModuleId> = scc.iter().copied().collect();
        let is_cycle =
            scc.len() > 1 || (scc.len() == 1 && graph.graph.contains_edge(scc[0], scc[0]));
        if !is_cycle {
            continue;
        }
        // Realizability filter (per DESIGN.md "The realizability
        // theorem"): an `I ∪ S` SCC is unrealizable iff at least
        // one cross-module edge between its members carries a
        // realizability-constraining reason — an at-init read
        // (`R`) or a side-effect ordering edge (`S`). Lazy reads
        // alone don't constrain it: the ESM linker evaluates the
        // SCC in *some* order, and the lazy reads only fire
        // afterwards (no TDZ, no missed side-effect ordering).
        let scc_constrains_evaluation_order = scc.iter().any(|&from| {
            scc.iter()
                .any(|&to| from != to && graph.has_realizability_constraining_edge(from, to))
        });
        if !scc_constrains_evaluation_order {
            continue;
        }
        let mut evidence = Vec::new();
        for (from, to, weight) in graph.iter_edges() {
            if !in_scc.contains(&from) || !in_scc.contains(&to) {
                continue;
            }
            for reason in &weight.reasons {
                evidence.push(CycleEdge {
                    from: module_name(from),
                    to: module_name(to),
                    statement_ordinal: reason.statement_ordinal,
                    binding: reason.binding.clone(),
                    kind: match reason.kind {
                        EdgeKind::AtInitRead => "at-init",
                        EdgeKind::LazyRead => "lazy",
                        EdgeKind::SideEffect => "side-effect",
                    },
                });
            }
        }
        let cut = compute_realizability_cut(graph, &scc, module_name);
        cycles.push(CycleReport {
            modules: scc.iter().copied().map(module_name).collect(),
            evidence,
            cut,
        });
    }
    ScheduleReport {
        kind: "js.schedule_validator_report",
        cycles,
        recommendations: Vec::new(),
        linker_order: Vec::new(),
    }
}

/// Compute a near-minimum cut of realizability-constraining edges
/// inside `scc` whose removal makes the SCC realizable.
///
/// Each iteration:
/// 1. Tarjan-SCC the working graph (initially the induced subgraph
///    on `scc` from `graph`).
/// 2. If no SCC of size ≥ 2 carries a cross-module
///    realizability-constraining edge, return the accumulated cut.
/// 3. Otherwise, pick the first such SCC, run
///    `petgraph::algo::greedy_feedback_arc_set` (Eades-Lin-Smyth)
///    on its induced subgraph, and pick the first FAS edge whose
///    metadata has an `AtInitRead` or `SideEffect` reason.
/// 4. Fall back to scanning the SCC's edges if the FAS only
///    yielded lazy edges (rare; happens when tie-breaking biases
///    the order toward picking lazy edges as back-edges).
/// 5. Append the picked edge's R/S reasons to the cut and remove
///    it from the working graph.
///
/// Termination: each iteration removes at least one R/S edge from
/// the working graph, and the count of R/S edges is finite.
/// Soundness: when the loop exits, every remaining SCC has only
/// lazy cross-module edges between members — realizable per the
/// DESIGN.md realizability theorem. Cuts are sorted
/// deterministically `(from, to, statement_ordinal, binding, kind)`
/// so test snapshots compare cleanly.
fn compute_realizability_cut(
    graph: &ModuleDepGraph,
    scc: &[ModuleId],
    module_name: &dyn Fn(ModuleId) -> String,
) -> Vec<CycleEdge> {
    if scc.len() < 2 {
        return Vec::new();
    }
    // Working copy: induced subgraph on `scc`. Edge weight is the
    // full `EdgeMetadata` so we can read reasons when adding to
    // the cut. Cloning is cheap — petgraph's `DiGraphMap` clone
    // is per-edge, and an SCC is at most a few thousand edges in
    // practice.
    let in_scc: HashSet<ModuleId> = scc.iter().copied().collect();
    let mut working = DiGraphMap::<ModuleId, EdgeMetadata>::new();
    for &m in scc {
        working.add_node(m);
    }
    for (from, to, weight) in graph.iter_edges() {
        if !in_scc.contains(&from) || !in_scc.contains(&to) || from == to {
            continue;
        }
        working.add_edge(from, to, weight.clone());
    }

    let mut cut: Vec<CycleEdge> = Vec::new();
    loop {
        let sub_sccs = tarjan_scc(&working);
        let problematic = sub_sccs.into_iter().find(|s| {
            if s.len() < 2 {
                return false;
            }
            let in_s: HashSet<ModuleId> = s.iter().copied().collect();
            s.iter().any(|&from| {
                working.edges(from).any(|(_, to, w)| {
                    from != to && in_s.contains(&to) && w.constrains_realizability()
                })
            })
        });
        let Some(s) = problematic else { break };
        let in_s: HashSet<ModuleId> = s.iter().copied().collect();

        // Induce a sub-SCC subgraph as an index-based `DiGraph`.
        // petgraph's `greedy_feedback_arc_set` requires
        // `NodeId: GraphIndex`, which `DiGraphMap`'s arbitrary key
        // type doesn't satisfy — `DiGraph` indexes nodes by
        // contiguous `NodeIndex`. Carry `ModuleId` as the node
        // weight so we can map FAS endpoints back.
        let mut induced: DiGraph<ModuleId, ()> = DiGraph::new();
        let mut idx_of: HashMap<ModuleId, _> = HashMap::new();
        for &m in &s {
            let ix = induced.add_node(m);
            idx_of.insert(m, ix);
        }
        for &from in &s {
            for (_, to, _) in working.edges(from) {
                if from != to && in_s.contains(&to) {
                    induced.add_edge(idx_of[&from], idx_of[&to], ());
                }
            }
        }
        let fas: Vec<(ModuleId, ModuleId)> = greedy_feedback_arc_set(&induced)
            .map(|e| (induced[e.source()], induced[e.target()]))
            .collect();

        // Prefer R/S FAS edges; fall back to scanning the sub-SCC
        // for any R/S edge if FAS only flagged lazy edges (rare).
        let pick_in_fas = fas.iter().copied().find(|&(u, v)| {
            working
                .edge_weight(u, v)
                .is_some_and(EdgeMetadata::constrains_realizability)
        });
        let pick = pick_in_fas.or_else(|| {
            for &from in &s {
                for (_, to, w) in working.edges(from) {
                    if from != to && in_s.contains(&to) && w.constrains_realizability() {
                        return Some((from, to));
                    }
                }
            }
            None
        });
        let Some((u, v)) = pick else {
            // Should be unreachable — `problematic` confirmed at
            // least one constraining cross-module edge in `s`.
            break;
        };

        let weight = working
            .remove_edge(u, v)
            .expect("edge picked from working graph just above");
        for reason in &weight.reasons {
            if matches!(reason.kind, EdgeKind::LazyRead) {
                continue;
            }
            cut.push(CycleEdge {
                from: module_name(u),
                to: module_name(v),
                statement_ordinal: reason.statement_ordinal,
                binding: reason.binding.clone(),
                kind: match reason.kind {
                    EdgeKind::AtInitRead => "at-init",
                    EdgeKind::SideEffect => "side-effect",
                    EdgeKind::LazyRead => unreachable!(),
                },
            });
        }
    }

    cut.sort_by(|a, b| {
        (
            a.from.as_str(),
            a.to.as_str(),
            a.statement_ordinal,
            &a.binding,
            a.kind,
        )
            .cmp(&(
                b.from.as_str(),
                b.to.as_str(),
                b.statement_ordinal,
                &b.binding,
                b.kind,
            ))
    });
    cut
}

/// Build the recommender side-output: for each binding declared in
/// the chunk but not owned by any logical module, list the modules
/// that read it, mark each candidate's `cycle_safe` flag.
///
/// Cycle-safety is checked by tentatively assigning the binding to
/// the candidate, rebuilding `I ∪ S` (via `build_module_dep_graph`),
/// and looking for non-trivial SCCs. Lazy-only candidates also need
/// this check, because lazy reads still emit `import` directives —
/// they still contribute to `I` — and the strict gating rule (see
/// DESIGN.md "The realizability theorem") rejects all `I ∪ S`
/// cycles, not just `R ∪ S` ones.
/// Project a `bindings` catalogue down to the `Owned` entries' owner
/// map — what the dep-graph builder, recommender and cycle-safety
/// check operate on. `Imported` bindings don't create at-init module
/// dep edges; their resolution is via the source chunk, not via
/// other logical modules.
fn owned_view(bindings: &BTreeMap<BindingName, BindingKind>) -> BTreeMap<BindingName, ModuleId> {
    bindings
        .iter()
        .filter_map(|(name, kind)| match kind {
            BindingKind::Owned { owner } => Some((name.clone(), *owner)),
            BindingKind::Imported { .. } => None,
        })
        .collect()
}

fn build_recommendations(schedule: &Schedule) -> Vec<AssignmentRecommendation> {
    let owned = owned_view(&schedule.bindings);
    let declared: BTreeSet<BindingName> = schedule
        .facts
        .iter()
        .flat_map(|f| f.declared.iter().cloned())
        .collect();

    let stmt_home = |stmt: &StatementFacts| -> ModuleId {
        stmt.declared
            .iter()
            .filter_map(|name| owned.get(name).copied())
            .next()
            .unwrap_or(ModuleId::ResidualEntry)
    };

    let mut recs = Vec::new();
    for name in &declared {
        if owned.contains_key(name) {
            continue;
        }
        let mut at_init_modules = BTreeSet::<ModuleId>::new();
        let mut any_modules = BTreeSet::<ModuleId>::new();
        for stmt in &schedule.facts {
            let home = stmt_home(stmt);
            if stmt.reads_at_init.contains(name) {
                at_init_modules.insert(home);
            }
            if stmt.reads_at_init.contains(name) || stmt.reads_lazy.contains(name) {
                any_modules.insert(home);
            }
        }
        // A reader from ResidualEntry doesn't introduce a meaningful
        // candidate — assigning the binding to ResidualEntry leaves
        // it where it already is.
        at_init_modules.remove(&ModuleId::ResidualEntry);
        any_modules.remove(&ModuleId::ResidualEntry);

        let mut candidates = Vec::new();
        for &m in &at_init_modules {
            let cycle_safe = is_assignment_cycle_safe(schedule, name, m);
            candidates.push(RecommendationCandidate {
                module: schedule.module_name(m),
                read_kind: RecommendationReadKind::AtInit,
                cycle_safe,
            });
        }
        for m in any_modules.difference(&at_init_modules) {
            // Lazy reads still emit `import` directives — they
            // contribute to `I` even when they're absent from `R`.
            // So lazy-only candidates need the same SCC check as
            // at-init ones; they are not free of cycle risk.
            let cycle_safe = is_assignment_cycle_safe(schedule, name, *m);
            candidates.push(RecommendationCandidate {
                module: schedule.module_name(*m),
                read_kind: RecommendationReadKind::LazyOnly,
                cycle_safe,
            });
        }
        if !candidates.is_empty() {
            recs.push(AssignmentRecommendation {
                binding: name.clone(),
                candidates,
            });
        }
    }
    recs
}

/// Tentatively assign `binding → candidate`, rebuild the dep graph,
/// and check that no realizability-violating cycle appears. Mirrors
/// `validate_schedule`'s gate: an SCC is unsafe iff it contains a
/// cross-module edge that constrains evaluation order — an at-init
/// read (`R`) or a side-effect ordering edge (`S`).
fn is_assignment_cycle_safe(
    schedule: &Schedule,
    binding: &BindingName,
    candidate: ModuleId,
) -> bool {
    let mut augmented = owned_view(&schedule.bindings);
    augmented.insert(binding.clone(), candidate);
    let graph = build_module_dep_graph(&schedule.facts, &augmented);
    let sccs = tarjan_scc(&graph.graph);
    !sccs.iter().any(|scc| {
        let is_cycle =
            scc.len() > 1 || (scc.len() == 1 && graph.graph.contains_edge(scc[0], scc[0]));
        if !is_cycle {
            return false;
        }
        scc.iter().any(|&from| {
            scc.iter()
                .any(|&to| from != to && graph.has_realizability_constraining_edge(from, to))
        })
    })
}

/// Topological linearization of the dep graph, dependency-first.
/// Empty if the graph has cycles (`tarjan_scc` plus the validator
/// gate handle that case).
///
/// The dep-graph edge convention is `(M, M')` meaning `M` depends
/// on `M'`. `petgraph::algo::toposort` returns `u`-before-`v` for
/// every edge `(u, v)`, which under our convention puts dependents
/// before dependencies. The returned order is reversed so the
/// dependency comes first — matching the order ECMA-262's link
/// traversal needs to evaluate (deepest leaf first).
fn compute_linker_order(
    dep_graph: &ModuleDepGraph,
    logical_modules: &[LogicalModule],
) -> Vec<ModuleId> {
    let mut graph = DiGraphMap::<ModuleId, ()>::new();
    // Add every module the schedule knows about so the order
    // covers them even if they have no dep-graph edges (singleton
    // leaves still need a deterministic position for emit ordering).
    graph.add_node(ModuleId::ResidualEntry);
    for idx in 0..logical_modules.len() {
        graph.add_node(ModuleId::Logical(LogicalModuleIndex(idx)));
    }
    for (from, to, _) in dep_graph.iter_edges() {
        graph.add_node(from);
        graph.add_node(to);
        graph.add_edge(from, to, ());
    }
    match toposort(&graph, None) {
        Ok(order) => order.into_iter().rev().collect(),
        Err(_) => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::{FileName, sync::Lrc};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    fn parse(source: &str) -> Module {
        let cm: Lrc<swc_common::SourceMap> = Default::default();
        let fm = cm.new_source_file(
            FileName::Custom("test.js".into()).into(),
            source.to_string(),
        );
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            Default::default(),
            StringInput::from(&*fm),
            None,
        );
        Parser::new_from(lexer).parse_module().unwrap()
    }

    #[test]
    fn function_body_reads_are_lazy() {
        let module = parse("function f() { return X; } const Y = 1;");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        assert_eq!(facts.len(), 2);
        // f() declares "f"; its body reference to X is lazy.
        assert_eq!(
            facts[0].declared,
            ["f"].iter().map(|s| s.to_string()).collect()
        );
        assert!(!facts[0].reads_at_init.contains("X"));
        assert_eq!(facts[0].kind, StatementKind::FnDecl);
        // Y declares "Y"; init is `1` (no reads).
        assert_eq!(
            facts[1].declared,
            ["Y"].iter().map(|s| s.to_string()).collect()
        );
        assert!(facts[1].reads_at_init.is_empty());
    }

    #[test]
    fn class_extends_clause_reads_at_init() {
        let module = parse("class B extends A { run() { return X; } }");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        assert_eq!(facts.len(), 1);
        // extends A is eager; method body reference to X is lazy.
        assert!(facts[0].reads_at_init.contains("A"));
        assert!(!facts[0].reads_at_init.contains("X"));
    }

    #[test]
    fn computed_key_reads_at_init() {
        let module = parse("const M = { [k.foo]: 1 };");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        // The key expression `k.foo` reads `k` at-init.
        assert!(facts[0].reads_at_init.contains("k"));
    }

    #[test]
    fn class_static_init_reads_at_init() {
        let module = parse("class C { static x = Y; }");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        assert!(facts[0].reads_at_init.contains("Y"));
    }

    #[test]
    fn class_instance_init_is_lazy() {
        let module = parse("class C { x = Y; }");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        // Instance field initializer evaluates per-instance, not at
        // class-decl time.
        assert!(!facts[0].reads_at_init.contains("Y"));
    }

    fn logical(idx: usize) -> ModuleId {
        ModuleId::Logical(LogicalModuleIndex(idx))
    }

    fn render(id: ModuleId) -> String {
        match id {
            ModuleId::Logical(LogicalModuleIndex(idx)) => format!("mod_{idx}"),
            ModuleId::ResidualEntry => "<residual>".to_string(),
        }
    }

    #[test]
    fn cycle_detected_between_two_modules() {
        // mod_a owns A; A's init reads B (owned by mod_b).
        // mod_b owns B; B's init reads A (owned by mod_a).
        let module = parse("const A = B + 1; const B = A + 1;");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = BTreeMap::new();
        binding_assignment.insert("A".to_string(), logical(0));
        binding_assignment.insert("B".to_string(), logical(1));
        let graph = build_module_dep_graph(&facts, &binding_assignment);
        let report = validate_schedule(&graph, &render);
        assert_eq!(report.cycles.len(), 1);
        assert_eq!(report.cycles[0].modules.len(), 2);
    }

    #[test]
    fn dag_has_no_cycles() {
        let module = parse("const A = 1; const B = A + 1; const C = B + A;");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = BTreeMap::new();
        binding_assignment.insert("A".to_string(), logical(0));
        binding_assignment.insert("B".to_string(), logical(1));
        binding_assignment.insert("C".to_string(), logical(2));
        let graph = build_module_dep_graph(&facts, &binding_assignment);
        let report = validate_schedule(&graph, &render);
        assert!(
            report.cycles.is_empty(),
            "expected no cycles, got {:?}",
            report.cycles
        );
    }

    /// Pin the cut behavior for the canonical mixed cycle: 2-module
    /// SCC with one lazy forward-edge and one at-init back-edge.
    /// The cut should contain exactly the at-init back-edge — lazy
    /// edges aren't realizability-constraining and removing one
    /// can't fix the cycle.
    #[test]
    fn cut_excludes_lazy_edges_in_mixed_cycle() {
        // mod_0 owns A and readB; readB body returns B (lazy read).
        // mod_1 owns B; B = A + 1 (at-init read of A).
        // R-edge: mod_1 → mod_0 (kind = at-init, binding = A).
        // L-edge: mod_0 → mod_1 (kind = lazy, binding = B).
        let module = parse("const A = 1; function readB() { return B; } const B = A + 1;");
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = BTreeMap::new();
        binding_assignment.insert("A".to_string(), logical(0));
        binding_assignment.insert("readB".to_string(), logical(0));
        binding_assignment.insert("B".to_string(), logical(1));
        let graph = build_module_dep_graph(&facts, &binding_assignment);
        let report = validate_schedule(&graph, &render);
        assert_eq!(
            report.cycles.len(),
            1,
            "expected one cycle, got {:?}",
            report.cycles,
        );
        let cycle = &report.cycles[0];
        assert!(
            cycle.evidence.iter().any(|e| e.kind == "lazy"),
            "evidence should include the lazy edge, got {:?}",
            cycle.evidence,
        );
        assert!(
            !cycle.cut.iter().any(|e| e.kind == "lazy"),
            "cut must not include lazy reasons, got {:?}",
            cycle.cut,
        );
        assert_eq!(
            cycle.cut.len(),
            1,
            "min cut for a single mixed cycle is one edge, got {:?}",
            cycle.cut,
        );
        let entry = &cycle.cut[0];
        assert_eq!(entry.from, "mod_1");
        assert_eq!(entry.to, "mod_0");
        assert_eq!(entry.binding, "A");
        assert_eq!(entry.kind, "at-init");
    }

    /// Pure-S cycle: cut consists of side-effect reasons; no
    /// lazy or at-init reasons should appear.
    #[test]
    fn cut_emits_side_effect_edges_for_s_only_cycle() {
        // Three side-effecting `globalThis.tag = ...` writes
        // interleaved across mod_0 (ord 0, 2) and mod_1 (ord 1).
        // S-edges: mod_0 → mod_1 (ord 0 < ord 1) and
        // mod_1 → mod_0 (ord 1 < ord 2). Cycle.
        let module = parse(
            r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
        );
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = BTreeMap::new();
        binding_assignment.insert("a1".to_string(), logical(0));
        binding_assignment.insert("a2".to_string(), logical(0));
        binding_assignment.insert("b1".to_string(), logical(1));
        let graph = build_module_dep_graph(&facts, &binding_assignment);
        let report = validate_schedule(&graph, &render);
        assert_eq!(report.cycles.len(), 1);
        let cycle = &report.cycles[0];
        assert!(
            !cycle.cut.is_empty(),
            "cut should be non-empty for an unrealizable cycle, got {:?}",
            cycle.cut,
        );
        assert!(
            cycle.cut.iter().all(|e| e.kind == "side-effect"),
            "S-only cycle cut should be all side-effect reasons, got {:?}",
            cycle.cut,
        );
    }

    /// Lazy-only cycle: realizability gate accepts it, so no
    /// CycleReport is emitted and there's no cut to compute.
    #[test]
    fn cut_is_absent_for_lazy_only_cycle() {
        // mod_0 owns helperA, A; mod_1 owns helperB, B. Both
        // helpers reference the other module's binding lazily;
        // no cross-module at-init or side-effect edges.
        let module = parse(
            "function helperA() { return B; } function helperB() { return A; } const A = 1; const B = 2;",
        );
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut binding_assignment = BTreeMap::new();
        binding_assignment.insert("helperA".to_string(), logical(0));
        binding_assignment.insert("A".to_string(), logical(0));
        binding_assignment.insert("helperB".to_string(), logical(1));
        binding_assignment.insert("B".to_string(), logical(1));
        let graph = build_module_dep_graph(&facts, &binding_assignment);
        let report = validate_schedule(&graph, &render);
        assert!(
            report.cycles.is_empty(),
            "lazy-only cycle is realizable; the gate must accept and emit no cycle (got {:?})",
            report.cycles,
        );
    }

    fn schedule_for(source: &str, ownership: &[(&str, ModuleId)]) -> Schedule {
        let module = parse(source);
        let facts = analyze_chunk_facts(&module, &BTreeSet::new());
        let mut bindings = BTreeMap::new();
        let mut max_idx = 0usize;
        for (name, id) in ownership {
            bindings.insert(name.to_string(), BindingKind::Owned { owner: *id });
            if let ModuleId::Logical(LogicalModuleIndex(i)) = id {
                max_idx = max_idx.max(*i);
            }
        }
        let logical_modules: Vec<LogicalModule> = (0..=max_idx)
            .map(|i| LogicalModule {
                id: format!("mod_{i}"),
                target_file: format!("mod_{i}.js"),
                rename_map: BTreeMap::new(),
            })
            .collect();
        Schedule::build("test_chunk".to_string(), facts, bindings, logical_modules)
    }

    #[test]
    fn lazy_only_read_yields_lazy_only_candidate() {
        // mod_0 owns helper; helper's body lazily reads X. X is unowned.
        let schedule = schedule_for(
            "function helper() { return X; } const X = 42;",
            &[("helper", logical(0))],
        );
        let report = schedule.validate();
        let rec = report
            .recommendations
            .iter()
            .find(|r| r.binding == "X")
            .expect("expected a recommendation for X");
        assert_eq!(rec.candidates.len(), 1);
        assert_eq!(rec.candidates[0].module, "mod_0");
        assert_eq!(
            rec.candidates[0].read_kind,
            RecommendationReadKind::LazyOnly
        );
        assert!(
            rec.candidates[0].cycle_safe,
            "lazy-only candidates are always cycle-safe"
        );
    }

    #[test]
    fn at_init_read_acyclic_is_cycle_safe() {
        // mod_0 owns A; A reads X at-init. mod_1 owns B; B reads A. X
        // is unowned. Assigning X → mod_0 is cycle-safe.
        let schedule = schedule_for(
            "const A = X + 1; const B = A + 1; const X = 42;",
            &[("A", logical(0)), ("B", logical(1))],
        );
        let report = schedule.validate();
        let rec = report
            .recommendations
            .iter()
            .find(|r| r.binding == "X")
            .expect("expected a recommendation for X");
        let mod_0 = rec
            .candidates
            .iter()
            .find(|c| c.module == "mod_0")
            .expect("mod_0 should be an at-init candidate (it reads X)");
        assert_eq!(mod_0.read_kind, RecommendationReadKind::AtInit);
        assert!(mod_0.cycle_safe, "X → mod_0 should be cycle-safe");
    }

    #[test]
    fn at_init_read_creating_cycle_is_not_cycle_safe() {
        // mod_0 owns A; A reads X at-init. mod_1 owns B; B reads A.
        // X has its own statement reading B at-init. Assigning X →
        // mod_0 creates the cycle mod_0 ↔ mod_1 (X's body would now
        // run from mod_0 and read mod_1's B).
        let schedule = schedule_for(
            "const A = X + 1; const B = A + 1; const X = B + 1;",
            &[("A", logical(0)), ("B", logical(1))],
        );
        let report = schedule.validate();
        let rec = report
            .recommendations
            .iter()
            .find(|r| r.binding == "X")
            .expect("expected a recommendation for X");
        let mod_0 = rec
            .candidates
            .iter()
            .find(|c| c.module == "mod_0")
            .expect("mod_0 reads X so should appear");
        assert_eq!(mod_0.read_kind, RecommendationReadKind::AtInit);
        assert!(
            !mod_0.cycle_safe,
            "X → mod_0 closes a cycle and must be flagged"
        );
    }

    #[test]
    fn owned_bindings_get_no_recommendation() {
        // Every binding has an explicit owner.
        let schedule = schedule_for(
            "const A = 1; const B = A + 1;",
            &[("A", logical(0)), ("B", logical(1))],
        );
        let report = schedule.validate();
        assert!(
            report.recommendations.is_empty(),
            "fully-explicit spec should produce no recommendations, got {:?}",
            report.recommendations
        );
    }

    // --- Purity classifier ---------------------------------------------------

    fn classify(src: &str) -> Purity {
        // Wrap the expression in a const so we can parse a module.
        let module = parse(&format!("const _ = {src};"));
        let var = match &module.body[0] {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
            other => panic!("expected `const _ = ...;`, got {other:?}"),
        };
        let init = var.decls[0].init.as_deref().expect("init expected");
        classify_expr_purity(
            init,
            &BTreeSet::new(),
            &BTreeSet::new(),
            &ChunkCodeGraph::default(),
        )
    }

    /// Run the classifier against `src` after computing the
    /// chunk-top-level shadowed-globals set from a wrapping
    /// module. Lets tests check the shadowing fallback.
    fn classify_with_module(prefix: &str, expr_src: &str) -> Purity {
        let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
        let shadowed = compute_shadowed_globals(&module.body);
        let var = match module.body.last().expect("non-empty body") {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
            other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
        };
        let init = var.decls[0].init.as_deref().expect("init expected");
        classify_expr_purity(
            init,
            &shadowed,
            &BTreeSet::new(),
            &ChunkCodeGraph::default(),
        )
    }

    /// Run the classifier against `src` with both shadowing and an
    /// explicit declared-pure binding set.
    fn classify_with_declared_pure(prefix: &str, expr_src: &str, declared: &[&str]) -> Purity {
        let module = parse(&format!("{prefix}\nconst _ = {expr_src};"));
        let shadowed = compute_shadowed_globals(&module.body);
        let declared_pure: BTreeSet<String> = declared.iter().map(|s| (*s).to_string()).collect();
        let var = match module.body.last().expect("non-empty body") {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
            other => panic!("expected last stmt to be `const _ = …;`, got {other:?}"),
        };
        let init = var.decls[0].init.as_deref().expect("init expected");
        classify_expr_purity(init, &shadowed, &declared_pure, &ChunkCodeGraph::default())
    }

    #[test]
    fn classify_literal_kinds_are_pure() {
        assert_eq!(classify("42"), Purity::Pure);
        assert_eq!(classify("\"hi\""), Purity::Pure);
        assert_eq!(classify("true"), Purity::Pure);
        assert_eq!(classify("null"), Purity::Pure);
        assert_eq!(classify("/foo/g"), Purity::Pure);
        assert_eq!(classify("`literal`"), Purity::Pure);
    }

    #[test]
    fn classify_ident_read_is_pure() {
        assert_eq!(classify("FOO"), Purity::Pure);
    }

    #[test]
    fn classify_pure_unary_and_binary() {
        assert_eq!(classify("-1"), Purity::Pure);
        assert_eq!(classify("!FOO"), Purity::Pure);
        assert_eq!(classify("typeof FOO"), Purity::Pure);
        assert_eq!(classify("A + 1"), Purity::Pure);
        assert_eq!(classify("A && B"), Purity::Pure);
        assert_eq!(classify("A ? B : C"), Purity::Pure);
    }

    #[test]
    fn classify_delete_is_impure() {
        assert_eq!(classify("delete o.x"), Purity::Impure);
    }

    #[test]
    fn classify_assignment_and_update_are_impure() {
        assert_eq!(classify("(x = 1)"), Purity::Impure);
        assert_eq!(classify("x++"), Purity::Impure);
    }

    #[test]
    fn classify_call_new_tagged_template_are_unknown() {
        assert_eq!(classify("foo()"), Purity::Unknown);
        assert_eq!(classify("new Foo()"), Purity::Unknown);
        assert_eq!(classify("tag`hi ${x}`"), Purity::Unknown);
    }

    #[test]
    fn classify_member_access_is_unknown() {
        assert_eq!(classify("o.x"), Purity::Unknown);
        assert_eq!(classify("o[k]"), Purity::Unknown);
        assert_eq!(classify("o?.x"), Purity::Unknown);
    }

    #[test]
    fn classify_object_literal_pure_when_props_pure() {
        assert_eq!(classify("({ a: 1, b: 'x' })"), Purity::Pure);
        assert_eq!(classify("({ [k]: 1 })"), Purity::Pure);
        // Computed key with member access — getter could fire.
        assert_eq!(classify("({ [k.x]: 1 })"), Purity::Unknown);
        // Spread of an arbitrary expr — iterator could fire.
        assert_eq!(classify("({ ...other })"), Purity::Unknown);
        // Method definitions are pure (defining, not calling).
        assert_eq!(classify("({ m() { return io(); } })"), Purity::Pure);
    }

    #[test]
    fn classify_array_literal_pure_when_elements_pure() {
        assert_eq!(classify("[1, 2, 'x']"), Purity::Pure);
        assert_eq!(classify("[A, B]"), Purity::Pure);
        assert_eq!(classify("[1, foo()]"), Purity::Unknown);
        // Spread is `Unknown` even on an array literal.
        assert_eq!(classify("[...other]"), Purity::Unknown);
    }

    #[test]
    fn classify_function_and_arrow_are_pure() {
        assert_eq!(classify("function () { return io(); }"), Purity::Pure);
        assert_eq!(classify("() => io()"), Purity::Pure);
    }

    #[test]
    fn classify_class_expr_pure_without_static_init() {
        assert_eq!(classify("class { m() { return io(); } }"), Purity::Pure);
        assert_eq!(classify("class { static x = 1 }"), Purity::Pure);
        assert_eq!(classify("class { static x = io() }"), Purity::Impure);
        assert_eq!(classify("class { static {} }"), Purity::Impure);
    }

    #[test]
    fn classify_template_with_pure_exprs_is_pure() {
        assert_eq!(classify("`a${A}b${1+2}c`"), Purity::Pure);
        assert_eq!(classify("`a${foo()}`"), Purity::Unknown);
    }

    #[test]
    fn classify_sequence_takes_worst() {
        assert_eq!(classify("(A, B, C)"), Purity::Pure);
        assert_eq!(classify("(A, foo(), C)"), Purity::Unknown);
        assert_eq!(classify("(A, x = 1, C)"), Purity::Impure);
    }

    // --- Whitelist: pure static property reads -------------------------------

    #[test]
    fn whitelist_static_props_are_pure() {
        // Math / Number / Symbol constants: pure internal-slot
        // reads, no coercion.
        assert_eq!(classify("Math.PI"), Purity::Pure);
        assert_eq!(classify("Math.E"), Purity::Pure);
        assert_eq!(classify("Math.SQRT2"), Purity::Pure);
        assert_eq!(classify("Number.EPSILON"), Purity::Pure);
        assert_eq!(classify("Number.MAX_SAFE_INTEGER"), Purity::Pure);
        assert_eq!(classify("Symbol.iterator"), Purity::Pure);
        assert_eq!(classify("Symbol.toStringTag"), Purity::Pure);
    }

    #[test]
    fn whitelist_misses_fall_back_to_unknown() {
        // Same receivers, properties that aren't on the whitelist:
        // could be a getter / a coercing call. Stays Unknown.
        assert_eq!(classify("Math.unknownProp"), Purity::Unknown);
        assert_eq!(classify("Number.unknownProp"), Purity::Unknown);
        assert_eq!(classify("Symbol.unknownProp"), Purity::Unknown);
    }

    // --- Whitelist: pure calls -----------------------------------------------

    #[test]
    fn whitelist_static_calls_are_pure_regardless_of_arg() {
        // Type predicates do not coerce or read user props on the
        // argument, so any Pure-classified arg keeps the call Pure.
        assert_eq!(classify("Array.isArray(x)"), Purity::Pure);
        assert_eq!(classify("Array.isArray([1, 2, 3])"), Purity::Pure);
        assert_eq!(classify("Number.isNaN(x)"), Purity::Pure);
        assert_eq!(classify("Number.isFinite(x)"), Purity::Pure);
        assert_eq!(classify("Number.isInteger(x)"), Purity::Pure);
        assert_eq!(classify("Number.isSafeInteger(x)"), Purity::Pure);
    }

    #[test]
    fn whitelist_static_calls_unknown_arg_infects() {
        // An argument whose evaluation may itself fire user code
        // poisons the whole call: even though `Array.isArray` is
        // a pure operation, evaluating `io()` first is not.
        assert_eq!(classify("Array.isArray(io())"), Purity::Unknown);
        assert_eq!(classify("Number.isNaN(o.x)"), Purity::Unknown);
    }

    // --- PURE_STATIC_FUNCTION_REFS: read-vs-call distinction ---------------

    #[test]
    fn static_function_ref_object_aliases_are_pure() {
        // Bare member READS access own data properties of the
        // built-in `Object` per ECMA-262 §20.1.2 — no getter
        // fires, no observable side effect. Aliasing the function
        // value into a binding stays pure (the value isn't called).
        assert_eq!(classify("Object.defineProperty"), Purity::Pure);
        assert_eq!(classify("Object.freeze"), Purity::Pure);
        assert_eq!(classify("Object.values"), Purity::Pure);
        assert_eq!(classify("Object.keys"), Purity::Pure);
    }

    #[test]
    fn static_function_ref_object_calls_remain_unknown() {
        // The CALL form of each function-ref entry is unsafe (see
        // `PURE_STATIC_FUNCTION_REFS` doc-comment for why each is
        // excluded from `PURE_STATIC_CALLS`). The function-ref
        // entry only opens the read path; the call must stay
        // Unknown so the soundness contract holds.
        assert_eq!(
            classify("Object.defineProperty(t, 'k', { value: 1 })"),
            Purity::Unknown
        );
        assert_eq!(classify("Object.freeze({ x: 1 })"), Purity::Unknown);
        assert_eq!(classify("Object.values(o)"), Purity::Unknown);
        assert_eq!(classify("Object.keys(o)"), Purity::Unknown);
    }

    #[test]
    fn static_function_ref_object_shadowed_falls_back_to_unknown() {
        // `Object` joins WHITELIST_RECEIVERS in this PR; if the
        // chunk shadows it (via a top-level decl OR an import
        // specifier per A8), the function-ref read must fall back
        // to Unknown — `Object.X` then resolves through the
        // user-bound value.
        assert_eq!(
            classify_with_module("const Object = userland;", "Object.defineProperty"),
            Purity::Unknown
        );
        assert_eq!(
            classify_with_module(
                r#"import { Object } from "./userland.js";"#,
                "Object.freeze"
            ),
            Purity::Unknown
        );
    }

    #[test]
    fn whitelist_global_callables_are_pure() {
        // Boolean(x) is `ToBoolean(x)`; per spec, no path fires
        // user code (objects → true unconditionally; primitives
        // are case-analysed structurally).
        assert_eq!(classify("Boolean(x)"), Purity::Pure);
        assert_eq!(classify("Boolean(0)"), Purity::Pure);
        assert_eq!(classify("Boolean({})"), Purity::Pure);
    }

    #[test]
    fn unsafe_global_callables_stay_unknown() {
        // ToNumber / ToString / ToPrimitive can call user
        // `valueOf` / `toString` / `[Symbol.toPrimitive]` on
        // object args; we don't track types, so these remain
        // Unknown to keep the whitelist sound.
        assert_eq!(classify("Number(x)"), Purity::Unknown);
        assert_eq!(classify("String(x)"), Purity::Unknown);
        assert_eq!(classify("Symbol(x)"), Purity::Unknown);
        assert_eq!(classify("parseInt(x, 10)"), Purity::Unknown);
        assert_eq!(classify("parseFloat(x)"), Purity::Unknown);
        assert_eq!(classify("isNaN(x)"), Purity::Unknown);
        assert_eq!(classify("isFinite(x)"), Purity::Unknown);
    }

    #[test]
    fn unsafe_static_calls_stay_unknown() {
        // Anything that coerces / iterates / fires getters /
        // mutates / reads through proxies is *not* on the
        // whitelist. These all stay Unknown.
        for src in [
            "Array.from(x)",
            "Array.of(1, 2, 3)",
            "Math.abs(x)",
            "Math.max(1, 2)",
            "Math.floor(x)",
            "Math.round(x)",
            "Math.sqrt(x)",
            "Object.keys(x)",
            "Object.values(x)",
            "Object.entries(x)",
            "Object.freeze(x)",
            "Object.assign({}, x)",
            "Object.fromEntries(x)",
            "Object.getOwnPropertyDescriptor(x, 'k')",
            "Object.hasOwn(x, 'k')",
            "JSON.parse(x)",
            "JSON.stringify(x)",
            "Number.parseInt(x)",
            "Number.parseFloat(x)",
            "String.fromCharCode(65)",
            "String.fromCodePoint(65)",
            "Symbol.for('k')",
            "Symbol.keyFor(s)",
        ] {
            assert_eq!(
                classify(src),
                Purity::Unknown,
                "expected {src} to stay Unknown (would fire user code)"
            );
        }
    }

    // --- Whitelist: shadowing fallback ---------------------------------------

    #[test]
    fn shadowed_receiver_disables_whitelist() {
        // A chunk-top-level binding for `Math` makes `Math.PI` no
        // longer reach the global; the whitelist must fall back
        // to Unknown.
        assert_eq!(
            classify_with_module("const Math = userland;", "Math.PI"),
            Purity::Unknown
        );
        assert_eq!(
            classify_with_module("function Math() {}", "Math.E"),
            Purity::Unknown
        );
        assert_eq!(
            classify_with_module("const Array = X;", "Array.isArray(x)"),
            Purity::Unknown
        );
        assert_eq!(
            classify_with_module("let Number = X;", "Number.isNaN(x)"),
            Purity::Unknown
        );
        assert_eq!(
            classify_with_module("const Boolean = X;", "Boolean(x)"),
            Purity::Unknown
        );
    }

    #[test]
    fn unshadowed_receiver_keeps_whitelist() {
        // A chunk that declares an unrelated binding leaves the
        // whitelist active — only same-named shadowing disables.
        assert_eq!(
            classify_with_module("const other = userland;", "Math.PI"),
            Purity::Pure
        );
    }

    #[test]
    fn import_specifier_locals_shadow_whitelist() {
        // Import bindings are top-level lexical decls and shadow
        // the global the same way `const Math = …` does. The
        // classifier must reach the same Unknown fallback. (Soundness
        // matters: the imported value can be anything, so
        // `<imported>.<prop>` is a property read that may fire a
        // user-defined getter.)
        assert_eq!(
            classify_with_module(r#"import { Math } from "./userland.js";"#, "Math.PI"),
            Purity::Unknown
        );
        assert_eq!(
            classify_with_module(r#"import Boolean from "./userland.js";"#, "Boolean(x)"),
            Purity::Unknown
        );
        assert_eq!(
            classify_with_module(
                r#"import * as Number from "./userland.js";"#,
                "Number.isNaN(x)"
            ),
            Purity::Unknown
        );
        assert_eq!(
            classify_with_module(
                r#"import { something as Array } from "./userland.js";"#,
                "Array.isArray(x)"
            ),
            Purity::Unknown
        );
    }

    // --- Declared purity (spec annotation) ---------------------------------

    #[test]
    fn declared_pure_ident_call_classifies_pure() {
        // A spec member with `purity: "pure"` populates the
        // declared-pure set. A call whose callee is the bound
        // Ident classifies Pure regardless of the body content
        // (the validator does not re-verify; author trust). Args
        // are still evaluated normally — pure args here, so the
        // whole call is Pure.
        assert_eq!(
            classify_with_declared_pure("function f(x) { return x; }", "f(42)", &["f"]),
            Purity::Pure
        );
        assert_eq!(
            classify_with_declared_pure("function f(x) { return x; }", "f({ k: 'v' })", &["f"]),
            Purity::Pure
        );
    }

    #[test]
    fn declared_pure_call_with_impure_arg_inherits_arg_purity() {
        // The declared-purity contract covers the function value;
        // arg evaluation is independent. An impure arg makes the
        // whole call Unknown.
        assert_eq!(
            classify_with_declared_pure(
                "function f(x) { return x; } function io() { return 1; }",
                "f(io())",
                &["f"]
            ),
            Purity::Unknown
        );
    }

    #[test]
    fn declared_pure_overrides_global_shadowing() {
        // Author trust contract: a declared-pure annotation wins
        // over both the whitelist's shadowing fallback and the
        // body's actual contents. The validator does not
        // second-guess.
        assert_eq!(
            classify_with_declared_pure(
                r#"import { Boolean } from "./userland.js";"#,
                "Boolean(x)",
                &["Boolean"]
            ),
            Purity::Pure
        );
    }

    #[test]
    fn declared_pure_does_not_bleed_to_unannotated_callees() {
        // Only the listed binding is treated pure. A call to a
        // sibling that wasn't annotated stays subject to the
        // normal classifier path (Unknown for opaque idents).
        assert_eq!(
            classify_with_declared_pure(
                "function pure(x) { return x; } function impure(x) { return x; }",
                "impure(x)",
                &["pure"]
            ),
            Purity::Unknown
        );
    }

    // --- ChunkCodeGraph: function-body purity inference --------------------

    /// Build a `ChunkCodeGraph` for `src` and return the purity it
    /// computed for the named function. Tests the full pipeline:
    /// chunk parsing → function collection → fixed-point.
    fn fn_purity(src: &str, name: &str) -> Option<Purity> {
        let module = parse(src);
        let body = split_comma_list_var_decls(&module.body);
        let shadowed = compute_shadowed_globals(&body);
        let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
        graph.function_purity(name)
    }

    #[test]
    fn fn_purity_pure_hof_wrapper() {
        // Body returns a fresh object literal whose values are a
        // bound parameter — no observable side effect.
        assert_eq!(
            fn_purity(
                r#"function wrap(f) { return { kind: "wrapped", impl: f }; }"#,
                "wrap"
            ),
            Some(Purity::Pure)
        );
    }

    #[test]
    fn fn_purity_impure_globalthis_write() {
        // Assignment to a member of `globalThis` is unambiguously
        // impure regardless of what's on the RHS.
        assert_eq!(
            fn_purity("function tag(x) { globalThis.tag = x; }", "tag"),
            Some(Purity::Impure)
        );
    }

    #[test]
    fn fn_purity_unknown_when_calling_console_log() {
        // `console.log(...)` is a member-call on a non-whitelisted
        // receiver — Unknown. Caller inherits.
        assert_eq!(
            fn_purity(
                r#"function logged(x) { console.log("init", x); return x; }"#,
                "logged"
            ),
            Some(Purity::Unknown)
        );
    }

    #[test]
    fn fn_purity_propagates_transitive_impurity() {
        // `caller` only calls `tainted`. `tainted` writes
        // `globalThis.touched`, so it's Impure. Fixed-point
        // propagates: `caller` becomes Impure on iteration 2.
        let src = r#"
            function tainted() { globalThis.touched = true; return 1; }
            function caller() { return tainted(); }
        "#;
        assert_eq!(fn_purity(src, "tainted"), Some(Purity::Impure));
        assert_eq!(fn_purity(src, "caller"), Some(Purity::Impure));
    }

    #[test]
    fn fn_purity_mutual_recursion_converges_pure() {
        // `even` and `odd` only reference each other inside their
        // bodies. Optimistic init (Pure) holds through the
        // fixed-point — neither body has an impure operation.
        let src = r#"
            function even(n) { return n === 0 ? true : odd(n - 1); }
            function odd(n) { return n === 0 ? false : even(n - 1); }
        "#;
        assert_eq!(fn_purity(src, "even"), Some(Purity::Pure));
        assert_eq!(fn_purity(src, "odd"), Some(Purity::Pure));
    }

    #[test]
    fn fn_purity_arrow_const_init() {
        // `const f = (x) => …` — chunk-top function in a VarDecl
        // initializer. Concise-arrow body classifies the single
        // return expression.
        assert_eq!(
            fn_purity("const wrap = (x) => ({ val: x });", "wrap"),
            Some(Purity::Pure)
        );
    }

    #[test]
    fn fn_purity_call_inherits_chunk_local_function_purity() {
        // `f()` where `f` is a chunk-top function in the cache
        // resolves through `ChunkCodeGraph::function_purity`. With
        // `f` body Pure, the call is Pure.
        let module = parse("function f() { return 42; } const x = f();");
        let body = split_comma_list_var_decls(&module.body);
        let shadowed = compute_shadowed_globals(&body);
        let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
        let var = match &body[1] {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var,
            other => panic!("expected VarDecl, got {other:?}"),
        };
        let init = var.decls[0].init.as_deref().expect("init");
        assert_eq!(
            classify_expr_purity(init, &shadowed, &BTreeSet::new(), &graph),
            Purity::Pure
        );
    }

    #[test]
    fn fn_purity_let_var_bound_arrows_are_not_cached() {
        // `let` and `var` bindings are reassignable. Caching their
        // body's purity would be unsound: a later `f = …` could
        // replace the value with something impure between graph
        // construction and the call site. Restrict graph entries
        // to `const`-bound function/arrow initializers.
        assert_eq!(
            fn_purity("let f = () => 1;", "f"),
            None,
            "`let`-bound arrow must not be in the function-purity graph"
        );
        assert_eq!(
            fn_purity("var f = function () { return 1; };", "f"),
            None,
            "`var`-bound function expr must not be in the function-purity graph"
        );
        // Sanity: `const` still works.
        assert_eq!(fn_purity("const f = () => 1;", "f"), Some(Purity::Pure));
    }

    #[test]
    fn fn_purity_throw_makes_function_impure_even_with_pure_arg() {
        // `throw e` alters control flow observably regardless of
        // whether `e` itself is pure. A function that always
        // throws must not classify as Pure.
        assert_eq!(
            fn_purity(r#"function f() { throw "boom"; }"#, "f"),
            Some(Purity::Impure)
        );
        // Conditional throw is still Impure (we don't reason
        // about reachability — soundness-first).
        assert_eq!(
            fn_purity(r#"function f(x) { if (x) throw "boom"; return x; }"#, "f"),
            Some(Purity::Impure)
        );
    }

    #[test]
    fn fn_purity_debugger_makes_function_impure() {
        // `debugger` pauses execution observably to a host
        // attached to the process — not Pure.
        assert_eq!(
            fn_purity("function f() { debugger; return 1; }", "f"),
            Some(Purity::Impure)
        );
    }

    // --- Call-graph topology: deep chains, isolated nodes ------------------

    #[test]
    fn fn_purity_deep_pure_chain_propagates_in_one_pass() {
        // `a → b → c → d → e`: a long chain of chunk-local calls,
        // each function pure on its own. SCC bottom-up classifies
        // `e` first (no callees), then `d`, ..., then `a` — each
        // function classified once. With the previous global
        // fixed-point this would still terminate but rewalk every
        // body each pass; with SCC-bottom-up each is touched once.
        let src = r#"
            function e() { return 0; }
            function d() { return e(); }
            function c() { return d(); }
            function b() { return c(); }
            function a() { return b(); }
        "#;
        for name in ["a", "b", "c", "d", "e"] {
            assert_eq!(
                fn_purity(src, name),
                Some(Purity::Pure),
                "expected {name} to classify Pure"
            );
        }
    }

    #[test]
    fn fn_purity_deep_chain_propagates_impurity_to_root() {
        // Same shape but `e` writes `globalThis`. SCC processes
        // `e` first → Impure; the worklist propagates Impure up
        // the chain (`d` calls `e` → Impure; `c` calls `d` →
        // Impure; ...; `a` → Impure). Each function still only
        // re-classified after a callee changes — bounded total
        // work even on long chains.
        let src = r#"
            function e() { globalThis.touched = true; return 0; }
            function d() { return e(); }
            function c() { return d(); }
            function b() { return c(); }
            function a() { return b(); }
        "#;
        for name in ["a", "b", "c", "d", "e"] {
            assert_eq!(
                fn_purity(src, name),
                Some(Purity::Impure),
                "expected {name} to inherit Impure from `e`"
            );
        }
    }

    #[test]
    fn fn_purity_independent_functions_isolated_in_call_graph() {
        // No edges between `a` / `b` / `c`. Each is its own SCC;
        // classification of each is independent. `a` Impure must
        // not affect `b` or `c`.
        let src = r#"
            function a() { globalThis.touched = true; }
            function b() { return 1; }
            function c() { return 2; }
        "#;
        assert_eq!(fn_purity(src, "a"), Some(Purity::Impure));
        assert_eq!(fn_purity(src, "b"), Some(Purity::Pure));
        assert_eq!(fn_purity(src, "c"), Some(Purity::Pure));
    }

    #[test]
    fn fn_purity_mutual_recursion_with_external_impure_callee() {
        // Mutual recursion `a <-> b` (one SCC) + `a` also calls
        // `c` (separate SCC, Impure). `c` is processed first
        // (sink); `c` Impure. SCC {a, b}: optimistic Pure init,
        // worklist sees `a` calls `c` (Impure) → `a` becomes
        // Impure → `b` (which calls `a`) gets pushed to worklist
        // → `b` becomes Impure.
        let src = r#"
            function c() { globalThis.touched = true; return 0; }
            function a(n) { return n === 0 ? c() : b(n - 1); }
            function b(n) { return n === 0 ? 0 : a(n - 1); }
        "#;
        assert_eq!(fn_purity(src, "c"), Some(Purity::Impure));
        assert_eq!(fn_purity(src, "a"), Some(Purity::Impure));
        assert_eq!(fn_purity(src, "b"), Some(Purity::Impure));
    }

    // --- has_side_effect refinement ------------------------------------------

    fn has_side_effect_for(src: &str) -> Vec<bool> {
        let module = parse(src);
        analyze_chunk_facts(&module, &BTreeSet::new())
            .into_iter()
            .map(|f| f.has_side_effect)
            .collect()
    }

    #[test]
    fn pure_const_decl_is_not_side_effecting() {
        assert_eq!(has_side_effect_for("const X = 42;"), vec![false]);
        assert_eq!(has_side_effect_for("const X = { a: 1 };"), vec![false]);
        assert_eq!(has_side_effect_for("const X = [1, 2, 3];"), vec![false]);
        assert_eq!(has_side_effect_for("const X = OTHER;"), vec![false]);
        assert_eq!(has_side_effect_for("const X = A + B;"), vec![false]);
    }

    #[test]
    fn impure_const_decl_is_side_effecting() {
        assert_eq!(has_side_effect_for("const X = compute();"), vec![true]);
        assert_eq!(has_side_effect_for("const X = new Foo();"), vec![true]);
        assert_eq!(has_side_effect_for("const X = (y = 1, y);"), vec![true]);
    }

    #[test]
    fn function_decl_is_not_side_effecting() {
        assert_eq!(
            has_side_effect_for("function f() { return io(); }"),
            vec![false]
        );
    }

    #[test]
    fn class_decl_pure_without_static_init() {
        assert_eq!(
            has_side_effect_for("class C { m() { return io(); } }"),
            vec![false]
        );
        assert_eq!(
            has_side_effect_for("class C { static x = 1; }"),
            vec![false]
        );
        assert_eq!(
            has_side_effect_for("class C { static x = io(); }"),
            vec![true]
        );
        assert_eq!(has_side_effect_for("class C { static {} }"), vec![true]);
    }

    #[test]
    fn bare_expression_classified_by_purity() {
        // Plain ident-read expression statement: pure.
        assert_eq!(has_side_effect_for("X;"), vec![false]);
        // Function call expression statement: side-effecting.
        assert_eq!(has_side_effect_for("io();"), vec![true]);
    }

    #[test]
    fn multi_declarator_var_decl_is_side_effecting_if_any_init_is() {
        // After the comma-list pre-split, a multi-declarator
        // var-decl becomes one row per declarator. So a
        // mixed-purity comma-list produces both a Pure row and
        // an Impure row, not a single conservative row.
        assert_eq!(
            has_side_effect_for("const A = 1, B = compute();"),
            vec![false, true]
        );
        assert_eq!(
            has_side_effect_for("const A = 1, B = 2, C = 3;"),
            vec![false, false, false]
        );
    }

    // --- Comma-list splitter -------------------------------------------------

    fn statement_kinds(source: &str) -> Vec<StatementKind> {
        let module = parse(source);
        analyze_chunk_facts(&module, &BTreeSet::new())
            .into_iter()
            .map(|f| f.kind)
            .collect()
    }

    fn declared_per_statement(source: &str) -> Vec<Vec<String>> {
        let module = parse(source);
        analyze_chunk_facts(&module, &BTreeSet::new())
            .into_iter()
            .map(|f| f.declared.into_iter().collect::<Vec<_>>())
            .collect()
    }

    #[test]
    fn split_two_declarator_const() {
        assert_eq!(
            statement_kinds("const A = 1, B = 2;"),
            vec![StatementKind::VarDecl, StatementKind::VarDecl]
        );
        assert_eq!(
            declared_per_statement("const A = 1, B = 2;"),
            vec![vec!["A".to_string()], vec!["B".to_string()]]
        );
    }

    #[test]
    fn split_three_declarator_let() {
        assert_eq!(
            declared_per_statement("let A = 1, B = 2, C = 3;"),
            vec![
                vec!["A".to_string()],
                vec!["B".to_string()],
                vec!["C".to_string()],
            ]
        );
    }

    #[test]
    fn split_export_const_with_comma_list() {
        // `export const A = 1, B = 2;` splits into two ExportDecls,
        // each declaring one name. Kind stays VarDecl (per
        // classify_item, ExportDecl-of-Var classifies as VarDecl).
        assert_eq!(
            statement_kinds("export const A = 1, B = 2;"),
            vec![StatementKind::VarDecl, StatementKind::VarDecl]
        );
        assert_eq!(
            declared_per_statement("export const A = 1, B = 2;"),
            vec![vec!["A".to_string()], vec!["B".to_string()]]
        );
    }

    #[test]
    fn single_declarator_var_decl_is_unchanged() {
        assert_eq!(statement_kinds("var A;"), vec![StatementKind::VarDecl]);
        assert_eq!(
            declared_per_statement("var A;"),
            vec![vec!["A".to_string()]]
        );
    }

    #[test]
    fn non_var_decl_statements_are_not_split() {
        // function / class declarations have no comma-list shape.
        // Mixed source: const + function + class + bare expression.
        assert_eq!(
            statement_kinds("const A = 1; function f() {} class C {} 'side-effecting-string';"),
            vec![
                StatementKind::VarDecl,
                StatementKind::FnDecl,
                StatementKind::ClassDecl,
                StatementKind::SideEffect,
            ]
        );
    }

    // --- Comma-list owner attribution in build_module_dep_graph -------------

    #[test]
    fn split_comma_list_attributes_reads_per_declarator() {
        // `const A = 1, B = X;` — A → mod_0, B → mod_1, X → mod_1.
        // Pre-split, `stmt_owner` would pick A's owner (mod_0)
        // for the whole comma-list and attribute `B`'s read of X
        // to mod_0, creating an R-edge mod_0 → mod_1 even though
        // the actual emitted module for B is mod_1. Post-split,
        // each declarator is its own statement: A's row owns
        // nothing readwise (literal init), B's row owns the read
        // of X but its home is mod_1 — so no edge (B reads X
        // within its own module).
        let schedule = schedule_for(
            "const A = 1, B = X; const X = 42;",
            &[("A", logical(0)), ("B", logical(1)), ("X", logical(1))],
        );
        // No cross-module read edges should exist: A's init is
        // pure, B reads X (same module).
        let mod_0 = ModuleId::Logical(LogicalModuleIndex(0));
        let mod_1 = ModuleId::Logical(LogicalModuleIndex(1));
        assert!(
            !schedule.dep_graph.graph.contains_edge(mod_0, mod_1),
            "no edge mod_0 → mod_1 expected, got: {:?}",
            schedule.dep_graph.graph.edge_weight(mod_0, mod_1),
        );
        assert!(
            !schedule.dep_graph.graph.contains_edge(mod_1, mod_0),
            "no edge mod_1 → mod_0 expected, got: {:?}",
            schedule.dep_graph.graph.edge_weight(mod_1, mod_0),
        );
    }

    #[test]
    fn split_comma_list_surfaces_real_cross_declarator_cycle() {
        // `const A = X, B = 1;` — A → mod_a, B → mod_b, X → mod_b.
        // mod_a's `A` reads X from mod_b → R-edge mod_a → mod_b.
        // Now also `const Y = A;` in mod_b reads A from mod_a:
        // → R-edge mod_b → mod_a. Cycle.
        //
        // Pre-split, the comma-list `const A = X, B = 1;` would
        // attribute the read of X to mod_a (A is declared first,
        // owner mod_a). So the edge is mod_a → mod_b. mod_b's
        // `Y = A` adds mod_b → mod_a. Cycle detected (correctly,
        // by accident). Post-split, A's row attributes the read
        // to mod_a, B's row to mod_b — same edges, same cycle.
        // This case demonstrates the split doesn't *miss* real
        // cycles either: the bug bit when multiple declarators
        // had differently-owned reads on the same line.
        let schedule = schedule_for(
            "const A = X, B = 1; const X = 42; const Y = A;",
            &[
                ("A", logical(0)),
                ("B", logical(1)),
                ("X", logical(1)),
                ("Y", logical(1)),
            ],
        );
        let report = schedule.validate();
        assert!(
            !report.cycles.is_empty(),
            "expected a real cycle to be reported"
        );
    }

    // --- linker_order in ScheduleReport --------------------------------------

    #[test]
    fn validate_surfaces_linker_order_for_acyclic_spec() {
        // mod_0 reads B from mod_1 at-init → mod_1 must precede
        // mod_0 in the linker's evaluation order.
        let schedule = schedule_for(
            "const A = B + 1; const B = 42;",
            &[("A", logical(0)), ("B", logical(1))],
        );
        let report = schedule.validate();
        let order = &report.linker_order;
        let pos = |name: &str| -> usize {
            order
                .iter()
                .position(|m| m == name)
                .unwrap_or_else(|| panic!("module {name} not in {order:?}"))
        };
        assert!(
            pos("mod_1") < pos("mod_0"),
            "mod_1 must precede mod_0 in linker_order; got {order:?}",
        );
    }

    #[test]
    fn validate_returns_empty_linker_order_for_cyclic_spec() {
        // mod_0 reads B (mod_1); mod_1 reads A (mod_0). Cycle.
        let schedule = schedule_for(
            "const A = B + 1; const B = A + 1;",
            &[("A", logical(0)), ("B", logical(1))],
        );
        let report = schedule.validate();
        assert!(!report.cycles.is_empty(), "expected a cycle in {report:?}",);
        assert!(
            report.linker_order.is_empty(),
            "linker_order must be empty when the dep graph is cyclic; got {:?}",
            report.linker_order,
        );
    }

    #[test]
    fn schedule_report_serializes_linker_order_as_camel_case() {
        let schedule = schedule_for(
            "const A = 1; const B = A + 1;",
            &[("A", logical(0)), ("B", logical(1))],
        );
        let report = schedule.validate();
        let json = serde_json::to_string(&report).expect("serialize ScheduleReport");
        assert!(
            json.contains(r#""linkerOrder""#),
            "ScheduleReport must serialize linker_order as `linkerOrder`; got: {json}",
        );
    }
}
