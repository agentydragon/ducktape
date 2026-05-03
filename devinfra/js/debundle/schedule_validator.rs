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

use std::collections::{BTreeMap, BTreeSet, HashSet};

use petgraph::algo::{tarjan_scc, toposort};
use petgraph::graphmap::DiGraphMap;
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
        let linker_order = compute_linker_order(&dep_graph.edges, &logical_modules);
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
pub fn analyze_chunk_facts(module: &Module) -> Vec<StatementFacts> {
    let body = split_comma_list_var_decls(&module.body);
    body.iter()
        .enumerate()
        .map(|(ordinal, item)| analyze_item(StatementOrdinal(ordinal), item))
        .collect()
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

fn analyze_item(ordinal: StatementOrdinal, item: &ModuleItem) -> StatementFacts {
    let kind = classify_item(item);
    let declared = collect_declared_names(item);
    let mut at_init = AtInitReadCollector::default();
    item.visit_with(&mut at_init);
    let mut lazy = LazyReadCollector::default();
    item.visit_with(&mut lazy);
    let has_side_effect = item_has_side_effect(item, kind);
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

fn classify_expr_purity(expr: &Expr) -> Purity {
    match expr {
        Expr::Lit(_) => Purity::Pure,
        Expr::Ident(_) => Purity::Pure,
        Expr::This(_) | Expr::MetaProp(_) => Purity::Pure,
        Expr::Tpl(tpl) => tpl
            .exprs
            .iter()
            .map(|e| classify_expr_purity(e))
            .fold(Purity::Pure, Purity::worst),
        Expr::Fn(_) | Expr::Arrow(_) => Purity::Pure,
        Expr::Class(class_expr) => {
            if class_has_static_observable(&class_expr.class) {
                Purity::Impure
            } else {
                Purity::Pure
            }
        }
        Expr::Paren(p) => classify_expr_purity(&p.expr),
        Expr::Unary(u) => match u.op {
            UnaryOp::Delete => Purity::Impure,
            // typeof / void / +/-/!/~ on a pure operand are pure
            // (they may coerce, but coercion of an Ident or Lit
            // doesn't run user code).
            _ => classify_expr_purity(&u.arg),
        },
        Expr::Bin(b) => classify_expr_purity(&b.left).worst(classify_expr_purity(&b.right)),
        Expr::Cond(c) => classify_expr_purity(&c.test)
            .worst(classify_expr_purity(&c.cons))
            .worst(classify_expr_purity(&c.alt)),
        Expr::Seq(s) => s
            .exprs
            .iter()
            .map(|e| classify_expr_purity(e))
            .fold(Purity::Pure, Purity::worst),
        Expr::Array(arr) => {
            let mut acc = Purity::Pure;
            for elem in arr.elems.iter().flatten() {
                if elem.spread.is_some() {
                    // Spread invokes the iterator protocol; could
                    // be impure even on a literal.
                    acc = acc.worst(Purity::Unknown);
                }
                acc = acc.worst(classify_expr_purity(&elem.expr));
            }
            acc
        }
        Expr::Object(obj) => {
            let mut acc = Purity::Pure;
            for prop in &obj.props {
                acc = acc.worst(classify_prop_purity(prop));
            }
            acc
        }
        // Member access is `Unknown` — `obj.prop` on an arbitrary
        // object can fire a getter; we can't tell statically.
        Expr::Member(_) | Expr::SuperProp(_) | Expr::OptChain(_) => Purity::Unknown,
        // Calls / `new` / tagged templates / dynamic import / yield-style:
        // unknown side effects.
        Expr::Call(_) | Expr::New(_) | Expr::TaggedTpl(_) => Purity::Unknown,
        Expr::Assign(_) | Expr::Update(_) => Purity::Impure,
        Expr::Await(_) | Expr::Yield(_) => Purity::Impure,
        // Anything we didn't enumerate falls into the Unknown
        // bucket — soundness-first.
        _ => Purity::Unknown,
    }
}

fn classify_prop_purity(prop: &PropOrSpread) -> Purity {
    match prop {
        PropOrSpread::Spread(spread) => {
            // Spreading an arbitrary expression invokes its
            // iterator (array spread) or property iteration
            // (object spread). Either can fire a getter or a
            // user-defined `[Symbol.iterator]`.
            classify_expr_purity(&spread.expr).worst(Purity::Unknown)
        }
        PropOrSpread::Prop(prop) => match prop.as_ref() {
            Prop::Shorthand(_) => Purity::Pure,
            Prop::KeyValue(kv) => {
                classify_propname_purity(&kv.key).worst(classify_expr_purity(&kv.value))
            }
            Prop::Assign(_) => Purity::Impure,
            // `{ get x() {}, set x(v) {}, m() {} }` — defining a
            // method or accessor is pure; invoking it is not, and
            // we don't invoke it during init.
            Prop::Getter(_) | Prop::Setter(_) | Prop::Method(_) => Purity::Pure,
        },
    }
}

fn classify_propname_purity(name: &PropName) -> Purity {
    match name {
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_) | PropName::BigInt(_) => {
            Purity::Pure
        }
        PropName::Computed(c) => classify_expr_purity(&c.expr),
    }
}

/// Whether a class declaration runs observable code at class-decl
/// time. Static blocks always run; static fields run their
/// initializer. `extends <expr>` is at-init: the expression itself
/// runs, but `extends` references are tracked as `R`-edges
/// elsewhere — here we only report whether the class itself
/// _additionally_ has observable side-effecting init code.
fn class_has_static_observable(class: &Class) -> bool {
    class.body.iter().any(|member| match member {
        ClassMember::StaticBlock(_) => true,
        ClassMember::ClassProp(prop) if prop.is_static => prop
            .value
            .as_deref()
            .map(|v| classify_expr_purity(v) != Purity::Pure)
            .unwrap_or(false),
        ClassMember::PrivateProp(prop) if prop.is_static => prop
            .value
            .as_deref()
            .map(|v| classify_expr_purity(v) != Purity::Pure)
            .unwrap_or(false),
        _ => false,
    })
}

fn item_has_side_effect(item: &ModuleItem, kind: StatementKind) -> bool {
    match kind {
        StatementKind::Import | StatementKind::Export | StatementKind::FnDecl => false,
        StatementKind::VarDecl => var_decl_of_item(item)
            .iter()
            .flat_map(|var| var.decls.iter())
            .any(|d| match d.init.as_deref() {
                Some(init) => classify_expr_purity(init) != Purity::Pure,
                None => false,
            }),
        StatementKind::ClassDecl => class_of_item(item)
            .map(class_has_static_observable)
            .unwrap_or(false),
        StatementKind::SideEffect => match item {
            ModuleItem::Stmt(Stmt::Expr(expr)) => classify_expr_purity(&expr.expr) != Purity::Pure,
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

/// Module dep graph built from per-statement facts and a binding →
/// module assignment.
#[derive(Debug, Clone)]
pub struct ModuleDepGraph {
    pub edges: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    /// Evidence map: `((from, to), reasons)` where each reason is
    /// `(statement_ordinal, binding)`. Used to render the cycle
    /// report.
    pub evidence: BTreeMap<(ModuleId, ModuleId), Vec<(StatementOrdinal, BindingName)>>,
}

/// Build the imports graph `I` (per DESIGN.md "Module dep
/// graphs"): an edge `(M, M')` for every cross-module reference,
/// at-init or lazy. Each edge of `I` corresponds to exactly one
/// emitted `import { b } from "<M'>"` directive in `M`'s body
/// — so the graph constructed here is exactly the graph the ESM
/// linker walks for evaluation order.
///
/// A binding referenced both eagerly and lazily inside the same
/// statement (e.g. `class A extends B { method() { return B; } }`)
/// shows up in both `reads_at_init` and `reads_lazy`. Iterating
/// both still produces the right edge set for `I`: the edge is
/// recorded once in `edges` (it's a `BTreeSet`) and twice in
/// `evidence`. Cycle detection only consults `edges`.
pub fn build_module_dep_graph(
    facts: &[StatementFacts],
    binding_assignment: &BTreeMap<BindingName, ModuleId>,
) -> ModuleDepGraph {
    let mut edges = BTreeMap::<ModuleId, BTreeSet<ModuleId>>::new();
    let mut evidence =
        BTreeMap::<(ModuleId, ModuleId), Vec<(StatementOrdinal, BindingName)>>::new();
    let stmt_owner = |stmt: &StatementFacts| -> ModuleId {
        stmt.declared
            .iter()
            .filter_map(|name| binding_assignment.get(name).copied())
            .next()
            .unwrap_or(ModuleId::ResidualEntry)
    };
    let record = |from: ModuleId,
                  binding: &BindingName,
                  ordinal: StatementOrdinal,
                  edges: &mut BTreeMap<ModuleId, BTreeSet<ModuleId>>,
                  evidence: &mut BTreeMap<
        (ModuleId, ModuleId),
        Vec<(StatementOrdinal, BindingName)>,
    >| {
        let Some(&to) = binding_assignment.get(binding) else {
            return; // not a chunk-owned binding (global, ImportSpecifier, never-declared)
        };
        if to == from {
            return;
        }
        edges.entry(from).or_default().insert(to);
        evidence
            .entry((from, to))
            .or_default()
            .push((ordinal, binding.clone()));
    };
    for stmt in facts {
        let from = stmt_owner(stmt);
        for binding in &stmt.reads_at_init {
            record(from, binding, stmt.ordinal, &mut edges, &mut evidence);
        }
        for binding in &stmt.reads_lazy {
            record(from, binding, stmt.ordinal, &mut edges, &mut evidence);
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
    let mut seen_modules: BTreeSet<ModuleId> = BTreeSet::new();
    for stmt in facts.iter().filter(|s| s.has_side_effect) {
        let from = stmt_owner(stmt);
        let predecessors: Vec<ModuleId> = seen_modules
            .iter()
            .copied()
            .filter(|&m| m != from)
            .collect();
        for to in predecessors {
            let inserted = edges.entry(from).or_default().insert(to);
            if inserted {
                // First-time edge — record one evidence entry per
                // (from, to) pair so the cycle report doesn't fan
                // out into thousands of side-effect rows. The
                // ordinal is the *later* statement; the earlier
                // one is implicit in `home(to)`.
                evidence
                    .entry((from, to))
                    .or_default()
                    .push((stmt.ordinal, "<side-effect>".to_string()));
            }
        }
        seen_modules.insert(from);
    }

    ModuleDepGraph { edges, evidence }
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
}

#[derive(Debug, Clone, Serialize)]
pub struct CycleEdge {
    pub from: String,
    pub to: String,
    #[serde(rename = "statementOrdinal")]
    pub statement_ordinal: StatementOrdinal,
    pub binding: BindingName,
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
    let sccs = sccs_of(&graph.edges);
    let mut cycles = Vec::new();
    for scc in sccs {
        let in_scc: HashSet<ModuleId> = scc.iter().copied().collect();
        let is_cycle = scc.len() > 1
            || (scc.len() == 1
                && graph
                    .edges
                    .get(&scc[0])
                    .is_some_and(|targets| targets.contains(&scc[0])));
        if !is_cycle {
            continue;
        }
        let mut evidence = Vec::new();
        for (&(from, to), reasons) in &graph.evidence {
            if !in_scc.contains(&from) || !in_scc.contains(&to) {
                continue;
            }
            for (ordinal, binding) in reasons {
                evidence.push(CycleEdge {
                    from: module_name(from),
                    to: module_name(to),
                    statement_ordinal: *ordinal,
                    binding: binding.clone(),
                });
            }
        }
        cycles.push(CycleReport {
            modules: scc.iter().copied().map(module_name).collect(),
            evidence,
        });
    }
    ScheduleReport {
        kind: "js.schedule_validator_report",
        cycles,
        recommendations: Vec::new(),
        linker_order: Vec::new(),
    }
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
/// and check that no SCC of size > 1 (and no self-loop) appears.
fn is_assignment_cycle_safe(
    schedule: &Schedule,
    binding: &BindingName,
    candidate: ModuleId,
) -> bool {
    let mut augmented = owned_view(&schedule.bindings);
    augmented.insert(binding.clone(), candidate);
    let graph = build_module_dep_graph(&schedule.facts, &augmented);
    let sccs = sccs_of(&graph.edges);
    !sccs.iter().any(|scc| {
        scc.len() > 1
            || (scc.len() == 1
                && graph
                    .edges
                    .get(&scc[0])
                    .is_some_and(|targets| targets.contains(&scc[0])))
    })
}

/// Strongly-connected components of the dep graph, via petgraph's
/// `tarjan_scc`. Each SCC is a `Vec<ModuleId>` in reverse
/// topological order (SCCs appear after their dependencies).
fn sccs_of(edges: &BTreeMap<ModuleId, BTreeSet<ModuleId>>) -> Vec<Vec<ModuleId>> {
    let mut graph = DiGraphMap::<ModuleId, ()>::new();
    // Ensure isolated nodes (only on the receive side of an edge or
    // standalone in the assignment) appear in the graph.
    for (&from, targets) in edges {
        graph.add_node(from);
        for &to in targets {
            graph.add_node(to);
            graph.add_edge(from, to, ());
        }
    }
    tarjan_scc(&graph)
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
    edges: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
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
    for (&from, targets) in edges {
        graph.add_node(from);
        for &to in targets {
            graph.add_node(to);
            graph.add_edge(from, to, ());
        }
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
        let facts = analyze_chunk_facts(&module);
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
        let facts = analyze_chunk_facts(&module);
        assert_eq!(facts.len(), 1);
        // extends A is eager; method body reference to X is lazy.
        assert!(facts[0].reads_at_init.contains("A"));
        assert!(!facts[0].reads_at_init.contains("X"));
    }

    #[test]
    fn computed_key_reads_at_init() {
        let module = parse("const M = { [k.foo]: 1 };");
        let facts = analyze_chunk_facts(&module);
        // The key expression `k.foo` reads `k` at-init.
        assert!(facts[0].reads_at_init.contains("k"));
    }

    #[test]
    fn class_static_init_reads_at_init() {
        let module = parse("class C { static x = Y; }");
        let facts = analyze_chunk_facts(&module);
        assert!(facts[0].reads_at_init.contains("Y"));
    }

    #[test]
    fn class_instance_init_is_lazy() {
        let module = parse("class C { x = Y; }");
        let facts = analyze_chunk_facts(&module);
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
        let facts = analyze_chunk_facts(&module);
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
        let facts = analyze_chunk_facts(&module);
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

    fn schedule_for(source: &str, ownership: &[(&str, ModuleId)]) -> Schedule {
        let module = parse(source);
        let facts = analyze_chunk_facts(&module);
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
        classify_expr_purity(init)
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

    // --- has_side_effect refinement ------------------------------------------

    fn has_side_effect_for(src: &str) -> Vec<bool> {
        let module = parse(src);
        analyze_chunk_facts(&module)
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
        analyze_chunk_facts(&module)
            .into_iter()
            .map(|f| f.kind)
            .collect()
    }

    fn declared_per_statement(source: &str) -> Vec<Vec<String>> {
        let module = parse(source);
        analyze_chunk_facts(&module)
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
        let edges = &schedule.dep_graph.edges;
        // No cross-module read edges should exist: A's init is
        // pure, B reads X (same module).
        let mod_0 = ModuleId::Logical(LogicalModuleIndex(0));
        let mod_1 = ModuleId::Logical(LogicalModuleIndex(1));
        assert!(
            edges
                .get(&mod_0)
                .is_none_or(|targets| !targets.contains(&mod_1)),
            "no edge mod_0 → mod_1 expected, got: {:?}",
            edges.get(&mod_0),
        );
        assert!(
            edges
                .get(&mod_1)
                .is_none_or(|targets| !targets.contains(&mod_0)),
            "no edge mod_1 → mod_0 expected, got: {:?}",
            edges.get(&mod_1),
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
