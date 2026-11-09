//! Report-only effect translation validation: initializer and owner
//! coverage for split declarators and deferred callables.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn deferred_creation_and_full_scope_facts_do_not_invent_effect_receipts() {
    for (helper, result_kind, unknown) in [
        // Array creation is a covered allocation; only the output call
        // remains uncovered. Record creation has no receipt yet.
        ("function make() { return [seed]; }", "object", 1),
        ("function make() { return [, seed]; }", "object", 1),
        ("function make() { return { value: seed }; }", "object", 2),
        (
            "function make() { return { read() { return seed; }, get value() { return seed; }, set value(next) {} }; }",
            "object",
            2,
        ),
        ("function make() { return () => seed; }", "function", 1),
        (
            "const make = function self() { return seed; };",
            "number",
            2,
        ),
        (
            "function make() { return seed; var { unused } = {}; }",
            "number",
            2,
        ),
        ("function make() { return seed; let unused; }", "number", 1),
        (
            "function make() { return seed; function unused() {} }",
            "number",
            2,
        ),
        (
            "function make() { return seed; class Unused {} }",
            "number",
            1,
        ),
        ("function make() { return seed; }", "number", 1),
    ] {
        let source = format!(
            "const seed = 41;\n{helper}\nvar result = (make(), make());\nconsole.log(typeof result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("make"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, &format!("{result_kind}\n"));
        if matches!(
            helper,
            "function make() { return seed; let unused; }"
                | "function make() { return seed; class Unused {} }"
        ) {
            let execution = binding_execution_report(&fixture, "result");
            assert_eq!(
                execution["binding_effect_authority"]["authority"], "operation_receipt",
                "an unreached lexical declaration still creates a private cell without exterior effects: {execution:#}"
            );
        }
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], unknown,
                "only actual operation receipts may cover the initializer for {helper}: {report:#}"
            );
        }
    }
}

#[test]
fn composed_initializer_coverage_retains_discarded_reads_and_child_results() {
    for prefix in ["", "export "] {
        let source = format!(
            "{prefix}const first = 1;\n\
{prefix}const value = (first, {{value: !first, ignored: void first, equal: first === first, items: [first]}});\n\
console.log(JSON.stringify(value));\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "values",
                &[Member::new("first"), Member::new("value")],
            )],
        ));
        assert_preserved_entry_output(&fixture, "{\"value\":false,\"equal\":true,\"items\":[1]}\n");
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], 1,
                "only the output call remains uncovered: {source}: {report:#}"
            );
        }
    }
}

#[test]
fn exported_split_declarators_keep_composed_coverage_local() {
    let fixture = run_fixture(FixtureOpts::new(
        "export const first = 1, value = (first, {value: first}), unsupported = (``, 2);\n\
console.log(value.value, unsupported);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("first"),
                Member::new("value"),
                Member::new("unsupported"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1 2\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["coverage"]["statement_count"], 4, "{report:#}");
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "{report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the unsupported sibling cannot borrow another declarator's coverage: {report:#}"
    );
}

#[test]
fn repeated_nested_calls_report_each_flat_operation_edge_once() {
    let source = "function leaf() { return 1; }\n\
function branch() { return (leaf(), leaf()); }\n\
function root() { return (branch(), branch()); }\n\
var result = (root(), root());\n\
console.log(result);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[
                Member::new("leaf"),
                Member::new("branch"),
                Member::new("root"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1\n");

    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("compound initializer source unit");
    let execution = serde_json::to_value(
        result
            .synchronous_execution
            .as_ref()
            .expect("initializer execution summary"),
    )
    .expect("execution report serializes");
    let authority = &execution["binding_effect_authority"];
    assert_eq!(authority["authority"], "operation_receipt", "{execution:#}");
    let calls = authority["receipt"]["calls"]
        .as_array()
        .expect("operation receipt call edges");
    assert_eq!(
        calls.len(),
        6,
        "two initializer-to-root, two root-to-branch, and two branch-to-leaf edges: {execution:#}"
    );
    assert_eq!(
        calls
            .iter()
            .filter(|call| call["caller"]["kind"] == "root_initializer")
            .count(),
        2,
        "both initializer call sites remain represented: {execution:#}"
    );
    let unique_edges: std::collections::BTreeSet<_> = calls
        .iter()
        .map(|call| {
            serde_json::to_string(&(&call["caller"], &call["call_span"], &call["target"]))
                .expect("edge identity serializes")
        })
        .collect();
    assert_eq!(
        unique_edges.len(),
        calls.len(),
        "the public report contains one row per exact caller/call-site/target edge: {execution:#}"
    );
}

#[test]
fn discarded_uninitialized_reads_cannot_complete_initializer_coverage() {
    for (kind, expected_error) in [("const", true), ("var", false)] {
        let source = format!(
            "const value = (later, {{value: 1}});\n{kind} later = 2;\nconsole.log(value.value);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "values",
                &[Member::new("value"), Member::new("later")],
            )],
        ));
        if expected_error {
            assert_preserved_entry_error(&fixture, "ReferenceError");
        } else {
            assert_preserved_entry_output(&fixture, "1\n");
        }
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], 2,
                "the discarded uninitialized read and output remain uncovered: {report:#}"
            );
        }
        assert_eq!(
            report["coverage"]["owner_coverage_refusals"]["initializer_result_unproved"], 1,
            "the final literal must not erase the discarded read: {report:#}"
        );
    }
}

#[test]
fn function_creation_with_lazy_parameters_can_cross_an_independent_initializer() {
    let source = "const marker = 1;\n\
const render = ({value = globalThis.readDefault(), ...props} = globalThis.input, ...tail) => {\n\
  globalThis.consume(value, props, tail);\n\
};\n\
console.log(marker);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("render", &[Member::new("render")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["counts"]["havoc_pairs"], 0, "{report:#}");
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "the verdict covers the exact closed candidate inversion: {report:#}"
    );
    assert_eq!(report["relocation_dependencies"], serde_json::json!({}));
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
}

#[test]
fn calling_a_function_with_lazy_parameters_retains_unknown_effects() {
    let source = "const input = {get value() { globalThis.parameterReads = (globalThis.parameterReads || 0) + 1; return 7; }};\n\
const select = ({value} = input) => value;\n\
const selected = select();\n\
console.log(selected, globalThis.parameterReads);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[
                Member::new("input"),
                Member::new("select"),
                Member::new("selected"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 1\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 3,
            "{report:#}"
        );
    }
}

#[test]
fn split_simple_middle_and_later_declarators_receive_owner_coverage() {
    let fixture = run_fixture(FixtureOpts::new(
        "const first = 1, middle = 2, later = 3;\nconsole.log(first, middle, later);\n",
        vec![
            logical_module("first", &[Member::new("first")]),
            logical_module("middle", &[Member::new("middle")]),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_entry_output(&fixture, "1 2 3\n");

    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "only console.log remains uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["statement_count"], 4,
        "the split declarators and trailing call remain separate source owners: {report:#}"
    );
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["split_declaration"]
            .as_u64()
            .unwrap_or_default(),
        0,
        "eligible split owners must not be refused as a whole declaration: {report:#}"
    );
}

#[test]
fn split_declarator_coverage_stays_local_and_requires_completed_initialization() {
    let effectful_sibling = run_fixture(FixtureOpts::new(
        "const first = (globalThis.marker = 1, 1), middle = 2, later = 3;\nconsole.log(first, middle, later, globalThis.marker);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("first"),
                Member::new("middle"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&effectful_sibling, "1 2 3 1\n");
    let report = effectful_sibling.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "the effectful first initializer and console.log remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the effectful sibling remains an unsupported source initializer: {report:#}"
    );

    let getter = run_fixture(FixtureOpts::new(
        "const source = { get value() { globalThis.touched = 1; return 4; } }, middle = source.value, later = 5;\nconsole.log(middle, later, globalThis.touched);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("source"),
                Member::new("middle"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&getter, "4 5 1\n");
    let report = getter.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 3,
            "getter-bearing source, member-read initializer, and console.log remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 2,
        "the accessor-bearing source and member read each retain their own refusal: {report:#}"
    );

    let callback = run_fixture(FixtureOpts::new(
        "function invoke(callback) { callback(); return 4; }\nconst callback = () => { globalThis.touched = 7; };\nconst middle = invoke(callback), later = 5;\nconsole.log(middle, later, globalThis.touched);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("callback"),
                Member::new("middle"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&callback, "4 5 7\n");
    let report = callback.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "the invoking initializer and console.log remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the call initializer remains outside the literal proof: {report:#}"
    );

    let tdz = run_fixture(FixtureOpts::new(
        "const first = later, middle = 2, later = 3;\nconsole.log(first, middle, later);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("first"),
                Member::new("middle"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_error(&tdz, "ReferenceError");
    let report = tdz.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "the TDZ read and console.log remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["uninitialized_read"], 1,
        "the first declarator's ordered read observes a TDZ cell: {report:#}"
    );
}

#[test]
fn deferred_callable_creation_coverage_composes_through_calls_and_exact_owners() {
    let fixture = run_fixture(FixtureOpts::new(
        "function factory() { return () => 3; }\nfunction nested() { return factory(); }\nconst selected = nested();\nconst result = selected();\nconsole.log(result);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "3\n");
    for (binding, owner, call_count) in [("selected", 2, 2), ("result", 3, 1)] {
        let execution = binding_execution_report(&fixture, binding);
        assert_eq!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "{execution:#}"
        );
        let receipt = &execution["binding_effect_authority"]["receipt"];
        assert_eq!(receipt["owner"], owner, "{execution:#}");
        assert_eq!(receipt["binding"], binding, "{execution:#}");
        let calls = receipt["calls"].as_array().unwrap();
        assert_eq!(calls.len(), call_count, "{receipt:#}");
        // These source-only operations consume no author/value/native premise.
        // Assert the exact dependency set rather than inferring it from an
        // empty observed-access ledger or timing completeness.
        assert_eq!(
            receipt["read_dependencies"],
            serde_json::json!({}),
            "{receipt:#}"
        );
        assert_eq!(
            receipt["rebind_dependencies"],
            serde_json::json!({}),
            "{receipt:#}"
        );
        for call in calls {
            assert_eq!(call["dependencies"], serde_json::json!({}), "{receipt:#}");
        }
    }
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
}

#[test]
fn deferred_callable_creation_does_not_cover_unknown_getter_or_reentry_invocations() {
    for (prefix, body) in [
        ("", "return globalThis.read();"),
        (
            "const record = { get value() { return 3; } };\n",
            "return record.value;",
        ),
        ("", "globalThis.reenter(); return 3;"),
    ] {
        let source = format!(
            "globalThis.read = () => 3;\nglobalThis.reenter = () => undefined;\n{prefix}function factory() {{ return () => {{ {body} }}; }}\nconst selected = factory();\nconst result = selected();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
        assert_preserved_entry_output(&fixture, "3\n");
        let execution = binding_execution_report(&fixture, "result");
        assert_ne!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "{body}: {execution:#}"
        );
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert!(
                report["coverage"]["unknown_coverage"][domain]
                    .as_u64()
                    .unwrap()
                    >= 2,
                "{body}: {report:#}"
            );
        }
    }
}

#[test]
fn deferred_callable_operation_receipts_preserve_exact_native_prefix_dependencies() {
    const SOURCE: &str = "const cache = new Map();\nfunction factory() { return () => cache; }\nfunction nested() { return factory(); }\nconst selected = nested();\nconst result = selected();\nconsole.log(result === cache);\n";
    let fixture = run_fixture(
        FixtureOpts::new(SOURCE, vec![]).with_native_operation_inputs(
            "static/app",
            vec![native_constructor_input(
                SOURCE,
                "const cache = new Map();",
                "cache",
                NativeOperationKind::MapEmpty,
            )],
        ),
    );
    assert_preserved_entry_output(&fixture, "true\n");
    let dependencies = serde_json::json!({
        "native_operation_inputs": [{"defining_chunk": "static/app", "input_index": 0}]
    });
    for binding in ["selected", "result"] {
        let execution = binding_execution_report(&fixture, binding);
        let receipt = &execution["binding_effect_authority"]["receipt"];
        assert_eq!(
            receipt["read_dependencies"], dependencies,
            "{binding}: {execution:#}"
        );
        assert_eq!(
            receipt["rebind_dependencies"], dependencies,
            "{binding}: {execution:#}"
        );
        for call in receipt["calls"].as_array().unwrap() {
            assert_eq!(
                call["dependencies"], dependencies,
                "{binding}: {execution:#}"
            );
        }
    }
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"], dependencies,
        "{report:#}"
    );
}

fn returned_callable_commutation_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        "function make() { return () => 3; }\nconst read = make();\nlet marker = 0;\nmarker = 1;\nconst result = read();\nmarker = 2;\nconsole.log(marker, result);\n",
        vec![
            logical_module_with_anon(
                "markers",
                &[Member::new("marker")],
                &["marker = 1;", "marker = 2;"],
            ),
            logical_module(
                "result",
                &[
                    Member::new("make"),
                    Member::new("read"),
                    Member::new("result"),
                ],
            ),
            logical_module_with_anon("observe", &[], &["console.log(marker, result);"]),
        ],
    )
}

#[test]
fn returned_callable_operation_coverage_discharges_only_its_dataflow_barrier() {
    let fixture =
        run_fixture(returned_callable_commutation_fixture().with_dataflow_aware_s_chain());
    assert_preserved_entry_output(&fixture, "2 3\n");
}

#[test]
fn operation_coverage_does_not_change_strict_callable_purity() {
    let rejected = run_rejection_fixture(returned_callable_commutation_fixture());
    let graph = rejected.dependency_graph();
    let result_id = source_unit_for_binding(&graph, "result");
    let result = graph
        .nodes
        .iter()
        .find(|node| node.id == result_id)
        .unwrap();
    assert_eq!(
        serde_json::to_value(&result.purity).unwrap()["kind"],
        "not_pure"
    );
    assert!(
        graph.edges.iter().any(|edge| {
            serde_json::to_value(&edge.sequenced_cause).unwrap()["rule"] == "strict_chain"
        }),
        "strict-mode rejection must retain the strict chain: {graph:#?}"
    );
}
