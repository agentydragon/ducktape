//! Factorization-validation tests: cycle detection, realizability,
//! and purity interplay through `validate_factorization` over real
//! parsed chunks. They exercise the analysis crate's
//! `chunk_factorization`, `validation`, and purity machinery (the
//! file lived at `peel/factorize_tests.rs` historically, but never
//! tested `peel::factorize`).

use std::collections::{BTreeSet, HashMap};

use super::{analyze_facts, parse, test_id};
use crate::*;
use analysis::*;
use swc_common::{FileName, sync::Lrc};
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

/// Test-only convenience for constructing `ModuleId` values from a
/// raw logical index (the `logical` free function is not part of the
/// crate's public API).
fn logical(idx: usize) -> ModuleId {
    ModuleId::logical(idx)
}

/// logical module always sits at the highest index appended by
/// `factorization_for` / `factorization_with_residual_module`. Tests use this
/// instead of the historical `ModuleId::ResidualEntry` literal.
fn residual() -> ModuleId {
    ModuleId::logical(usize::MAX)
}

/// Canonical-path renderer for `validate_factorization`: the residual
/// sentinel renders as `residual`, explicit modules as `mod_<idx>`.
fn render(id: ModuleId) -> spec::ModulePath {
    let LogicalModuleIndex(idx) = id.0;
    let raw = if idx == usize::MAX {
        "residual".to_string()
    } else {
        format!("mod_{idx}")
    };
    spec::ModulePath::parse(&raw, "").unwrap()
}

fn member_bindings(members: &[BindingReport]) -> Vec<String> {
    members
        .iter()
        .map(|member| member.binding.to_string())
        .collect()
}

// --- Factorization: cycle detection & realizability ---------------------

#[test]
fn cycle_detected_between_two_modules() {
    // mod_a owns A; A's init reads B (owned by mod_b).
    // mod_b owns B; B's init reads A (owned by mod_a).
    let module = parse("const A = B + 1; const B = A + 1;");
    let facts = analyze_facts(&module);
    let mut binding_assignment = HashMap::new();
    binding_assignment.insert(test_id("A"), logical(0));
    binding_assignment.insert(test_id("B"), logical(1));
    let owner_graph = build_owner_graph(&facts).unwrap();
    let partition =
        Partition::from_binding_assignment(&owner_graph, &binding_assignment, residual());
    let report = validate_factorization(&owner_graph, &partition, &render);
    assert_eq!(report.cycles.len(), 1);
    assert_eq!(report.cycles[0].modules.len(), 2);
}

#[test]
fn dag_has_no_cycles() {
    let module = parse("const A = 1; const B = A + 1; const C = B + A;");
    let facts = analyze_facts(&module);
    let mut binding_assignment = HashMap::new();
    binding_assignment.insert(test_id("A"), logical(0));
    binding_assignment.insert(test_id("B"), logical(1));
    binding_assignment.insert(test_id("C"), logical(2));
    let owner_graph = build_owner_graph(&facts).unwrap();
    let partition =
        Partition::from_binding_assignment(&owner_graph, &binding_assignment, residual());
    let report = validate_factorization(&owner_graph, &partition, &render);
    assert!(
        report.cycles.is_empty(),
        "expected no cycles, got {:?}",
        report.cycles
    );
}

/// A mixed cycle (lazy forward-edge, at-init back-edge) where
/// the lazy direction is NOT invoked at-init is realizable per
/// docs/design.md "Realizability primitive" clause 3 — the
/// constraining-edge subgraph (drops LazyUse) has no
/// multi-module SCC. The materializer's Lemma 2 steering
/// (ChunkFactorization::source_import_position with SCC-aware reverse)
/// gives entry an import order such that the ESM linker
/// resolves the cycle without TDZ.
#[test]
fn mixed_cycle_without_at_init_call_is_realizable() {
    // mod_0 owns A and readB; readB body returns B (lazy read,
    // never invoked at-init). mod_1 owns B; B = A + 1
    // (at-init read of A). Constraining subgraph: only
    // mod_1 → mod_0 — acyclic. Relaxed clause-3 accepts.
    let module = parse("const A = 1; function readB() { return B; } const B = A + 1;");
    let facts = analyze_facts(&module);
    let mut binding_assignment = HashMap::new();
    binding_assignment.insert(test_id("A"), logical(0));
    binding_assignment.insert(test_id("readB"), logical(0));
    binding_assignment.insert(test_id("B"), logical(1));
    let owner_graph = build_owner_graph(&facts).unwrap();
    let partition =
        Partition::from_binding_assignment(&owner_graph, &binding_assignment, residual());
    let report = validate_factorization(&owner_graph, &partition, &render);
    assert!(
        report.cycles.is_empty(),
        "mixed cycle with no at-init call should be realizable; got {:?}",
        report.cycles,
    );
}

/// Verify that at-init call promotion materializes a promoted
/// owner-graph edge for a top-level call to a chunk function
/// whose body lazily reads a cross-module binding. The promoted
/// edge appears as an EagerUse edge from the caller statement's
/// owner to the target binding's owner.
#[test]
fn at_init_call_promotion_materializes_owner_edge() {
    // owner 0: function readB { return B; } (reads.lazy = {B})
    // owner 1: const A = 1
    // owner 2: const triggerInit = readB(); (calls.eager = {readB})
    // owner 3: const B = A + 1; (declared = {B}, reads.eager = {A})
    // Promotion should add an EagerUse edge owner 2 → owner 3
    // because triggerInit at-init-calls readB whose body reads B.
    let module = parse(
        "function readB() { return B; } const A = 1; const triggerInit = readB(); const B = A + 1;",
    );
    let facts = analyze_facts(&module);
    assert_eq!(
        facts[2].calls.eager,
        BTreeSet::from([test_id("readB")]),
        "triggerInit's calls.eager must include readB: {:?}",
        facts[2].calls.eager,
    );
    let owner_graph = build_owner_graph(&facts).unwrap();
    let promoted: Vec<_> = owner_graph
        .iter_edges()
        .filter(|e| e.from == OwnerId(2) && e.to == OwnerId(3))
        .collect();
    assert!(
        promoted
            .iter()
            .any(|e| e.reason.kind() == DepKind::EagerUse),
        "expected a promoted EagerUse edge owner 2 → owner 3 in {:?}",
        owner_graph
            .iter_edges()
            .map(|e| (e.from, e.to, e.reason.kind()))
            .collect::<Vec<_>>(),
    );
}

#[test]
fn at_init_call_promotion_closes_otherwise_relaxed_cycle() {
    // mod_0 owns readB, triggerInit (which at-init-calls readB),
    // and A. mod_1 owns B. Promotion: triggerInit's owner (mod_0)
    // gets a promoted eager edge to B's owner (mod_1) because
    // readB's body lazily reads B. Combined with B's eager edge
    // back to A (mod_1 → mod_0), the constraining-edge subgraph
    // contains a 2-cycle. The lazy `readB → B` edge is still in
    // the full quotient as evidence but is excluded from the cut.
    // Source order matters: B reads A (mod_1 → mod_0 eager) is
    // the back-edge that the promoted forward-edge closes into a
    // cycle.
    let module = parse(
        "function readB() { return B; } const A = 1; const triggerInit = readB(); const B = A + 1;",
    );
    let facts = analyze_facts(&module);
    let mut binding_assignment = HashMap::new();
    binding_assignment.insert(test_id("readB"), logical(0));
    binding_assignment.insert(test_id("triggerInit"), logical(0));
    binding_assignment.insert(test_id("A"), logical(0));
    binding_assignment.insert(test_id("B"), logical(1));
    let owner_graph = build_owner_graph(&facts).unwrap();
    let partition =
        Partition::from_binding_assignment(&owner_graph, &binding_assignment, residual());
    let report = validate_factorization(&owner_graph, &partition, &render);
    assert_eq!(
        report.cycles.len(),
        1,
        "at-init call promotion must close the cycle; got {:?}",
        report.cycles,
    );
    let cycle = &report.cycles[0];
    assert!(
        !cycle.cut.iter().any(|e| e.kind == DepKind::LazyUse),
        "cut must not include lazy reasons, got {:?}",
        cycle.cut,
    );
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
    let facts = analyze_facts(&module);
    let mut binding_assignment = HashMap::new();
    binding_assignment.insert(test_id("a1"), logical(0));
    binding_assignment.insert(test_id("a2"), logical(0));
    binding_assignment.insert(test_id("b1"), logical(1));
    let owner_graph = build_owner_graph(&facts).unwrap();
    let partition =
        Partition::from_binding_assignment(&owner_graph, &binding_assignment, residual());
    let report = validate_factorization(&owner_graph, &partition, &render);
    assert_eq!(report.cycles.len(), 1);
    let cycle = &report.cycles[0];
    assert!(
        !cycle.cut.is_empty(),
        "cut should be non-empty for an unrealizable cycle, got {:?}",
        cycle.cut,
    );
    assert!(
        cycle.cut.iter().all(|e| e.kind == DepKind::Sequenced),
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
    let facts = analyze_facts(&module);
    let mut binding_assignment = HashMap::new();
    binding_assignment.insert(test_id("helperA"), logical(0));
    binding_assignment.insert(test_id("A"), logical(0));
    binding_assignment.insert(test_id("helperB"), logical(1));
    binding_assignment.insert(test_id("B"), logical(1));
    let owner_graph = build_owner_graph(&facts).unwrap();
    let partition =
        Partition::from_binding_assignment(&owner_graph, &binding_assignment, residual());
    let report = validate_factorization(&owner_graph, &partition, &render);
    assert!(
        report.cycles.is_empty(),
        "lazy-only cycle is realizable; the gate must accept and emit no cycle (got {:?})",
        report.cycles,
    );
}

// --- Lazy rebind atomic-unit constraints --------------------------------

/// LazyRebind atomic-unit split: declarer and assigner of a
/// mutable binding must materialize together. `factor_assembly`
/// records this as an `atomic_unit_conflicts` entry on the
/// factorization; the materializer bails on any non-empty list.
#[test]
fn cross_destination_lazy_write_is_rejected() {
    let factorization = factorization_for(
        "let A = 0; function B() { A = 1; }",
        &[("A", logical(0)), ("B", residual())],
    );
    let report = factorization.validate();
    assert_eq!(
        report.atomic_unit_conflicts.len(),
        1,
        "expected one atomic-unit conflict (A and B share a LazyRebind atomic unit but the spec splits them): {report:?}",
    );
    let conflict = &report.atomic_unit_conflicts[0];
    // Residual is now the synthesized logical module at index 1
    // (the explicit `mod_0` is at index 0).
    assert_eq!(
        distinct_claim_modules(conflict),
        vec![ModuleId::logical(0), ModuleId::logical(1)],
    );
}

/// Sorted distinct destination modules across a conflict's claims —
/// the typed equivalent of the prior string-rendered
/// `conflicting_modules` field on `AtomicUnitConflictReport`.
fn distinct_claim_modules(conflict: &AtomicUnitConflict) -> Vec<ModuleId> {
    let mut modules: Vec<ModuleId> = conflict
        .claims
        .iter()
        .map(|c| c.module)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    modules.sort();
    modules
}

#[test]
fn same_destination_lazy_write_is_allowed() {
    let factorization = factorization_for(
        "let A = 0; function B() { A = 1; }",
        &[("A", logical(0)), ("B", logical(0))],
    );

    let report = factorization.validate();
    assert!(
        report.atomic_unit_conflicts.is_empty(),
        "same-destination rebinding writes should stay local to the emitted module: {report:?}",
    );
}

// --- Factorization helpers -----------------------------------------------

/// Resolve a sentinel `residual()` ModuleId to the real residual
/// logical-module index. Helper for `factorization_for`: in the new
/// no-variant world the residual is just a logical module, so
/// tests that used to write `ModuleId::ResidualEntry` now write
/// `residual()` (a `usize::MAX`-indexed sentinel) and let the
/// builder remap it.
fn resolve_test_module_id(id: ModuleId, residual_idx: usize) -> ModuleId {
    if id.0.0 == usize::MAX {
        ModuleId::logical(residual_idx)
    } else {
        id
    }
}

fn factorization_for(source: &str, ownership: &[(&str, ModuleId)]) -> ChunkFactorization {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let mut max_idx: Option<usize> = None;
    for (_, id) in ownership {
        let LogicalModuleIndex(i) = id.0;
        if i == usize::MAX {
            continue;
        }
        max_idx = Some(max_idx.map_or(i, |m| m.max(i)));
    }
    let explicit_count = max_idx.map_or(0, |i| i + 1);
    let residual_idx = explicit_count;
    let mut bindings = HashMap::new();
    for (name, id) in ownership {
        let resolved = resolve_test_module_id(*id, residual_idx);
        bindings.insert(test_id(name), BindingKind::Owned { module: resolved });
    }
    let mut logical_modules: Vec<LogicalModule> = (0..explicit_count)
        .map(|i| LogicalModule {
            id: format!("mod_{i}"),
            target_file: format!("mod_{i}.js"),
            residual: false,
            rename_map: HashMap::new(),
            anonymous_statement_ordinals: Vec::new(),
        })
        .collect();
    logical_modules.push(LogicalModule {
        id: "residual".to_string(),
        target_file: "residual/unhandled.js".to_string(),
        residual: true,
        rename_map: HashMap::new(),
        anonymous_statement_ordinals: Vec::new(),
    });
    ChunkFactorization::build(
        "test_chunk".to_string(),
        &facts,
        bindings,
        logical_modules,
        HashMap::new(),
        ModuleId::logical(residual_idx),
    )
}

fn factorization_with_residual_module(
    source: &str,
    residual_bindings: &[&str],
    logical_bindings: &[&str],
) -> ChunkFactorization {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let residual = logical(0);
    let logical = logical(1);
    let mut bindings = HashMap::new();
    for name in residual_bindings {
        bindings.insert(test_id(name), BindingKind::Owned { module: residual });
    }
    for name in logical_bindings {
        bindings.insert(test_id(name), BindingKind::Owned { module: logical });
    }
    let logical_modules = vec![
        LogicalModule {
            id: "residual".to_string(),
            target_file: "residual/unhandled.js".to_string(),
            residual: true,
            rename_map: HashMap::new(),
            anonymous_statement_ordinals: Vec::new(),
        },
        LogicalModule {
            id: "mod_1".to_string(),
            target_file: "mod_1.js".to_string(),
            residual: false,
            rename_map: HashMap::new(),
            anonymous_statement_ordinals: Vec::new(),
        },
    ];
    ChunkFactorization::build(
        "test_chunk".to_string(),
        &facts,
        bindings,
        logical_modules,
        HashMap::new(),
        residual,
    )
}

// --- Owner graph quotient ------------------------------------------------

#[test]
fn owner_graph_retains_reads_to_unassigned_declared_bindings() {
    let factorization = factorization_for("const A = X + 1; const X = 42;", &[("A", logical(0))]);

    assert!(
        factorization
            .analysis
            .owner_graph()
            .iter_edges()
            .any(|edge| {
                edge.from == OwnerId(0)
                    && edge.to == OwnerId(1)
                    && edge.reason.kind() == DepKind::EagerUse
                    && edge.reason.statement_ordinal() == StatementOrdinal(0)
                    && edge.reason.binding().is_some_and(|id| id.0 == "X")
            }),
        "owner graph should retain the unassigned declared provider edge",
    );
    // The residual is the synthesized logical module at index 1
    // (after the explicit `mod_0` at index 0).
    assert!(
        factorization
            .dep_graph
            .contains_edge(logical(0), ModuleId::logical(1)),
        "the quotient should expose the logical-module -> residual read",
    );

    let report = factorization.owner_graph_report();
    let residual_owner = report
        .nodes
        .iter()
        .find(|node| node.id == "owner:1")
        .expect("X owner should be reported");
    assert!(
        report.is_residual(&residual_owner.destination),
        "residual owner should land on the synthesized residual module: {:?}",
        residual_owner.destination,
    );
}

#[test]
fn owner_graph_report_emits_atomic_graph_not_heuristic_peel_fields() {
    let factorization = factorization_with_residual_module(
        "const Leaf = 1; const ResidualUse = Leaf + 1; const Existing = ResidualUse + 1;",
        &["Leaf", "ResidualUse"],
        &["Existing"],
    );

    let report = factorization.owner_graph_report();
    assert_eq!(report.atomic_graph.nodes.len(), 3);
    assert!(
        report
            .atomic_graph
            .nodes
            .iter()
            .any(
                |unit| member_bindings(&unit.members) == vec!["Leaf".to_string()]
                    && unit
                        .destinations
                        .iter()
                        .any(|destination| report.is_residual(destination))
            ),
        "Leaf should appear as a residual atomic unit: {:#?}",
        report.atomic_graph,
    );
    let json = serde_json::to_string(&report).expect("serialize OwnerGraphReport");
    assert!(json.contains(r#""atomic_graph""#));
    assert!(!json.contains(r#""peelability""#));
    assert!(!json.contains(r#""peel_proposals""#));
}

#[test]
fn atomic_graph_excludes_lazy_use_edges() {
    let factorization = factorization_for("function Leaf() { return Dep; } const Dep = 1;", &[]);

    let report = factorization.owner_graph_report();
    assert_eq!(report.atomic_graph.nodes.len(), 2);
    assert!(
        report.atomic_graph.edges.is_empty(),
        "lazy-only function body reads should not create atomic DAG edges: {:#?}",
        report.atomic_graph,
    );
}

#[test]
fn atomic_graph_collapses_constraining_eager_cycle() {
    let factorization =
        factorization_with_residual_module("const A = B + 1; const B = A + 1;", &["A", "B"], &[]);

    let report = factorization.owner_graph_report();
    let cycle_unit = report
        .atomic_graph
        .nodes
        .iter()
        .find(|unit| member_bindings(&unit.members) == vec!["A".to_string(), "B".to_string()])
        .expect("A/B eager cycle should be one atomic unit");
    assert_eq!(cycle_unit.owner_ids.len(), 2);
    assert!(cycle_unit.causes.contains(&DepKind::EagerUse));
}

#[test]
fn atomic_graph_preserves_direction_between_atomic_units() {
    let factorization = factorization_with_residual_module(
        "const Leaf = 1; const ResidualUse = Leaf + 1;",
        &["Leaf", "ResidualUse"],
        &[],
    );

    let report = factorization.owner_graph_report();
    assert_eq!(report.atomic_graph.nodes.len(), 2);
    assert_eq!(report.atomic_graph.edges.len(), 1);
    let edge = &report.atomic_graph.edges[0];
    assert_ne!(edge.source, edge.target);
    assert_eq!(edge.edge_kinds, vec![DepKind::EagerUse]);
    assert!(edge.constrains_init_order);
}

// --- has_side_effect refinement ------------------------------------------

fn has_side_effect_for(src: &str) -> Vec<bool> {
    let module = parse(src);
    analyze_facts(&module)
        .into_iter()
        .map(|f| !f.purity.is_pure())
        .collect()
}

#[test]
fn pure_const_decl_is_not_side_effecting() {
    assert_eq!(has_side_effect_for("const X = 42;"), vec![false]);
    assert_eq!(has_side_effect_for("const X = { a: 1 };"), vec![false]);
    assert_eq!(has_side_effect_for("const X = [1, 2, 3];"), vec![false]);
    assert_eq!(has_side_effect_for("const X = OTHER;"), vec![false]);
    assert_eq!(has_side_effect_for("const X = 1 + 2;"), vec![false]);
    // `A + B` on opaque Idents runs ToPrimitive — possible user
    // `valueOf` — and is side-effecting under the coercing-operator
    // gate (see purity classifier_tests).
    assert_eq!(has_side_effect_for("const X = A + B;"), vec![true]);
}

#[test]
fn impure_const_decl_is_side_effecting() {
    assert_eq!(has_side_effect_for("const X = compute();"), vec![true]);
    assert_eq!(has_side_effect_for("const X = new Foo();"), vec![true]);
    assert_eq!(has_side_effect_for("const X = (y = 1, y);"), vec![true]);
}

#[test]
fn ts_enum_iife_var_decl_is_not_side_effecting_for_matching_binding() {
    assert_eq!(
        has_side_effect_for(
            r#"var WL = ((n) => (n.NO_SOUND = "no-sound", n.YES = "yes", n))(WL || {});"#
        ),
        vec![false]
    );
    assert_eq!(
        has_side_effect_for(r#"var E = ((n) => (n[(n.A = 0)] = "A", n))(E || {});"#),
        vec![false]
    );
}

#[test]
fn ts_enum_iife_var_decl_rejects_unsafe_shapes_as_side_effecting() {
    for source in [
        r#"var X = ((p) => (p.A = io(), p))(X || {});"#,
        r#"var X = ((p) => (globalThis.A = "a", p))(X || {});"#,
        r#"var X = ((p) => (other.A = "a", p))(X || {});"#,
        r#"var X = ((p) => (p[key] = "a", p))(X || {});"#,
        r#"var X = ((p) => (p.A = value, p))(X || {});"#,
        r#"var X = ((p) => (leaked = p, p))(X || {});"#,
        r#"var X = ((p) => (p.A = "a", p))(Other || {});"#,
    ] {
        assert_eq!(has_side_effect_for(source), vec![true], "{source}");
    }
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
    analyze_facts(&module).into_iter().map(|f| f.kind).collect()
}

fn declared_per_statement(source: &str) -> Vec<Vec<String>> {
    let module = parse(source);
    analyze_facts(&module)
        .into_iter()
        .map(|f| f.declared.into_iter().map(|id| id.0.to_string()).collect())
        .collect()
}

fn owner_for_binding(graph: &OwnerGraph, name: &str) -> OwnerId {
    graph
        .iter_nodes()
        .find(|node| node.declared.iter().any(|id| id.0.as_ref() == name))
        .map(|node| node.id)
        .unwrap_or_else(|| panic!("binding {name} should have an owner"))
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
fn split_mixed_purity_const_gives_each_declarator_its_own_owner() {
    let source = r#"class Something {}
const impure = new Something(), pureBrand = Symbol("Brand");"#;
    assert_eq!(
        declared_per_statement(source),
        vec![
            vec!["Something".to_string()],
            vec!["impure".to_string()],
            vec!["pureBrand".to_string()],
        ],
    );

    let module = parse(source);
    let facts = analyze_facts(&module);
    let graph = build_owner_graph(&facts).unwrap();
    let impure_owner = owner_for_binding(&graph, "impure");
    let brand_owner = owner_for_binding(&graph, "pureBrand");
    assert_ne!(
        impure_owner, brand_owner,
        "comma-list declarators must become distinct owners",
    );
    assert!(
        !graph.node(impure_owner).unwrap().purity.is_pure(),
        "`new Something()` should remain impure after splitting",
    );
    assert!(
        graph.node(brand_owner).unwrap().purity.is_pure(),
        "`Symbol(\"Brand\")` should classify pure after splitting",
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
fn split_comma_list_attributes_later_declarator_read_to_later_owner() {
    let module = parse(r#"const first = Symbol("First"), second = first;"#);
    let facts = analyze_facts(&module);
    let graph = build_owner_graph(&facts).unwrap();
    let first_owner = owner_for_binding(&graph, "first");
    let second_owner = owner_for_binding(&graph, "second");
    let first_binding = test_id("first");
    let edge = graph
        .iter_edges()
        .find(|edge| {
            edge.from == second_owner
                && edge.to == first_owner
                && edge.reason.kind() == DepKind::EagerUse
                && edge.reason.binding() == Some(&first_binding)
        })
        .expect("second's initializer should eagerly read first");
    assert_eq!(
        edge.reason.statement_ordinal(),
        graph.node(second_owner).unwrap().statement_ordinal,
        "the read edge must be attributed to the later declarator's owner",
    );
}

#[test]
fn split_comma_list_rebind_unit_sticks_to_mutable_declarator_only() {
    let module = parse(
        r#"let mutable = 1, peer = Symbol("Peer");
mutable = mutable + 1;"#,
    );
    let facts = analyze_facts(&module);
    let graph = build_owner_graph(&facts).unwrap();
    let mutable_owner = owner_for_binding(&graph, "mutable");
    let peer_owner = owner_for_binding(&graph, "peer");
    let assign_owner = OwnerId(2);
    let units = compute_atomic_units(&graph);
    assert_partitions_all_owners(&units, 3);

    let mutable_unit = units
        .iter()
        .find(|unit| unit.members.contains(&mutable_owner))
        .expect("mutable owner should appear in an atomic unit");
    assert!(
        mutable_unit.members.contains(&assign_owner),
        "mutable declarator must co-locate with its rebinding assignment: {units:?}",
    );
    assert!(
        !mutable_unit.members.contains(&peer_owner),
        "independent split sibling must not be pulled into the mutable rebind unit: {units:?}",
    );

    let peer_unit = units
        .iter()
        .find(|unit| unit.members.contains(&peer_owner))
        .expect("peer owner should appear in an atomic unit");
    assert_eq!(
        peer_unit.members.len(),
        1,
        "pure split sibling should remain independently peelable",
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

// --- Comma-list owner attribution in owner graph quotient ---------------

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
    let factorization = factorization_for(
        "const A = 1, B = X; const X = 42;",
        &[("A", logical(0)), ("B", logical(1)), ("X", logical(1))],
    );
    // No cross-module read edges should exist: A's init is
    // pure, B reads X (same module).
    let mod_0 = ModuleId(LogicalModuleIndex(0));
    let mod_1 = ModuleId(LogicalModuleIndex(1));
    assert!(
        !factorization.dep_graph.contains_edge(mod_0, mod_1),
        "no edge mod_0 → mod_1 expected, got: {:?}",
        factorization.dep_graph.edge_weight(mod_0, mod_1),
    );
    assert!(
        !factorization.dep_graph.contains_edge(mod_1, mod_0),
        "no edge mod_1 → mod_0 expected, got: {:?}",
        factorization.dep_graph.edge_weight(mod_1, mod_0),
    );
}

#[test]
fn split_comma_list_assigns_per_declarator_source_ranges() {
    // Multi-declarator var statement spread across three source
    // lines. After Bucket-F splits the comma list, each resulting
    // single-declarator owner must report just its declarator's
    // line range — not the full parent statement's range. The
    // factorizer's `size_lines_estimate` and the lane workers'
    // `body_extraction` per-owner snippets both rely on this.
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let source = "const A = 1,\n      B = 2,\n      C = 3;\n";
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
    let module = Parser::new_from(lexer).parse_module().unwrap();
    let cm_clone = cm.clone();
    let line_range_for_span = move |span: swc_common::Span| -> Option<(usize, usize)> {
        if span == swc_common::DUMMY_SP {
            return None;
        }
        let lo = cm_clone.lookup_char_pos(span.lo()).line;
        let hi = cm_clone.lookup_char_pos(span.hi()).line;
        Some((lo, hi))
    };
    let analysis = analyze_chunk(
        &module,
        &AnalysisHints::default(),
        Some("test.js"),
        line_range_for_span,
    );
    assert_eq!(analysis.facts.len(), 3);
    let lines: Vec<(usize, usize)> = analysis
        .facts
        .iter()
        .map(|f| {
            let loc = f
                .source_location
                .as_ref()
                .expect("source_location should be populated");
            (loc.start_line, loc.end_line)
        })
        .collect();
    assert_eq!(
        lines,
        vec![(1, 1), (2, 2), (3, 3)],
        "each declarator should report only its own line, got {lines:?}",
    );
}

#[test]
fn split_export_comma_list_assigns_per_declarator_source_ranges() {
    // Same fix applies to `export const A = 1, B = 2;`: the
    // outer `ExportDecl` wrapper's span gets replaced by the
    // declarator's span on each post-split item.
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let source = "export const A = 1,\n             B = 2,\n             C = 3;\n";
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
    let module = Parser::new_from(lexer).parse_module().unwrap();
    let cm_clone = cm.clone();
    let line_range_for_span = move |span: swc_common::Span| -> Option<(usize, usize)> {
        if span == swc_common::DUMMY_SP {
            return None;
        }
        let lo = cm_clone.lookup_char_pos(span.lo()).line;
        let hi = cm_clone.lookup_char_pos(span.hi()).line;
        Some((lo, hi))
    };
    let analysis = analyze_chunk(
        &module,
        &AnalysisHints::default(),
        Some("test.js"),
        line_range_for_span,
    );
    let lines: Vec<(usize, usize)> = analysis
        .facts
        .iter()
        .map(|f| {
            let loc = f
                .source_location
                .as_ref()
                .expect("source_location should be populated");
            (loc.start_line, loc.end_line)
        })
        .collect();
    assert_eq!(
        lines,
        vec![(1, 1), (2, 2), (3, 3)],
        "each exported declarator should report only its own line, got {lines:?}",
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
    let factorization = factorization_for(
        "const A = X, B = 1; const X = 42; const Y = A;",
        &[
            ("A", logical(0)),
            ("B", logical(1)),
            ("X", logical(1)),
            ("Y", logical(1)),
        ],
    );
    let report = factorization.validate();
    assert!(
        !report.cycles.is_empty(),
        "expected a real cycle to be reported"
    );
}

// --- linker_order in FactorizationReport --------------------------------------

#[test]
fn validate_surfaces_linker_order_for_acyclic_spec() {
    // mod_0 reads B from mod_1 at-init → mod_1 must precede
    // mod_0 in the linker's evaluation order.
    let factorization = factorization_for(
        "const A = B + 1; const B = 42;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = factorization.validate();
    let order = &report.linker_order;
    let pos = |name: &str| -> usize {
        order
            .iter()
            .position(|m| m.as_str() == name)
            .unwrap_or_else(|| panic!("module {name} not in {order:?}"))
    };
    assert!(
        pos("mod_1") < pos("mod_0"),
        "mod_1 must precede mod_0 in linker_order; got {order:?}",
    );
}

#[test]
fn validate_returns_empty_linker_order_for_cyclic_spec() {
    // Genuine cross-module constraining cycle: `A = B + 1` and
    // `B = A + 1` both read at-init. After the relaxed-predicate
    // routing of the validator (docs/design.md "Realizability
    // primitive"), the case has to actually produce a cycle in
    // the constraining-edge subgraph — mutual at-init reads do.
    let factorization = factorization_for(
        "const A = B + 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = factorization.validate();
    assert!(!report.cycles.is_empty(), "expected a cycle in {report:?}",);
    assert!(
        report.linker_order.is_empty(),
        "linker_order must be empty when the dep graph is cyclic; got {:?}",
        report.linker_order,
    );
}

// --- Atomic units ---------------------------------------------------------

fn atomic_units_for(source: &str) -> Vec<AtomicUnit> {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let owner_graph = build_owner_graph(&facts).unwrap();
    compute_atomic_units(&owner_graph)
}

fn unit_sizes(units: &[AtomicUnit]) -> Vec<usize> {
    let mut sizes: Vec<usize> = units.iter().map(|u| u.members.len()).collect();
    sizes.sort_unstable();
    sizes
}

fn assert_partitions_all_owners(units: &[AtomicUnit], total_owners: usize) {
    let summed: usize = units.iter().map(|u| u.members.len()).sum();
    assert_eq!(
        summed, total_owners,
        "atomic units must cover every owner exactly once; got units {units:?}",
    );
    let mut seen = BTreeSet::new();
    for unit in units {
        for owner in &unit.members {
            assert!(
                seen.insert(*owner),
                "owner {owner:?} appears in more than one atomic unit",
            );
        }
    }
}

#[test]
fn atomic_units_singletons_for_independent_owners() {
    // No edges → each owner is its own atomic unit.
    let units = atomic_units_for("const A = 1; const B = 2; const C = 3;");
    assert_partitions_all_owners(&units, 3);
    assert_eq!(unit_sizes(&units), vec![1, 1, 1]);
}

#[test]
fn atomic_units_eager_use_chain_stays_split() {
    // A → B → C via EagerUse: directed-only edges, no cycle, so
    // each owner remains its own unit.
    let units = atomic_units_for("const C = 3; const B = C + 1; const A = B + 1;");
    assert_partitions_all_owners(&units, 3);
    assert_eq!(unit_sizes(&units), vec![1, 1, 1]);
}

#[test]
fn atomic_units_eager_use_cycle_merges() {
    // A and B form an EagerUse cycle → must co-locate.
    let units = atomic_units_for("const A = B + 1; const B = A + 1;");
    assert_partitions_all_owners(&units, 2);
    assert_eq!(unit_sizes(&units), vec![2]);
}

#[test]
fn atomic_units_lazy_use_cycle_stays_split() {
    // Two functions that reference each other lazily plus their
    // bindings — no constraining edges, so all four owners are
    // independent units.
    let units = atomic_units_for(
        "function helperA() { return B; } function helperB() { return A; } const A = 1; const B = 2;",
    );
    assert_partitions_all_owners(&units, 4);
    assert_eq!(unit_sizes(&units), vec![1, 1, 1, 1]);
}

#[test]
fn atomic_units_sequenced_chain_stays_split() {
    // Three side-effecting top-level statements form a directed
    // Sequenced chain. A directed source-order edge alone is
    // satisfiable by linker order — no co-location forced — so
    // every owner stays in its own atomic unit. Co-location
    // would only kick in if some non-Sequenced edge ran in the
    // reverse direction.
    let units = atomic_units_for(
        r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
    );
    assert_partitions_all_owners(&units, 3);
    assert_eq!(unit_sizes(&units), vec![1, 1, 1]);
}

#[test]
fn atomic_units_sequenced_plus_reverse_eager_merges() {
    // A Sequenced source-order edge in one direction plus an
    // EagerUse read in the reverse direction forms an SCC in
    // `G_atomic` and forces co-location.
    // `const A = 1;` is pure (no side effect, no Sequenced edge);
    // `const x = (globalThis.tag = "x", A);` is side-effecting AND
    // eagerly reads `A`. The eager read draws `x → A`; the
    // Sequenced edge from the next side-effect (`const y = ...`)
    // gives `y → x`. Eager `y → A` adds `y → A` too. So {x, y}
    // ends up merged only if some edge reverses through A. Use a
    // shape that produces a real cycle:
    // `let A = 1; A = (globalThis.tag = "x", 2); A = (globalThis.tag = "y", 3);`
    // — top-level Sequenced + EagerRebind force {A, stmt_1, stmt_2}
    // into one unit (Rebind bidirectional + Sequenced directed
    // form a cycle).
    let units = atomic_units_for(
        r#"let A = 1; A = (globalThis.tag = "x", 2); A = (globalThis.tag = "y", 3);"#,
    );
    assert_partitions_all_owners(&units, 3);
    assert_eq!(unit_sizes(&units), vec![3]);
}

#[test]
fn atomic_units_lazy_rebind_merges() {
    // `let A = 0; function B() { A = 1; }` produces a LazyRebind
    // edge from B → A. LazyRebind is bidirectional in `G_atomic`,
    // so A and B collapse into one unit.
    let units = atomic_units_for("let A = 0; function B() { A = 1; }");
    assert_partitions_all_owners(&units, 2);
    assert_eq!(unit_sizes(&units), vec![2]);
}

// --- Factor assembly -----------------------------------------------------

fn partition_summary(factorization: &ChunkFactorization) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = factorization
        .analysis
        .owner_graph()
        .iter_nodes()
        .map(|node| {
            let declared: Vec<String> = node.declared.iter().map(|id| id.0.to_string()).collect();
            let key = if declared.is_empty() {
                format!("stmt_{}", node.statement_ordinal.0)
            } else {
                declared.join(",")
            };
            let dest = factorization.partition.of(node.id);
            // Residual modules render as `<residual>` so this
            // summary stays stable across residual-index changes;
            // explicit modules use their `mod_<idx>` label.
            let LogicalModuleIndex(idx) = dest.0;
            let label = match factorization
                .analysis
                .logical_module(LogicalModuleIndex(idx))
            {
                Some(m) if m.residual => "<residual>".to_string(),
                _ => render(dest).to_string(),
            };
            (key, label)
        })
        .collect();
    out.sort();
    out
}

#[test]
fn factor_assembly_unclaimed_owners_default_to_residual() {
    let factorization = factorization_for("const A = 1; const B = 2;", &[]);
    let summary = partition_summary(&factorization);
    assert_eq!(
        summary,
        vec![
            ("A".to_string(), "<residual>".to_string()),
            ("B".to_string(), "<residual>".to_string()),
        ],
    );
}

#[test]
fn factor_assembly_single_claim_with_unclaimed_unit_members_is_a_conflict() {
    // `const A = B + 1; const B = A + 1;` is one EagerUse cycle —
    // a single atomic unit. Claiming A for mod_0 leaves B
    // defaulting to residual entry, which splits the unit
    // across {mod_0, <residual_entry>} — unrealizable. The spec
    // author needs to either also assign B (or leave both
    // unassigned), or remove the constraining edge that fused
    // them in the first place. `debundle coverage` may flag the
    // split unit as an advisory edit, but factor_assembly refuses
    // to silently move B for the user.
    let factorization =
        factorization_for("const A = B + 1; const B = A + 1;", &[("A", logical(0))]);
    let report = factorization.validate();
    assert_eq!(
        report.atomic_unit_conflicts.len(),
        1,
        "expected the half-claimed EagerUse cycle to surface as an atomic-unit conflict: {report:?}",
    );
    // Residual is the synthesized logical module appended after
    // the explicit `mod_0`.
    assert_eq!(
        distinct_claim_modules(&report.atomic_unit_conflicts[0]),
        vec![ModuleId::logical(0), ModuleId::logical(1)],
    );
}

#[test]
fn factor_assembly_concordant_claims_within_unit_are_fine() {
    // Both members of the same atomic unit claimed for the same
    // module — that's the spec author being explicit, not a
    // conflict.
    let factorization = factorization_for(
        "const A = B + 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(0))],
    );
    let summary = partition_summary(&factorization);
    assert_eq!(
        summary,
        vec![
            ("A".to_string(), "mod_0".to_string()),
            ("B".to_string(), "mod_0".to_string()),
        ],
    );
}

#[test]
fn factor_assembly_records_conflict_on_split_eager_use_cycle() {
    let factorization = factorization_for(
        "const A = B + 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = factorization.validate();
    assert_eq!(
        report.atomic_unit_conflicts.len(),
        1,
        "expected the A↔B EagerUse cycle to surface as an atomic-unit conflict: {report:?}",
    );
    let conflict = &report.atomic_unit_conflicts[0];
    assert_eq!(
        distinct_claim_modules(conflict),
        vec![ModuleId::logical(0), ModuleId::logical(1)],
    );
}

#[test]
fn factor_assembly_records_no_conflict_for_sequenced_only_chain() {
    // Three side-effect statements, source-ordered. Directed
    // Sequenced edges alone form a chain in `G_atomic`, not an
    // SCC, so every owner is its own atomic unit and the spec
    // can split them across modules without violating
    // co-location. The validator may still flag a module-level
    // cycle when the spec creates one through reverse claims,
    // but factor_assembly itself does not panic / record a
    // conflict here.
    let factorization = factorization_for(
        r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
        &[("a1", logical(0)), ("b1", logical(1)), ("a2", logical(0))],
    );
    let report = factorization.validate();
    assert!(
        report.atomic_unit_conflicts.is_empty(),
        "Sequenced-only chains never force co-location: {report:?}",
    );
}

#[test]
fn factor_assembly_independent_owners_keep_independent_claims() {
    // Three eager-use chain: A → B → C, no cycle, three atomic
    // units. Each owner's claim takes effect independently.
    let factorization = factorization_for(
        "const C = 3; const B = C + 1; const A = B + 1;",
        &[("A", logical(0)), ("B", logical(1)), ("C", logical(0))],
    );
    let summary = partition_summary(&factorization);
    assert_eq!(
        summary,
        vec![
            ("A".to_string(), "mod_0".to_string()),
            ("B".to_string(), "mod_1".to_string()),
            ("C".to_string(), "mod_0".to_string()),
        ],
    );
}

#[test]
fn factorization_report_serializes_linker_order_as_snake_case() {
    let factorization = factorization_for(
        "const A = 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = factorization.validate();
    let json = serde_json::to_string(&report).expect("serialize FactorizationReport");
    assert!(
        json.contains(r#""linker_order""#),
        "FactorizationReport must serialize linker_order as `linker_order`; got: {json}",
    );
}
