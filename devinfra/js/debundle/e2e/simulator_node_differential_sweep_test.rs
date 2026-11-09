//! Node-differential sweep over the accepted asymmetric / phantom /
//! tie-break fixture family: the public emitted-order simulator must
//! predict exactly the ECMA-262 Phase-2 evaluation order Node produces
//! for the emitted tree.
//!
//! Emitter and public simulator both consume the shared
//! `EsmImportOrder` factory (see `esm_import_order`), so this checks
//! that the prediction matches the bundle Node evaluates. Each case
//! runs the real pipeline, instruments the emitted module files with
//! per-module marker prints (after the debundler runs, so the analyzed
//! graph is untouched), and compares the observed completion order
//! against `gate::simulated_evaluation_post_order` on the same
//! dependency graph and assignment.

use std::collections::BTreeMap;

use analysis::facts::analyze_chunk;
use analysis::graph::{build_dependency_graph_with, chunk_constraining_module_edges};
use analysis::ids::ModuleId;
use analysis::{AnalysisInputs, DependencyGraph, ModuleAssignment};
use debundle_e2e_support::*;
use gate::simulated_evaluation_post_order;

struct SweepCase {
    name: &'static str,
    source: &'static str,
    /// `(module name, member bindings)`, alphabetical by module name.
    /// The pipeline indexes planned output modules in spec-map order
    /// (alphabetical by target path), with the entry destination after them;
    /// the hand-built assignment below mirrors that numbering so the
    /// simulator's `ModuleId` tie-breaks match the emitted tree's.
    modules: &'static [(&'static str, &'static [&'static str])],
}

const SWEEP: &[SweepCase] = &[
    // Asymmetric I-cycle whose only residual-side reference points at the
    // constraining edge's TARGET (the dependency). Node side also pinned by
    // `asymmetric_non_residual_cycle_test::dependency_only_residual_reference_into_asymmetric_cycle_runs_under_node`.
    SweepCase {
        name: "asymmetric_dependency_only_residual_reference",
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
    // order is the reverse of source order. Entry import fanout must follow
    // the same source preference as emission, not the base ModuleId tie-break.
    SweepCase {
        name: "acyclic_independent_array_chains_prefer_source_order",
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
    // both SCC members; entry's intra-SCC reversal imports the
    // dependent first.
    SweepCase {
        name: "asymmetric_cycle_residual_reads_both",
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
    // members, so their relative order is decided purely by the
    // shared tie-break (`ModuleId` ascending) — the case where a
    // simulator/emitter tie-break drift would slip past every
    // TDZ-anchored test.
    SweepCase {
        name: "pure_lazy_cycle_tie_break",
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
    // the S-edge, emitted as a phantom side-effect import (the
    // Lemma 5 shape — see `lemma_five_side_effect_order_test`).
    SweepCase {
        name: "phantom_side_effect_chain",
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
        let canonical = chunk_constraining_module_edges(&dependency_graph, &assignment);
        let post_order =
            simulated_evaluation_post_order(&canonical, &dependency_graph, &assignment);
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
        let fixture = run_fixture(FixtureOpts::new(
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
        ));
        let actual = node_module_evaluation_order(&fixture, &instrumented);

        assert_eq!(
            actual, predicted,
            "{}: simulator-predicted post-order must match the emitted \
             tree's actual Node evaluation order",
            case.name,
        );
    }
}
