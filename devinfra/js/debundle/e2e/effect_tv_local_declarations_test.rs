//! Report-only effect translation validation: local declarations, scalar
//! and numeric operations, and imported primitives.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn local_declaration_operations_cover_private_cells_and_compose_receipts() {
    for body in [
        "const local = seed; return local;",
        "let local = seed; return local;",
        "const first = seed, second = first; return second;",
        "let local; return (local, seed);",
        "return seed; let unreached;",
    ] {
        let source = format!(
            "const seed = 41;\nfunction readSeed() {{ {body} }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "41\n");
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(execution["complete"], true, "{body}: {execution:#}");
        assert_eq!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "actual declaration creation/initialization and reads compose: {body}: {execution:#}"
        );
        let graph = fixture.dependency_graph();
        let result = source_unit_for_binding(&graph, "result");
        let seed = source_unit_for_binding(&graph, "seed");
        assert!(
            graph.edges.iter().any(|edge| {
                edge.source == result
                    && edge.target == seed
                    && edge.binding.as_deref() == Some("seed")
                    && edge.constrains_init_order
            }),
            "the initializer's module read remains ordered: {body}"
        );
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], 1,
                "the private cell has no nonlexical effects; only output remains unknown: {body}: {report:#}"
            );
        }
    }
}

#[test]
fn local_declaration_receipts_preserve_callable_selection_requirements() {
    for (selection, authority) in [
        ("readSeed", "operation_receipt"),
        ("alias", "ordered_root_initializer_uncovered"),
    ] {
        let source = format!(
            "const seed = 41;\nfunction readSeed() {{ return seed; }}\nconst alias = {{run: readSeed}}.run;\nfunction invoke() {{ const selected = {selection}; return selected(); }}\nvar result = invoke();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("alias"),
                    Member::new("invoke"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "41\n");
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"], authority,
            "a local declaration transports actual selection but cannot mint it from an equal value: {selection}: {execution:#}"
        );
    }
}

#[test]
fn local_declaration_receipts_refuse_captured_cells_and_rebinding() {
    for body in [
        "var local = seed; return local;",
        "let local = seed; local = seed; return local;",
        "const local = seed; const read = () => local; return read();",
    ] {
        let source = format!(
            "const seed = 41;\nfunction readSeed() {{ {body} }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "41\n");
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "other cell kinds and assignment effects need their own receipts: {body}: {execution:#}"
        );
    }
}

#[test]
fn abrupt_local_declarations_cannot_seal_binding_effects() {
    for (body, error) in [
        (
            "const first = later; let later = seed; return first;",
            "ReferenceError",
        ),
        ("const local = local; return local;", "ReferenceError"),
        ("const local = throwSeed(); return local;", "41"),
        (
            "const local = seed; local = seed; return local;",
            "TypeError",
        ),
    ] {
        let source = format!(
            "const seed = 41;\nfunction throwSeed() {{ throw seed; }}\nfunction readSeed() {{ {body} }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("throwSeed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_error(&fixture, error);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "TDZ, abrupt initialization and immutable stores cannot seal a normal root: {body}: {execution:#}"
        );
    }
}

#[test]
fn primitive_leaves_close_scalar_callable_receipts_for_actual_inversions() {
    for (expression, expected) in [
        ("void 0", "7 undefined\n"),
        ("!0n", "7 true\n"),
        ("0 === 1", "7 false\n"),
        ("'' !== 'value'", "7 true\n"),
        ("``", "7 \n"),
    ] {
        let source = format!(
            "function compute() {{ return {expression}; }}\nconst independent = 7;\nvar result = compute();\nconsole.log(independent, result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "compute",
                &[Member::new("compute"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_inline_operation_receipt(&fixture, "result", 1, expression);
        let report = fixture.effect_translation_validation();
        assert_eq!(report["swapped_pairs"], 1, "{expression}: {report:#}");
        assert_eq!(
            report["decision"]["status"], "accepted",
            "{expression}: {report:#}"
        );
    }
}

#[test]
fn scalar_primitive_result_does_not_cover_a_discarded_call() {
    let fixture = run_fixture(FixtureOpts::new(
        "let calls = 0;\nfunction operand() { calls++; return 1; }\nfunction compute() { return void operand(); }\nvar result = compute();\nconsole.log(calls, result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("calls"),
                Member::new("operand"),
                Member::new("compute"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1 undefined\n");
    let execution = binding_execution_report(&fixture, "result");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "{execution:#}"
    );
}

#[test]
fn noncoercing_scalar_results_keep_local_call_receipts() {
    for (seed, expression, expected) in [
        ("41", "void seed", "undefined\n"),
        ("41", "!seed", "false\n"),
        ("41", "seed === seed", "true\n"),
        ("41", "seed !== seed", "false\n"),
        (
            "{ valueOf() { throw Error('coerced'); } }",
            "!seed",
            "false\n",
        ),
        (
            "{ valueOf() { throw Error('coerced'); } }",
            "seed === seed",
            "true\n",
        ),
    ] {
        let source = format!(
            "const seed = {seed};\nfunction readSeed() {{ return {expression}; }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_inline_operation_receipt(&fixture, "result", 1, expression);
    }
}

#[test]
fn scalar_call_receipt_accepts_independent_statement_inversion() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return !seed; }\nconst independent = 7;\nvar result = readSeed();\nconsole.log(independent, result);\n",
        vec![logical_module(
            "scalar",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 false\n");
    assert_inline_operation_receipt(&fixture, "result", 1, "scalar inversion");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{report:#}"
    );
}

#[test]
fn numeric_local_updates_keep_receipts_across_an_independent_inversion() {
    for (body, expected) in [
        ("let value = 1; return value++;", "7 1\n"),
        ("let value = 1; return ++value;", "7 2\n"),
        ("let value = 2; return value--;", "7 2\n"),
        ("let value = 2; return --value;", "7 1\n"),
        ("let value = '4'; return ++value;", "7 5\n"),
        ("let value = 1; value++; return value;", "7 2\n"),
    ] {
        let source = format!(
            "function bump() {{ {body} }}\nconst independent = 7;\nvar result = bump();\nconsole.log(independent, result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "update",
                &[Member::new("bump"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_inline_operation_receipt(&fixture, "result", 1, body);
        let report = fixture.effect_translation_validation();
        assert_eq!(report["swapped_pairs"], 1, "{body}: {report:#}");
        assert_eq!(
            report["decision"]["status"], "accepted",
            "{body}: {report:#}"
        );
        assert_eq!(
            report["decision"]["scope"], "statement_order_commutation",
            "{body}: {report:#}"
        );
    }
}

#[test]
fn numeric_parameter_update_store_keeps_receipt_across_an_independent_inversion() {
    let fixture = run_fixture(FixtureOpts::new(
        "function bump(value) { value++; return value; }\nfunction invoke() { return bump(1); }\nconst independent = 7;\nvar result = invoke();\nconsole.log(independent, result);\n",
        vec![logical_module(
            "update",
            &[
                Member::new("bump"),
                Member::new("invoke"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 2\n");
    assert_inline_operation_receipt(&fixture, "result", 2, "parameter update");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{report:#}"
    );
}

#[test]
fn numeric_local_updates_keep_const_tdz_and_symbol_failures_abrupt() {
    for (body, error) in [
        ("const value = 1; return value++;", "TypeError"),
        ("return value++; let value = 1;", "ReferenceError"),
        ("let value = Symbol(); return value++;", "TypeError"),
    ] {
        let source =
            format!("function bump() {{ {body} }}\nvar result = bump();\nconsole.log(result);\n");
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[Member::new("bump"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_error(&fixture, error);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "{body}: {execution:#}"
        );
    }
}

#[test]
fn numeric_local_updates_refuse_bigint_coercion_and_captured_cells() {
    for (body, expected) in [
        ("let value = 1n; return value++;", "1n 0\n"),
        (
            "let value = { valueOf() { return ++calls; } }; return value++;",
            "1 1\n",
        ),
        (
            "let value = 1; const read = () => value; value++; return read();",
            "2 0\n",
        ),
    ] {
        let source = format!(
            "let calls = 0;\nfunction bump() {{ {body} }}\nvar result = bump();\nconsole.log(result, calls);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("calls"),
                    Member::new("bump"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "{body}: {execution:#}"
        );
    }
}

#[test]
fn noncoercing_scalar_operators_do_not_cover_abrupt_children() {
    for expression in ["void later", "!later", "later === later"] {
        let source = format!(
            "const seed = 41;\nfunction readSeed() {{ return {expression}; let later = seed; }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_error(&fixture, "ReferenceError");
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "{expression}: {execution:#}"
        );
    }
}

#[test]
fn noncoercing_scalar_operators_refuse_unproved_getters() {
    for (expression, expected) in [
        ("void seed.value", "undefined 1\n"),
        ("!seed.value", "false 1\n"),
        ("seed.value === seed.value", "false 2\n"),
    ] {
        let source = format!(
            "let counter = 0;\nconst seed = {{ get value() {{ return ++counter; }} }};\nfunction readSeed() {{ return {expression}; }}\nvar result = readSeed();\nconsole.log(result, counter);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("counter"),
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "{expression}: {execution:#}"
        );
    }
}

#[test]
fn imported_primitive_return_does_not_inherit_local_operation_authority() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { literal } from './peer.js';\nconst result = literal();\nconsole.log(result);\n",
            vec![],
        ).with_extra_chunks(&[("static/peer", "export function literal() { return 3; }\n")]),
    );
    assert_preserved_entry_output(&fixture, "3\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert!(
            report["coverage"]["unknown_coverage"][domain]
                .as_u64()
                .unwrap()
                >= 2,
            "{report:#}"
        );
    }
}
