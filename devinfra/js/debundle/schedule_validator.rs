//! Static schedule validation for `materialize_logical_modules`.
//!
//! Background: see `plans/your-terminal-goal-is-iridescent-meteor.md`
//! ("Debundler — principled redesign"). This module is Phase 1 of
//! that redesign — a *report-only* pass that runs alongside the
//! existing init-wrapper machinery.
//!
//! The pass treats debundling as a scheduling problem:
//!
//! 1. For each top-level statement in the source chunk, compute the
//!    bindings it declares, the bindings it reads at-init, whether
//!    it has an observable side effect, and its source ordinal.
//! 2. Map each statement to its destination module (logical module
//!    or residual entry) using the spec's binding assignment.
//! 3. Build a directed module dep graph: edge `M_S → M_b` for every
//!    `(S, b)` where statement `S` lives in module `M_S` and
//!    `b ∈ reads_at_init(S)` is owned by module `M_b ≠ M_S`.
//! 4. Validate: the dep graph must be acyclic. Cycles are the
//!    unrealizable case — no ESM evaluation order can satisfy the
//!    spec's assignment without papering over the cycle at runtime.
//!
//! The output is a JSON report listing the cycles + their evidence
//! (which `(statement, binding)` pairs form each cycle). The report
//! is written next to the existing manifests; the existing emit path
//! continues unchanged. Phase 3 (later) switches the emit path to
//! source-order and turns cycles into hard errors.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};

use serde::Serialize;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

/// The residual entry "module" is given a synthetic index so cycles
/// involving it surface in the report just like cycles between
/// logical modules.
pub const RESIDUAL_ENTRY_INDEX: usize = usize::MAX;

#[derive(Debug, Clone)]
pub struct StatementFacts {
    pub ordinal: usize,
    pub declared: BTreeSet<String>,
    pub reads_at_init: BTreeSet<String>,
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
pub fn analyze_chunk_facts(module: &Module) -> Vec<StatementFacts> {
    module
        .body
        .iter()
        .enumerate()
        .map(|(ordinal, item)| analyze_item(ordinal, item))
        .collect()
}

fn analyze_item(ordinal: usize, item: &ModuleItem) -> StatementFacts {
    let kind = classify_item(item);
    let declared = collect_declared_names(item);
    let mut reads_collector = AtInitReadCollector::default();
    item.visit_with(&mut reads_collector);
    let reads_at_init = reads_collector.names;
    let has_side_effect = matches!(kind, StatementKind::VarDecl | StatementKind::SideEffect)
        || (kind == StatementKind::ClassDecl && class_has_static_init(item));
    StatementFacts {
        ordinal,
        declared,
        reads_at_init,
        has_side_effect,
        kind,
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

fn class_has_static_init(item: &ModuleItem) -> bool {
    let class = match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(cls))) => &cls.class,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Class(cls) => &cls.class,
            _ => return false,
        },
        _ => return false,
    };
    class.body.iter().any(|member| match member {
        ClassMember::ClassProp(prop) => prop.is_static,
        ClassMember::PrivateProp(prop) => prop.is_static,
        ClassMember::StaticBlock(_) => true,
        _ => false,
    })
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

/// Module dep graph built from per-statement facts and a binding →
/// module-index assignment.
#[derive(Debug, Clone)]
pub struct ModuleDepGraph {
    /// Nodes are module indices; the residual entry is
    /// `RESIDUAL_ENTRY_INDEX`.
    pub edges: BTreeMap<usize, BTreeSet<usize>>,
    /// Evidence map: `((from, to), reasons)` where each reason is
    /// `(statement_ordinal, binding)`. Used to render the cycle
    /// report.
    pub evidence: BTreeMap<(usize, usize), Vec<(usize, String)>>,
}

pub fn build_module_dep_graph(
    facts: &[StatementFacts],
    binding_assignment: &BTreeMap<String, usize>,
) -> ModuleDepGraph {
    let mut edges = BTreeMap::<usize, BTreeSet<usize>>::new();
    let mut evidence = BTreeMap::<(usize, usize), Vec<(usize, String)>>::new();
    let stmt_owner = |stmt: &StatementFacts| -> usize {
        stmt.declared
            .iter()
            .filter_map(|name| binding_assignment.get(name).copied())
            .next()
            .unwrap_or(RESIDUAL_ENTRY_INDEX)
    };
    for stmt in facts {
        let from = stmt_owner(stmt);
        for binding in &stmt.reads_at_init {
            let Some(&to) = binding_assignment.get(binding) else {
                continue; // not a chunk-owned binding (could be a global, an import, or a never-declared name)
            };
            if to == from {
                continue;
            }
            edges.entry(from).or_default().insert(to);
            evidence
                .entry((from, to))
                .or_default()
                .push((stmt.ordinal, binding.clone()));
        }
    }
    ModuleDepGraph { edges, evidence }
}

/// Result of validating a module dep graph.
#[derive(Debug, Clone, Serialize)]
pub struct ScheduleReport {
    pub kind: &'static str,
    pub cycles: Vec<CycleReport>,
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
    pub statement_ordinal: usize,
    pub binding: String,
}

/// Find SCCs in the dep graph and produce a report listing every
/// non-trivial cycle (size > 1 OR a self-loop). Trivial single-node
/// non-self-loop SCCs are dropped.
pub fn validate_schedule(
    graph: &ModuleDepGraph,
    module_name: &dyn Fn(usize) -> String,
) -> ScheduleReport {
    let sccs = tarjan_sccs(&graph.edges);
    let mut cycles = Vec::new();
    for scc in sccs {
        let in_scc: HashSet<usize> = scc.iter().copied().collect();
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
    }
}

/// Tarjan's strongly-connected-components algorithm. Returns SCCs in
/// reverse topological order.
fn tarjan_sccs(edges: &BTreeMap<usize, BTreeSet<usize>>) -> Vec<Vec<usize>> {
    let mut nodes: BTreeSet<usize> = edges.keys().copied().collect();
    for targets in edges.values() {
        nodes.extend(targets.iter().copied());
    }
    let mut index_counter = 0usize;
    let mut indices = HashMap::<usize, usize>::new();
    let mut lowlinks = HashMap::<usize, usize>::new();
    let mut on_stack = HashSet::<usize>::new();
    let mut stack = Vec::<usize>::new();
    let mut sccs = Vec::<Vec<usize>>::new();
    for &node in &nodes {
        if !indices.contains_key(&node) {
            strong_connect(
                node,
                edges,
                &mut index_counter,
                &mut indices,
                &mut lowlinks,
                &mut on_stack,
                &mut stack,
                &mut sccs,
            );
        }
    }
    sccs
}

#[allow(clippy::too_many_arguments)]
fn strong_connect(
    v: usize,
    edges: &BTreeMap<usize, BTreeSet<usize>>,
    index_counter: &mut usize,
    indices: &mut HashMap<usize, usize>,
    lowlinks: &mut HashMap<usize, usize>,
    on_stack: &mut HashSet<usize>,
    stack: &mut Vec<usize>,
    sccs: &mut Vec<Vec<usize>>,
) {
    // Iterative Tarjan: emulate the recursive call stack so deep
    // graphs don't blow the Rust stack.
    enum Frame {
        Visit(usize),
        Resume(usize, std::vec::IntoIter<usize>),
    }
    let mut frames = VecDeque::<Frame>::new();
    frames.push_back(Frame::Visit(v));
    while let Some(frame) = frames.pop_back() {
        match frame {
            Frame::Visit(v) => {
                indices.insert(v, *index_counter);
                lowlinks.insert(v, *index_counter);
                *index_counter += 1;
                stack.push(v);
                on_stack.insert(v);
                let neighbours: Vec<usize> = edges
                    .get(&v)
                    .map(|set| set.iter().copied().collect())
                    .unwrap_or_default();
                frames.push_back(Frame::Resume(v, neighbours.into_iter()));
            }
            Frame::Resume(v, mut iter) => {
                if let Some(w) = iter.next() {
                    frames.push_back(Frame::Resume(v, iter));
                    if !indices.contains_key(&w) {
                        frames.push_back(Frame::Visit(w));
                    } else if on_stack.contains(&w) {
                        let lw = lowlinks[&w];
                        let lv = lowlinks[&v];
                        lowlinks.insert(v, lv.min(lw));
                    }
                } else {
                    // Combine lowlinks of children: when a child has
                    // resolved we adjust `v`'s lowlink to whichever
                    // is smaller.
                    let neighbours: Vec<usize> = edges
                        .get(&v)
                        .map(|set| set.iter().copied().collect())
                        .unwrap_or_default();
                    for w in neighbours {
                        if let (Some(&lv), Some(&lw)) = (lowlinks.get(&v), lowlinks.get(&w)) {
                            lowlinks.insert(v, lv.min(lw));
                        }
                    }
                    if lowlinks[&v] == indices[&v] {
                        let mut component = Vec::new();
                        loop {
                            let w = stack.pop().expect("non-empty SCC stack");
                            on_stack.remove(&w);
                            component.push(w);
                            if w == v {
                                break;
                            }
                        }
                        sccs.push(component);
                    }
                }
            }
        }
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

    #[test]
    fn cycle_detected_between_two_modules() {
        // mod_a owns A; A's init reads B (owned by mod_b).
        // mod_b owns B; B's init reads A (owned by mod_a).
        let module = parse("const A = B + 1; const B = A + 1;");
        let facts = analyze_chunk_facts(&module);
        let mut binding_assignment = BTreeMap::new();
        binding_assignment.insert("A".to_string(), 0);
        binding_assignment.insert("B".to_string(), 1);
        let graph = build_module_dep_graph(&facts, &binding_assignment);
        let report = validate_schedule(&graph, &|idx| format!("mod_{idx}"));
        assert_eq!(report.cycles.len(), 1);
        assert_eq!(report.cycles[0].modules.len(), 2);
    }

    #[test]
    fn dag_has_no_cycles() {
        let module = parse("const A = 1; const B = A + 1; const C = B + A;");
        let facts = analyze_chunk_facts(&module);
        let mut binding_assignment = BTreeMap::new();
        binding_assignment.insert("A".to_string(), 0);
        binding_assignment.insert("B".to_string(), 1);
        binding_assignment.insert("C".to_string(), 2);
        let graph = build_module_dep_graph(&facts, &binding_assignment);
        let report = validate_schedule(&graph, &|idx| format!("mod_{idx}"));
        assert!(
            report.cycles.is_empty(),
            "expected no cycles, got {:?}",
            report.cycles
        );
    }
}
