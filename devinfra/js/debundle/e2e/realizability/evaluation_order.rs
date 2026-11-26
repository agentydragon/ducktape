//! Node-differential sweep over the accepted asymmetric / phantom /
//! explicit-priority fixture family: the shared verifier must
//! predict exactly the ECMA-262 Phase-2 evaluation order Node produces
//! for the emitted tree.
//!
//! Emitter and verifier both consume the same supplied chunk priority, so this checks
//! that the prediction matches the bundle Node evaluates. Each case
//! runs the real pipeline, instruments the emitted module files with
//! per-module marker prints (after the debundler runs, so the analyzed
//! graph is untouched), and compares the observed completion order
//! against `gate::check_realizability_with_priority` on the same
//! dependency graph and assignment.

use std::collections::{BTreeMap, BTreeSet};

use analysis::AnalysisInputs;
use analysis::ids::ModuleId;
use analyze::analyze_chunk;
use debundle_e2e_support::*;
use gate::check_realizability_with_priority;
use graph::build_dependency_graph_with;
use graph::{DependencyGraph, ModuleAssignment};

struct SweepCase {
    name: &'static str,
    source: &'static str,
    stdout: &'static str,
    required_import: Option<(&'static str, &'static str)>,
    /// `(module name, member bindings)`, alphabetical by module name.
    /// The assignment below uses this numbering. Priority is independent
    /// of module IDs and alphabetical path order.
    modules: &'static [(&'static str, &'static [&'static str])],
    priority: &'static [&'static str],
}

const SWEEP: &[SweepCase] = &[
    // Asymmetric I-cycle whose only residual-side reference points at the
    // constraining edge's TARGET (the dependency).
    SweepCase {
        name: "asymmetric_dependency_only_residual_reference",
        stdout: "v\n",
        required_import: None,
        priority: &["mod_ids", "mod_schemas"],
        source: "const schemas_target = \"v\";\n\
                 function lazy_back() { return ids_val; }\n\
                 const ids_val = schemas_target;\n\
                 console.log(schemas_target);\n",
        modules: &[
            ("mod_ids", &["ids_val"]),
            ("mod_schemas", &["schemas_target", "lazy_back"]),
        ],
    },
    // Two independent acyclic array chains whose alphabetical output-module
    // order is the reverse of the explicitly supplied source preference.
    SweepCase {
        name: "acyclic_independent_array_chains_prefer_source_order",
        stdout: "1 2\n",
        required_import: None,
        priority: &["z_first", "z_first_read", "a_second", "a_second_read"],
        source: "const first = [1];\n\
                 const firstRead = first[0];\n\
                 const second = [2];\n\
                 const secondRead = second[0];\n\
                 console.log(firstRead, secondRead);\n",
        modules: &[
            ("a_second", &["second"]),
            ("a_second_read", &["secondRead"]),
            ("z_first", &["first"]),
            ("z_first_read", &["firstRead"]),
        ],
    },
    // Lemma 2's rescued asymmetric cycle: residual at-init reads
    // both SCC members; the supplied priority imports the dependent first.
    SweepCase {
        name: "asymmetric_cycle_residual_reads_both",
        stdout: "alpha alpha-beta alpha-beta\n",
        required_import: None,
        priority: &["mod_dependent", "mod_dep"],
        source: "const dep_value = \"alpha\";\n\
                 const cross_value = dep_value + \"-beta\";\n\
                 function lazy_reader() { return cross_value; }\n\
                 console.log(dep_value, cross_value, lazy_reader());\n",
        modules: &[
            ("mod_dep", &["dep_value", "lazy_reader"]),
            ("mod_dependent", &["cross_value"]),
        ],
    },
    // Mediator-only entrant: residual's statements never reference
    // the SCC directly; the entry's universal per-plan imports still
    // DFS into the SCC at the dependent first.
    SweepCase {
        name: "mediator_only_entrant_into_asymmetric_cycle",
        stdout: "alphaalpha-beta\n",
        required_import: None,
        priority: &["mod_dependent", "mod_dep", "mod_mediator"],
        source: "const dep_value = \"alpha\";\n\
                 const cross_value = dep_value + \"-beta\";\n\
                 function lazy_reader() { return cross_value; }\n\
                 function mediator_helper() { return dep_value + lazy_reader(); }\n\
                 const mediator_init = mediator_helper();\n\
                 console.log(mediator_init);\n",
        modules: &[
            ("mod_dep", &["dep_value", "lazy_reader"]),
            ("mod_dependent", &["cross_value"]),
            ("mod_mediator", &["mediator_helper", "mediator_init"]),
        ],
    },
    // Pure mutual lazy cycle: no constraining edge between the SCC
    // members, so their relative order follows only the supplied
    // priority — a case where simulator/emitter drift would slip past every
    // TDZ-anchored test.
    SweepCase {
        name: "pure_lazy_cycle_tie_break",
        stdout: "a b\n",
        required_import: None,
        priority: &["mod_a", "mod_b"],
        source: "const a_value = \"a\";\n\
                 const b_value = \"b\";\n\
                 function read_a() { return a_value; }\n\
                 function read_b() { return b_value; }\n\
                 console.log(read_a(), read_b());\n",
        modules: &[
            ("mod_a", &["a_value", "read_b"]),
            ("mod_b", &["b_value", "read_a"]),
        ],
    },
    // Phantom side-effect chain: the impure statements share no
    // binding reads, so the only cross-module ordering constraint is
    // the S-edge, emitted as a phantom side-effect import (Lemma 5).
    // Request the opposite priority so a missing import reverses the effects.
    SweepCase {
        name: "phantom_side_effect_chain",
        stdout: "first\nsecond\nf s\n",
        required_import: Some(("mod_a_second", "./mod_b_first.js")),
        priority: &["mod_a_second", "mod_b_first"],
        source: "const loud_first = (console.log(\"first\"), \"f\");\n\
                 const loud_second = (console.log(\"second\"), \"s\");\n\
                 console.log(loud_first, loud_second);\n",
        modules: &[
            ("mod_a_second", &["loud_second"]),
            ("mod_b_first", &["loud_first"]),
        ],
    },
];

/// Parse `source` and build the dependency graph through the same
/// analysis path the pipeline uses (mirrors
/// `realizability::tests::parse_and_build`).
fn parse_and_build(source: &str) -> DependencyGraph {
    let module = raw_js_test_support::parse(source);
    let facts = analyze_chunk(&module, &AnalysisInputs::default(), None).facts;
    build_dependency_graph_with(&facts, Default::default(), &[])
        .unwrap()
        .0
}

/// Build the assignment matching the case's spec assignment:
/// module index = position in the (alphabetical) module list,
/// entry = the destination index after every planned output module.
fn assignment_for(dependency_graph: &DependencyGraph, case: &SweepCase) -> ModuleAssignment {
    let residual = ModuleId(case.modules.len());
    let mut assignment = ModuleAssignment::new(
        dependency_graph,
        analysis::EntryDestination::from_module_id(residual),
    );
    for (index, (module_name, bindings)) in case.modules.iter().enumerate() {
        for binding in *bindings {
            let source_unit = dependency_graph
                .iter_nodes()
                .find(|node| node.declared.iter().any(|id| id.0.as_ref() == *binding))
                .unwrap_or_else(|| {
                    panic!(
                        "{}: no source unit declares binding {binding} for {module_name}",
                        case.name
                    )
                });
            assignment.set(source_unit.id, ModuleId(index));
        }
    }
    assignment
}

#[test]
fn simulator_post_order_matches_node_evaluation_order_across_fixture_family() {
    for case in SWEEP {
        assert!(
            case.modules.is_sorted_by_key(|(name, _)| *name),
            "{}: modules must be listed alphabetically so the hand-built \
             ModuleIds match the pipeline's plan indexing",
            case.name,
        );

        // Simulator side: predicted post-order on the same source unit
        // graph + assignment the pipeline derives from the spec.
        let dependency_graph = parse_and_build(case.source);
        let assignment = assignment_for(&dependency_graph, case);
        let nodes: BTreeSet<_> = (0..=case.modules.len()).map(ModuleId).collect();
        let priority: Vec<_> = case
            .priority
            .iter()
            .map(|name| {
                ModuleId(
                    case.modules
                        .iter()
                        .position(|(module, _)| module == name)
                        .unwrap(),
                )
            })
            .collect();
        let (verdict, schedule) =
            check_realizability_with_priority(&dependency_graph, &assignment, &nodes, &priority)
                .unwrap();
        assert!(
            verdict.is_realizable(),
            "{}: supplied priority must be valid: {verdict:?}",
            case.name
        );
        let post_order = schedule.post_order();
        // The simulator's residual node is the ESM DFS root — the
        // emitted entry file (the sweep's residual statements are all
        // anonymous, so no catchall module file materializes).
        let label_of = |module: ModuleId| -> &'static str {
            let ModuleId(index) = module;
            case.modules
                .get(index)
                .map(|(name, _)| *name)
                .unwrap_or("entry")
        };
        let predicted: Vec<&str> = {
            let by_rank: BTreeMap<usize, ModuleId> = post_order
                .iter()
                .map(|(module, rank)| (*rank, *module))
                .collect();
            by_rank.into_values().map(label_of).collect()
        };
        assert_eq!(
            predicted.len(),
            case.modules.len() + 1,
            "{}: every module (plus residual) must be reachable in the \
             simulator's universe; predicted: {predicted:?}",
            case.name,
        );

        // Node side: run the real pipeline, instrument the emitted
        // modules, observe the actual evaluation completion order.
        let mut instrumented: Vec<&str> = case.modules.iter().map(|(name, _)| *name).collect();
        instrumented.push("entry");
        let fixture = run_fixture(
            FixtureOpts::new(
                case.source,
                case.modules
                    .iter()
                    .map(|(name, bindings)| {
                        let members: Vec<Member> = bindings
                            .iter()
                            .map(|binding| Member::new(binding))
                            .collect();
                        logical_module(name, &members)
                    })
                    .collect(),
            )
            .with_import_order(case.priority),
        );
        assert_preserved_entry_output(&fixture, case.stdout);
        if let Some((module, import)) = case.required_import {
            assert_module_source(
                &fixture.out_root,
                &format!("{}/modules/{module}.js", fixture.chunk_id),
                &[import],
                &[],
            );
        }
        let actual = node_module_evaluation_order(&fixture, &instrumented);

        assert_eq!(
            actual, predicted,
            "{}: simulator-predicted post-order must match the emitted \
             tree's actual Node evaluation order",
            case.name,
        );
    }
}
