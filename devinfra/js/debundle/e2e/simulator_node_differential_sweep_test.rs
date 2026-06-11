//! Node-differential sweep over the accepted asymmetric / phantom /
//! tie-break fixture family: the realizability gate's evaluation
//! simulator must predict exactly the ECMA-262 Phase-2 evaluation
//! order Node produces for the emitted tree.
//!
//! Emitter and simulator both consume `EsmImportOrder` (the shared
//! single source of truth — see `esm_import_order`), so a divergence
//! here means the gate's verdict has stopped describing the bundle
//! the emitter produces: an accepted spec could TDZ under Node, or a
//! runnable spec could be over-rejected. Each case runs the real
//! pipeline, instruments the emitted module files with per-module
//! marker prints (after the debundler runs, so the analyzed graph is
//! untouched), and compares the observed completion order against
//! `gate::simulated_evaluation_post_order` on the same owner graph
//! and partition.
//!
//! Generalizes the single-shape unit pin
//! `realizability::tests::simulator_post_order_matches_emitted_evaluation_order`
//! (the gaffer asymmetric cycle) into a sweep; that pin keeps the
//! hand-derived expected order at the unit level, this sweep pins the
//! simulator against the live Node runtime across the family.

use std::collections::BTreeMap;

use analysis::facts::analyze_chunk;
use analysis::graph::build_owner_graph;
use analysis::ids::{LogicalModuleIndex, ModuleId};
use analysis::{AnalysisHints, OwnerGraph, Partition};
use debundle_e2e_support::*;
use gate::simulated_evaluation_post_order;
use swc_common::{FileName, SourceMap, sync::Lrc};
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

struct SweepCase {
    name: &'static str,
    source: &'static str,
    /// `(module name, member bindings)`, alphabetical by module name.
    /// The pipeline indexes logical-module plans in spec-map order
    /// (alphabetical by target path) with the residual sentinel last;
    /// the hand-built partition below mirrors that numbering so the
    /// simulator's `ModuleId` tie-breaks match the emitted tree's.
    modules: &'static [(&'static str, &'static [&'static str])],
}

const SWEEP: &[SweepCase] = &[
    // The gaffer over-rejection shape (#2071): asymmetric I-cycle
    // whose only residual-side reference points at the constraining
    // edge's TARGET (the dependency). Node side originally pinned by
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

/// Parse `source` and build the owner graph through the same
/// analysis path the pipeline uses (mirrors
/// `realizability::tests::parse_and_build`).
fn parse_and_build(source: &str) -> OwnerGraph {
    let cm: Lrc<SourceMap> = Default::default();
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
    let module = Parser::new_from(lexer)
        .parse_module()
        .expect("parse module");
    let facts = analyze_chunk(&module, &AnalysisHints::default(), None, |_| None).facts;
    build_owner_graph(&facts).unwrap()
}

/// Build the partition matching the case's spec assignment:
/// module index = position in the (alphabetical) module list,
/// residual = the sentinel index after every named module.
fn partition_for(owner_graph: &OwnerGraph, case: &SweepCase) -> Partition {
    let residual = ModuleId(LogicalModuleIndex(case.modules.len()));
    let mut partition = Partition::new(owner_graph, residual);
    for (index, (module_name, bindings)) in case.modules.iter().enumerate() {
        for binding in *bindings {
            let owner = owner_graph
                .iter_nodes()
                .find(|node| node.declared.iter().any(|id| id.0.as_ref() == *binding))
                .unwrap_or_else(|| {
                    panic!(
                        "{}: no owner declares binding {binding} for {module_name}",
                        case.name
                    )
                });
            partition.set(owner.id, ModuleId(LogicalModuleIndex(index)));
        }
    }
    partition
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

        // Simulator side: predicted post-order on the same owner
        // graph + partition the pipeline derives from the spec.
        let owner_graph = parse_and_build(case.source);
        let partition = partition_for(&owner_graph, case);
        let post_order = simulated_evaluation_post_order(&owner_graph, &partition);
        // The simulator's residual node is the ESM DFS root — the
        // emitted entry file (the sweep's residual statements are all
        // anonymous, so no catchall module file materializes).
        let label_of = |module: ModuleId| -> &'static str {
            let ModuleId(LogicalModuleIndex(index)) = module;
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
