//! Focused assignment, report, purity and source-location tests.
//! Acceptance, rejection and runtime ordering are exercised through the CLI
//! in e2e/realizability/gate_reports.rs and the at_init_* and lemma_* suites.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use super::*;
use analysis::*;
use analysis_model::DepKind;
use analysis_vocabulary::inputs::AnalysisInputs;
use graph::*;
use raw_js_test_support::parse;
use source_identity::ChunkId;
use swc_common::SyntaxContext;
use swc_ecma_ast::{Id, Module};

fn test_id(name: &str) -> Id {
    (name.into(), SyntaxContext::empty())
}

fn analyze_facts(module: &Module) -> Vec<StatementFacts> {
    analyze::analyze_chunk(module, &AnalysisInputs::default(), None).facts
}

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

/// Sorted distinct destination modules across a conflict's claims.
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

fn assignment_analysis_from_facts(
    facts: &[StatementFacts],
    bindings: HashMap<swc_ecma_ast::Id, BindingKind>,
    planned_modules: Vec<PlannedModule>,
    entry_destination: EntryDestination,
) -> AssignmentAnalysis {
    assert_eq!(
        entry_destination,
        EntryDestination::after_planned_modules(planned_modules.len())
    );
    let priority: Vec<_> = planned_modules
        .iter()
        .map(|module| spec::ModulePath::parse(&module.id, "test_chunk").unwrap())
        .collect();
    AssignmentAnalysis::build_with(
        "test_chunk".to_string(),
        ChunkId(0),
        compute_dependency_graph_and_atomic_groups_with(facts, GraphOptions::default(), &[])
            .expect("chunk facts declare a binding twice"),
        BindingCatalogue::new(ChunkId(0), bindings),
        planned_modules,
        HashMap::new(),
        &priority,
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
        &[],
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
fn quotient_report_keeps_scc_ids_in_graph_traversal_order() {
    let assignment = assignment_analysis_for(
        "function c() { return d; } function d() { return c; }
         function a() { return b; } function b() { return a; }",
        &[
            ("c", logical(3)),
            ("d", logical(2)),
            ("a", logical(1)),
            ("b", logical(0)),
        ],
    );
    let report = dependency_graph_report(&assignment);
    let cycles: Vec<_> = report
        .quotient
        .sccs
        .iter()
        .filter(|scc| scc.is_cycle)
        .collect();
    let [first, second] = cycles.as_slice() else {
        panic!("expected two separate structural SCCs, got {cycles:?}");
    };
    assert_eq!(first.id, "scc:0");
    assert_eq!(
        first.modules,
        [
            report::module_key(ModuleId(2)),
            report::module_key(ModuleId(3))
        ]
    );
    assert_eq!(first.module_edge_ids, ["module_edge:2", "module_edge:3"]);
    assert_eq!(second.id, "scc:1");
    assert_eq!(
        second.modules,
        [
            report::module_key(ModuleId(0)),
            report::module_key(ModuleId(1))
        ]
    );
    assert_eq!(second.module_edge_ids, ["module_edge:0", "module_edge:1"]);
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
fn validate_retains_the_supplied_traversal_for_cyclic_constraints() {
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
    assert_eq!(
        report
            .linker_order
            .iter()
            .map(|path| path.as_str())
            .collect::<Vec<_>>(),
        ["mod_1", "mod_0", "anon_residual_sentinel"],
        "a rejected witness still has a concrete traversal; it is not a successful schedule-existence query",
    );
}

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

/// An atomic group split across destinations is one conflict between the two
/// destination modules (the entry follows the one planned output module).
/// `assignment_assembly` records it in `atomic_group_conflicts`; the
/// materializer bails on any non-empty list.
#[test]
fn splitting_an_atomic_group_is_one_conflict() {
    let cycle = "const A = B + 1; const B = A + 1;";
    for (source, ownership) in [
        // A LazyRebind group: the declarer and assigner of a mutable binding
        // must materialize together.
        (
            "let A = 0; function B() { A = 1; }",
            vec![("A", logical(0)), ("B", TestDestination::Entry)],
        ),
        // An EagerUse cycle is one atomic group. Claiming only A leaves B
        // defaulting to the entry; `assignment_assembly` refuses to move B
        // silently.
        (cycle, vec![("A", logical(0))]),
        (cycle, vec![("A", logical(0)), ("B", logical(1))]),
    ] {
        let report = assignment_analysis_for(source, &ownership).validate();
        assert_eq!(
            report.atomic_group_conflicts.len(),
            1,
            "{source} with {ownership:?}: {report:?}",
        );
        assert_eq!(
            distinct_claim_modules(&report.atomic_group_conflicts[0]),
            vec![ModuleId(0), ModuleId(1)],
            "{source} with {ownership:?}",
        );
    }
}

#[test]
fn claims_that_keep_atomic_groups_whole_are_not_conflicts() {
    for (source, ownership) in [
        // Rebinding writes stay local to the emitted module.
        (
            "let A = 0; function B() { A = 1; }",
            vec![("A", logical(0)), ("B", logical(0))],
        ),
        // Directed Sequenced edges alone form a chain in `G_atomic`, not an
        // SCC, so every source unit is its own atomic group and the spec can
        // split them across modules without violating co-location.
        (
            r#"const a1 = (globalThis.tag = "a1", 1); const b1 = (globalThis.tag = "b1", 2); const a2 = (globalThis.tag = "a2", 3);"#,
            vec![("a1", logical(0)), ("b1", logical(1)), ("a2", logical(0))],
        ),
    ] {
        let report = assignment_analysis_for(source, &ownership).validate();
        assert!(
            report.atomic_group_conflicts.is_empty(),
            "{source} with {ownership:?}: {report:?}",
        );
    }
}

#[test]
fn assignments_follow_claims_per_source_unit() {
    for (source, ownership, expected) in [
        // Unclaimed source units default to the residual.
        (
            "const A = 1; const B = 2;",
            vec![],
            vec![("A", "<residual>"), ("B", "<residual>")],
        ),
        // Both members of one atomic group claimed for the same module is
        // explicit, not a conflict.
        (
            "const A = B + 1; const B = A + 1;",
            vec![("A", logical(0)), ("B", logical(0))],
            vec![("A", "mod_0"), ("B", "mod_0")],
        ),
        // An eager-use chain A -> B -> C has no cycle, so each source unit's
        // claim takes effect independently.
        (
            "const C = 3; const B = C + 1; const A = B + 1;",
            vec![("A", logical(0)), ("B", logical(1)), ("C", logical(0))],
            vec![("A", "mod_0"), ("B", "mod_1"), ("C", "mod_0")],
        ),
    ] {
        let summary = assignment_summary(&assignment_analysis_for(source, &ownership));
        let expected: Vec<_> = expected
            .iter()
            .map(|(key, label)| (key.to_string(), label.to_string()))
            .collect();
        assert_eq!(summary, expected, "{source} with {ownership:?}");
    }
}
