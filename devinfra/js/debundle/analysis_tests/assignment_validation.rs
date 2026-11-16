//! Focused atomic-group, factor-assembly, purity and source-location tests.
//! Acceptance, rejection and runtime ordering are exercised through the CLI
//! in e2e/realizability_test.rs and the at_init_* and lemma_* suites.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use super::{analyze_facts, build_dependency_graph, parse, parse_with_source_map, test_id};
use crate::*;
use analysis::*;
use analysis_model::DepKind;
use source_identity::ChunkId;

#[derive(Debug, Clone, Copy)]
enum TestDestination {
    Planned(ModuleId),
    Entry,
}

/// Test-only convenience for constructing a planned output identity.
/// Fixtures declare no author inputs, so reports render no spec locations.
fn dependency_graph_report(assignment_analysis: &AssignmentAnalysis) -> DependencyGraphReport {
    static NO_OPTIONS: BTreeMap<String, spec::DependencyGraphOptions> = BTreeMap::new();
    assignment_analysis.dependency_graph_report(
        "../program_graph.json.gz".into(),
        &analysis::reports::author_inputs::AuthorInputLocations::new(None, &NO_OPTIONS)
            .expect("no author inputs need no provenance"),
    )
}

fn dependency_graph_report_for_facts(facts: &[StatementFacts]) -> DependencyGraphReport {
    let assignment_analysis = assignment_analysis_from_facts(
        facts,
        HashMap::new(),
        Vec::new(),
        EntryDestination::after_planned_modules(0),
    );
    dependency_graph_report(&assignment_analysis)
}

#[test]
fn report_graph_restores_declarations_from_the_matching_source_census() {
    let source = "import { value as imported } from './dep.js'; const first = imported, second = 2; var repeated = first; var repeated = second; function readRepeated() { return repeated; } console.log(readRepeated());";
    let module = parse(source);
    let structural_facts = analyze_chunk_structural(&module, None).per_statement;
    let semantic_facts = analyze_facts(&module);
    let mut report = dependency_graph_report_for_facts(&semantic_facts);
    report.nodes.reverse();
    let reported_ids: Vec<_> = report.nodes.iter().map(|node| node.id.clone()).collect();
    let reported_ordinals: Vec<_> = report
        .nodes
        .iter()
        .map(|node| node.statement_ordinal)
        .collect();
    assert_eq!(reported_ordinals.len(), structural_facts.len());
    assert!(
        structural_facts
            .iter()
            .any(|fact| fact.kind == StatementKind::Import),
        "the source-only census retains import statements"
    );
    for binding in ["first", "second"] {
        assert_eq!(
            structural_facts
                .iter()
                .filter(|fact| {
                    fact.declared
                        .iter()
                        .any(|declared| declared.0.as_ref() == binding)
                })
                .count(),
            1,
            "split declarator {binding} has its own source unit"
        );
    }
    assert_eq!(
        structural_facts
            .iter()
            .filter(|fact| {
                fact.declared
                    .iter()
                    .any(|declared| declared.0.as_ref() == "repeated")
            })
            .count(),
        2,
        "repeated var declaration sites remain separate source units"
    );

    let input = DependencyGraphInput::from_report(report.clone()).expect("report graph");
    let shared_graph = input.shared_graph();
    let restored = input
        .with_source_declarations(&structural_facts)
        .expect("matching parsed source census");

    assert_eq!(restored.graph().num_nodes(), structural_facts.len());
    assert_eq!(restored.report().nodes.len(), reported_ids.len());
    assert_eq!(
        restored
            .report()
            .nodes
            .iter()
            .map(|node| node.id.clone())
            .collect::<Vec<_>>(),
        reported_ids
    );
    for (row, node) in restored.graph().iter_nodes().enumerate() {
        assert_eq!(
            node.statement_ordinal,
            restored.report().nodes[row].statement_ordinal,
            "graph IDs remain in report row order"
        );
        let source_fact = structural_facts
            .iter()
            .find(|fact| fact.ordinal == node.statement_ordinal)
            .expect("report ordinal joins source census");
        assert_eq!(node.declared, source_fact.declared);
    }
    assert!(
        shared_graph
            .iter_nodes()
            .all(|node| node.declared.is_empty()),
        "copy-on-write restoration must not mutate other graph readers"
    );

    let unshared = DependencyGraphInput::from_report(report)
        .expect("report graph")
        .with_source_declarations(&structural_facts)
        .expect("unshared graph restores the source census");
    assert_eq!(unshared.graph().num_nodes(), structural_facts.len());
}

#[test]
fn report_graph_rejects_non_bijective_or_inconsistent_source_censuses() {
    let module = parse(
        "import { value as imported } from './dep.js'; const alpha = imported; function readAlpha() { return alpha; } alpha;",
    );
    let facts = analyze_chunk_structural(&module, None).per_statement;
    let report = dependency_graph_report_for_facts(&analyze_facts(&module));
    let assert_rejected = |report: DependencyGraphReport, facts: &[StructuralStatementFacts]| {
        let input = DependencyGraphInput::from_report(report).expect("valid report topology");
        assert!(input.with_source_declarations(facts).is_err());
    };

    assert_rejected(report.clone(), &facts[..facts.len() - 1]);

    let mut duplicate_fact = facts.clone();
    duplicate_fact.push(facts[0].clone());
    assert_rejected(report.clone(), &duplicate_fact);

    let mut changed_binding = facts.clone();
    let declaration = changed_binding
        .iter_mut()
        .find(|fact| !fact.declared.is_empty())
        .expect("fixture declaration");
    let original = declaration.declared.pop_first().expect("binding");
    declaration
        .declared
        .insert(("changedName".into(), original.1));
    assert_rejected(report.clone(), &changed_binding);

    let mut changed_kind = facts.clone();
    changed_kind[0].kind = StatementKind::FnDecl;
    assert_rejected(report.clone(), &changed_kind);

    let mut changed_report_binding = report.clone();
    let declaration = changed_report_binding
        .nodes
        .iter_mut()
        .find(|node| !node.declared_bindings.is_empty())
        .expect("report declaration");
    declaration.declared_bindings[0].binding = "changedName".into();
    assert_rejected(changed_report_binding, &facts);

    let mut duplicate_report_ordinal = report;
    duplicate_report_ordinal.nodes[1].statement_ordinal =
        duplicate_report_ordinal.nodes[0].statement_ordinal;
    assert_rejected(duplicate_report_ordinal, &facts);
}

fn logical(idx: usize) -> TestDestination {
    TestDestination::Planned(ModuleId(idx))
}

fn render(id: ModuleId) -> spec::ModulePath {
    spec::ModulePath::parse(&format!("mod_{}", id.0), "").unwrap()
}

fn member_bindings(members: &[BindingReport]) -> Vec<String> {
    members
        .iter()
        .map(|member| member.binding.to_string())
        .collect()
}

// --- Lazy rebind atomic-group constraints --------------------------------

/// LazyRebind atomic-group split: declarer and assigner of a
/// mutable binding must materialize together. `assignment_assembly`
/// records this as an `atomic_group_conflicts` entry on the
/// assignment analysis; the materializer bails on any non-empty list.
#[test]
fn cross_destination_lazy_write_is_rejected() {
    let assignment_analysis = assignment_analysis_for(
        "let A = 0; function B() { A = 1; }",
        &[("A", logical(0)), ("B", TestDestination::Entry)],
    );
    let report = assignment_analysis.validate();
    assert_eq!(
        report.atomic_group_conflicts.len(),
        1,
        "expected one atomic-group conflict (A and B share a LazyRebind atomic group but the spec splits them): {report:?}",
    );
    let conflict = &report.atomic_group_conflicts[0];
    // The entry destination follows the one planned output module.
    assert_eq!(
        distinct_claim_modules(conflict),
        vec![ModuleId(0), ModuleId(1)],
    );
}

/// Sorted distinct destination modules across a conflict's claims —
/// the typed equivalent of the prior string-rendered
/// `conflicting_modules` field on `AtomicGroupConflictReport`.
fn distinct_claim_modules(conflict: &AtomicGroupConflict) -> Vec<ModuleId> {
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
    let assignment_analysis = assignment_analysis_for(
        "let A = 0; function B() { A = 1; }",
        &[("A", logical(0)), ("B", logical(0))],
    );

    let report = assignment_analysis.validate();
    assert!(
        report.atomic_group_conflicts.is_empty(),
        "same-destination rebinding writes should stay local to the emitted module: {report:?}",
    );
}

// --- Assignment-analysis helpers -----------------------------------------------

fn assignment_analysis_from_facts(
    facts: &[StatementFacts],
    bindings: HashMap<swc_ecma_ast::Id, BindingKind>,
    planned_modules: Vec<PlannedModule>,
    entry_destination: EntryDestination,
) -> AssignmentAnalysis {
    AssignmentAnalysis::build_with(
        "test_chunk".to_string(),
        ChunkId(0),
        compute_dependency_graph_and_atomic_groups_with(facts, GraphOptions::default(), &[])
            .expect("chunk facts declare a binding twice"),
        BindingCatalogue::new(ChunkId(0), bindings),
        planned_modules,
        HashMap::new(),
        entry_destination,
    )
    .expect("test bindings belong to test_chunk")
}

#[test]
fn assignment_analysis_rejects_a_catalogue_from_another_chunk() {
    let result = AssignmentAnalysis::build_with(
        "diagnostic_chunk_name".to_string(),
        ChunkId(1),
        compute_dependency_graph_and_atomic_groups_with(&[], GraphOptions::default(), &[])
            .expect("empty facts are valid"),
        BindingCatalogue::new(ChunkId(0), HashMap::new()),
        Vec::new(),
        HashMap::new(),
        EntryDestination::after_planned_modules(0),
    );

    assert!(result.is_err(), "wrong-chunk catalogues must be rejected");
}

fn assignment_analysis_for(
    source: &str,
    ownership: &[(&str, TestDestination)],
) -> AssignmentAnalysis {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let max_idx = ownership
        .iter()
        .filter_map(|(_, destination)| match destination {
            TestDestination::Planned(id) => Some(id.0),
            TestDestination::Entry => None,
        })
        .max();
    let planned_module_count = max_idx.map_or(0, |i| i + 1);
    let entry_destination = EntryDestination::after_planned_modules(planned_module_count);
    let mut bindings = HashMap::new();
    for (name, destination) in ownership {
        let module_id = match destination {
            TestDestination::Planned(id) => *id,
            TestDestination::Entry => entry_destination.module_id(),
        };
        bindings.insert(test_id(name), BindingKind::Owned { module: module_id });
    }
    let planned_modules = (0..planned_module_count)
        .map(|i| PlannedModule {
            id: format!("mod_{i}"),
            target_file: format!("mod_{i}.js"),
            role: ModuleRole::Specified,
            rename_map: HashMap::new(),
            anonymous_source_unit_ordinals: Vec::new(),
        })
        .collect();
    assignment_analysis_from_facts(&facts, bindings, planned_modules, entry_destination)
}

fn assignment_analysis_with_unassigned_module(
    source: &str,
    residual_bindings: &[&str],
    logical_bindings: &[&str],
) -> AssignmentAnalysis {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let residual = ModuleId(0);
    let logical = ModuleId(1);
    let mut bindings = HashMap::new();
    for name in residual_bindings {
        bindings.insert(test_id(name), BindingKind::Owned { module: residual });
    }
    for name in logical_bindings {
        bindings.insert(test_id(name), BindingKind::Owned { module: logical });
    }
    let logical_modules = vec![
        PlannedModule {
            id: "residual".to_string(),
            target_file: "residual/unhandled.js".to_string(),
            role: ModuleRole::Unassigned,
            rename_map: HashMap::new(),
            anonymous_source_unit_ordinals: Vec::new(),
        },
        PlannedModule {
            id: "mod_1".to_string(),
            target_file: "mod_1.js".to_string(),
            role: ModuleRole::Specified,
            rename_map: HashMap::new(),
            anonymous_source_unit_ordinals: Vec::new(),
        },
    ];
    let entry_destination = EntryDestination::after_planned_modules(logical_modules.len());
    assignment_analysis_from_facts(&facts, bindings, logical_modules, entry_destination)
}

// --- dependency graph quotient ------------------------------------------------

#[test]
fn empty_dependency_graph_report_includes_entry_destination() {
    let assignment_analysis = assignment_analysis_from_facts(
        &[],
        HashMap::new(),
        Vec::new(),
        EntryDestination::after_planned_modules(0),
    );

    let report = dependency_graph_report(&assignment_analysis);
    assert_eq!(report.quotient.nodes.len(), 1);
    let entry = &report.quotient.nodes[0];
    assert_eq!(entry.key.as_str(), "logical:0");
    assert_eq!(entry.path.as_str(), "anon_residual_sentinel");
    assert_eq!(entry.kind, ModuleDestinationKind::Entry);
}

#[test]
fn dependency_graph_report_emits_atomic_graph() {
    let assignment_analysis = assignment_analysis_with_unassigned_module(
        "const Leaf = 1; const ResidualUse = Leaf + 1; const Existing = ResidualUse + 1;",
        &["Leaf", "ResidualUse"],
        &["Existing"],
    );

    let report = dependency_graph_report(&assignment_analysis);
    assert_eq!(report.atomic_graph.nodes.len(), 3);
    assert_eq!(
        report
            .quotient
            .nodes
            .iter()
            .map(|entry| entry.kind)
            .collect::<Vec<_>>(),
        vec![
            ModuleDestinationKind::Unassigned,
            ModuleDestinationKind::Specified,
            ModuleDestinationKind::Entry,
        ]
    );
    assert!(
        report
            .atomic_graph
            .nodes
            .iter()
            .any(
                |atomic_group| member_bindings(&atomic_group.members) == vec!["Leaf".to_string()]
                    && atomic_group
                        .destinations
                        .iter()
                        .any(|destination| report.is_unassigned_or_entry(destination))
            ),
        "Leaf should appear as a residual atomic group: {:#?}",
        report.atomic_graph,
    );
    let json = serde_json::to_string(&report).expect("serialize DependencyGraphReport");
    assert!(json.contains(r#""atomic_graph""#));
}

/// `cli/gate.rs` reads the callee source unit off the serialized `dependency_graph.json.gz` edge to
/// drop a promoted edge when callee and caller land in different modules.
#[test]
fn dependency_graph_report_names_the_callee_source_unit_on_a_promoted_at_init_edge() {
    let assignment_analysis = assignment_analysis_with_unassigned_module(
        "const seed = 7; const dep = 42; const result = callee(seed); function callee(x) { return dep + x; }",
        &["seed", "result", "callee", "dep"],
        &[],
    );

    let report = dependency_graph_report(&assignment_analysis);
    let source_unit_of = |binding: &str| {
        report
            .nodes
            .iter()
            .find(|node| node.declared_bindings.iter().any(|b| b.binding == binding))
            .unwrap_or_else(|| panic!("no source unit declares `{binding}`: {:#?}", report.nodes))
            .id
            .clone()
    };
    let promoted = report
        .edges
        .iter()
        .find(|edge| {
            edge.source == source_unit_of("result")
                && edge.target == source_unit_of("dep")
                && edge.edge_kind == DepKind::EagerUse
        })
        .unwrap_or_else(|| {
            panic!(
                "no promoted edge from `result` to `dep`: {:#?}",
                report.edges
            )
        });
    assert_eq!(
        promoted.role,
        Some(EdgeRoleReport::PromotedAtInit {
            callee_source_unit: source_unit_of("callee")
        }),
    );
}

#[test]
fn atomic_graph_collapses_constraining_eager_cycle() {
    let assignment_analysis = assignment_analysis_with_unassigned_module(
        "const A = B + 1; const B = A + 1;",
        &["A", "B"],
        &[],
    );

    let report = dependency_graph_report(&assignment_analysis);
    let cycle_atomic_group = report
        .atomic_graph
        .nodes
        .iter()
        .find(|atomic_group| {
            member_bindings(&atomic_group.members) == vec!["A".to_string(), "B".to_string()]
        })
        .expect("A/B eager cycle should be one atomic group");
    assert_eq!(cycle_atomic_group.source_unit_ids.len(), 2);
    assert!(cycle_atomic_group.causes.contains(&DepKind::EagerUse));
}

#[test]
fn atomic_graph_preserves_direction_between_atomic_groups() {
    let assignment_analysis = assignment_analysis_with_unassigned_module(
        "const Leaf = 1; const ResidualUse = Leaf + 1;",
        &["Leaf", "ResidualUse"],
        &[],
    );

    let report = dependency_graph_report(&assignment_analysis);
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
fn immutable_symbol_const_is_an_inert_computed_class_key() {
    assert_eq!(
        has_side_effect_for("const brand = Symbol('brand'); class C { [brand]() {} }"),
        vec![false, false]
    );
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

// --- Comma-list source locations -----------------------------------------

#[test]
fn split_comma_list_assigns_per_declarator_source_ranges() {
    // Each post-split single-declarator source unit must report just its
    // declarator's line range, not the parent statement's (for
    // `export const`, not the `ExportDecl` wrapper's). The proposer's
    // `size_lines_estimate` and the lane workers' `body_extraction`
    // per-source-unit snippets both rely on this.
    for source in [
        "const A = 1,\n      B = 2,\n      C = 3;\n",
        "export const A = 1,\n             B = 2,\n             C = 3;\n",
    ] {
        let (module, cm) = parse_with_source_map(source);
        let line_index = js_ast::SourceLineIndex::for_source_map(&cm);
        let analysis = analyze_chunk(
            &module,
            &AnalysisInputs::default(),
            Some(analysis::execution_evidence::SourceLocator {
                source_path: "test.js",
                lines: &line_index,
            }),
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
            "each declarator should report only its own line for {source:?}",
        );
    }
}

// --- linker_order in AssignmentReport --------------------------------------

#[test]
fn validate_surfaces_linker_order_for_acyclic_spec() {
    // mod_0 reads B from mod_1 at-init → mod_1 must precede
    // mod_0 in the linker's evaluation order.
    let assignment_analysis = assignment_analysis_for(
        "const B = 42; const A = B + 1;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = assignment_analysis.validate();
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
    let assignment_analysis = assignment_analysis_for(
        "const A = B + 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = assignment_analysis.validate();
    assert!(!report.cycles.is_empty(), "expected a cycle in {report:?}",);
    assert!(
        report.linker_order.is_empty(),
        "linker_order must be empty when the dep graph is cyclic; got {:?}",
        report.linker_order,
    );
}

// --- Atomic groups ---------------------------------------------------------

fn atomic_groups_for(source: &str) -> Vec<AtomicGroup> {
    let module = parse(source);
    let facts = analyze_facts(&module);
    let dependency_graph = build_dependency_graph(&facts).unwrap();
    compute_atomic_groups(&dependency_graph)
}

fn atomic_group_sizes(atomic_groups: &[AtomicGroup]) -> Vec<usize> {
    let mut sizes: Vec<usize> = atomic_groups.iter().map(|u| u.members.len()).collect();
    sizes.sort_unstable();
    sizes
}

fn assert_partitions_all_source_units(atomic_groups: &[AtomicGroup], total_source_units: usize) {
    let summed: usize = atomic_groups.iter().map(|u| u.members.len()).sum();
    assert_eq!(
        summed, total_source_units,
        "atomic groups must cover every source unit exactly once; got atomic_groups {atomic_groups:?}",
    );
    let mut seen = BTreeSet::new();
    for atomic_group in atomic_groups {
        for source_unit in &atomic_group.members {
            assert!(
                seen.insert(*source_unit),
                "source unit {source_unit:?} appears in more than one atomic group",
            );
        }
    }
}

#[test]
fn atomic_groups_singletons_for_independent_source_units() {
    // No edges → each source unit is its own atomic group.
    let atomic_groups = atomic_groups_for("const A = 1; const B = 2; const C = 3;");
    assert_partitions_all_source_units(&atomic_groups, 3);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![1, 1, 1]);
}

#[test]
fn atomic_groups_eager_use_chain_stays_split() {
    // A → B → C via EagerUse: directed-only edges, no cycle, so
    // each source unit remains its own atomic group.
    let atomic_groups = atomic_groups_for("const C = 3; const B = C + 1; const A = B + 1;");
    assert_partitions_all_source_units(&atomic_groups, 3);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![1, 1, 1]);
}

#[test]
fn atomic_groups_lazy_use_cycle_stays_split() {
    // Two functions that reference each other lazily plus their
    // bindings — no constraining edges, so all four source units are
    // independent atomic groups.
    let atomic_groups = atomic_groups_for(
        "function helperA() { return B; } function helperB() { return A; } const A = 1; const B = 2;",
    );
    assert_partitions_all_source_units(&atomic_groups, 4);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![1, 1, 1, 1]);
}

#[test]
fn atomic_groups_sequenced_chain_stays_split() {
    // Three side-effecting top-level statements form a directed
    // Sequenced chain. A directed source-order edge alone is
    // satisfiable by linker order — no co-location forced — so
    // every source unit stays in its own atomic group. Co-location
    // would only kick in if some non-Sequenced edge ran in the
    // reverse direction.
    let atomic_groups = atomic_groups_for(
        r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
    );
    assert_partitions_all_source_units(&atomic_groups, 3);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![1, 1, 1]);
}

#[test]
fn atomic_groups_sequenced_plus_reverse_eager_merges() {
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
    // into one atomic group (Rebind bidirectional + Sequenced directed
    // form a cycle).
    let atomic_groups = atomic_groups_for(
        r#"let A = 1; A = (globalThis.tag = "x", 2); A = (globalThis.tag = "y", 3);"#,
    );
    assert_partitions_all_source_units(&atomic_groups, 3);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![3]);
}

#[test]
fn atomic_groups_lazy_rebind_merges() {
    // `let A = 0; function B() { A = 1; }` produces a LazyRebind
    // edge from B → A. LazyRebind is bidirectional in `G_atomic`,
    // so A and B collapse into one atomic group.
    let atomic_groups = atomic_groups_for("let A = 0; function B() { A = 1; }");
    assert_partitions_all_source_units(&atomic_groups, 2);
    assert_eq!(atomic_group_sizes(&atomic_groups), vec![2]);
}

// --- Factor assembly -----------------------------------------------------

fn assignment_summary(assignment_analysis: &AssignmentAnalysis) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = assignment_analysis
        .inputs()
        .dependency_graph()
        .iter_nodes()
        .map(|node| {
            let declared: Vec<String> = node.declared.iter().map(|id| id.0.to_string()).collect();
            let key = if declared.is_empty() {
                format!("stmt_{}", node.statement_ordinal.0)
            } else {
                declared.join(",")
            };
            let dest = assignment_analysis.assignment().of(node.id);
            // The legacy residual report projection renders as
            // `<residual>` so this summary stays stable across index
            // changes; specified modules use their `mod_<idx>` label.
            let idx = dest.0;
            let label = if dest
                == assignment_analysis
                    .assignment()
                    .entry_destination()
                    .module_id()
                || assignment_analysis
                    .inputs()
                    .planned_module(ModuleId(idx))
                    .is_some_and(|module| module.role == ModuleRole::Unassigned)
            {
                "<residual>".to_string()
            } else {
                render(dest).to_string()
            };
            (key, label)
        })
        .collect();
    out.sort();
    out
}

#[test]
fn assignment_assembly_unclaimed_source_units_default_to_residual() {
    let assignment_analysis = assignment_analysis_for("const A = 1; const B = 2;", &[]);
    let summary = assignment_summary(&assignment_analysis);
    assert_eq!(
        summary,
        vec![
            ("A".to_string(), "<residual>".to_string()),
            ("B".to_string(), "<residual>".to_string()),
        ],
    );
}

#[test]
fn assignment_assembly_single_claim_with_unclaimed_atomic_group_members_is_a_conflict() {
    // `const A = B + 1; const B = A + 1;` is one EagerUse cycle —
    // a single atomic group. Claiming A for mod_0 leaves B
    // defaulting to residual entry, which splits the atomic group
    // across {mod_0, <residual_entry>} — unrealizable. The spec
    // author needs to either also assign B (or leave both
    // unassigned), or remove the constraining edge that fused
    // them in the first place. `debundle coverage` may flag the
    // split the atomic group as an advisory edit, but assignment_assembly refuses
    // to silently move B for the user.
    let assignment_analysis =
        assignment_analysis_for("const A = B + 1; const B = A + 1;", &[("A", logical(0))]);
    let report = assignment_analysis.validate();
    assert_eq!(
        report.atomic_group_conflicts.len(),
        1,
        "expected the half-claimed EagerUse cycle to surface as an atomic-group conflict: {report:?}",
    );
    // The entry destination follows the one planned output module.
    assert_eq!(
        distinct_claim_modules(&report.atomic_group_conflicts[0]),
        vec![ModuleId(0), ModuleId(1)],
    );
}

#[test]
fn assignment_assembly_concordant_claims_within_atomic_group_are_fine() {
    // Both members of the same atomic group claimed for the same
    // module — that's the spec author being explicit, not a
    // conflict.
    let assignment_analysis = assignment_analysis_for(
        "const A = B + 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(0))],
    );
    let summary = assignment_summary(&assignment_analysis);
    assert_eq!(
        summary,
        vec![
            ("A".to_string(), "mod_0".to_string()),
            ("B".to_string(), "mod_0".to_string()),
        ],
    );
}

#[test]
fn assignment_assembly_records_conflict_on_split_eager_use_cycle() {
    let assignment_analysis = assignment_analysis_for(
        "const A = B + 1; const B = A + 1;",
        &[("A", logical(0)), ("B", logical(1))],
    );
    let report = assignment_analysis.validate();
    assert_eq!(
        report.atomic_group_conflicts.len(),
        1,
        "expected the A↔B EagerUse cycle to surface as an atomic-group conflict: {report:?}",
    );
    let conflict = &report.atomic_group_conflicts[0];
    assert_eq!(
        distinct_claim_modules(conflict),
        vec![ModuleId(0), ModuleId(1)],
    );
}

#[test]
fn assignment_assembly_records_no_conflict_for_sequenced_only_chain() {
    // Three side-effect statements, source-ordered. Directed
    // Sequenced edges alone form a chain in `G_atomic`, not an
    // SCC, so every source unit is its own atomic group and the spec
    // can split them across modules without violating
    // co-location. The validator may still flag a module-level
    // cycle when the spec creates one through reverse claims,
    // but assignment_assembly itself does not panic / record a
    // conflict here.
    let assignment_analysis = assignment_analysis_for(
        r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
        &[("a1", logical(0)), ("b1", logical(1)), ("a2", logical(0))],
    );
    let report = assignment_analysis.validate();
    assert!(
        report.atomic_group_conflicts.is_empty(),
        "Sequenced-only chains never force co-location: {report:?}",
    );
}

#[test]
fn assignment_assembly_independent_source_units_keep_independent_claims() {
    // Three eager-use chain: A → B → C, no cycle, three atomic
    // atomic groups. Each source unit's claim takes effect independently.
    let assignment_analysis = assignment_analysis_for(
        "const C = 3; const B = C + 1; const A = B + 1;",
        &[("A", logical(0)), ("B", logical(1)), ("C", logical(0))],
    );
    let summary = assignment_summary(&assignment_analysis);
    assert_eq!(
        summary,
        vec![
            ("A".to_string(), "mod_0".to_string()),
            ("B".to_string(), "mod_1".to_string()),
            ("C".to_string(), "mod_0".to_string()),
        ],
    );
}
